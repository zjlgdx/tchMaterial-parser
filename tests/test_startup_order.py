import ast
from pathlib import Path
import unittest

APP_SOURCE = Path(__file__).resolve().parents[1] / "src" / "tchmaterial_parser" / "app.py"


def call_name(node): # 取调用目标的名称，如 tk.Tk() 取 "Tk"、thread_it() 取 "thread_it"
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def direct_calls(node): # 枚举随语句立即执行的调用，跳过函数与 lambda 体（它们要等事件触发才运行）
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from direct_calls(child)


class StartupOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = ast.parse(APP_SOURCE.read_text(encoding="utf-8"))
        cls.main = next(node for node in cls.module.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        root_calls = [node for node in direct_calls(cls.main) if call_name(node) == "Tk"]
        assert len(root_calls) == 1, "main() 中应当只创建一个 Tk 主窗口"
        cls.root_line = root_calls[0].lineno

    def all_calls(self, name): # 模块内所有指向该名称的调用，含嵌套函数中的调用
        return [node for node in ast.walk(self.module) if isinstance(node, ast.Call) and call_name(node) == name]

    def dialog_calls(self, node): # 弹窗调用：messagebox.showwarning、messagebox.askokcancel 等
        return [
            child for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name) and child.func.value.id == "messagebox"
        ]

    def test_no_dialog_runs_before_the_main_window_is_created(self):
        # 没有 root 时 tkinter 会隐式创建一个 Tk 实例，于是多出一个空白窗口
        early_dialogs = [node.lineno for node in self.dialog_calls(self.main) if node.lineno < self.root_line]
        self.assertEqual(early_dialogs, [])
        immediate_dialogs = [node.lineno for node in direct_calls(self.main) if node in self.dialog_calls(self.main)]
        self.assertTrue(all(lineno > self.root_line for lineno in immediate_dialogs))
        for statement in self.module.body:
            if not isinstance(statement, ast.FunctionDef):
                self.assertEqual(self.dialog_calls(statement), [])

    def test_resource_list_is_fetched_in_background_after_the_window_exists(self):
        fetch_calls = self.all_calls("fetch_resource_list")
        self.assertTrue(fetch_calls)
        self.assertEqual([node for node in direct_calls(self.main) if node in fetch_calls], []) # 构造阶段不抓取
        self.assertTrue(all(node.lineno > self.root_line for node in fetch_calls))

    def test_loading_thread_is_started_by_the_event_loop(self):
        # 主循环跑起来之前，后台线程调用的 root.after_idle 只会等上一秒然后抛错，结果被静默丢弃，界面会永远停在提示行
        scheduled = [
            node for node in direct_calls(self.main)
            if call_name(node) in ("after", "after_idle")
            and any(isinstance(argument, ast.Name) and argument.id == "thread_it" for argument in node.args)
        ]
        self.assertTrue(scheduled)
        self.assertEqual([node for node in direct_calls(self.main) if call_name(node) == "thread_it"], []) # 不在构造阶段直接开线程


if __name__ == "__main__":
    unittest.main()
