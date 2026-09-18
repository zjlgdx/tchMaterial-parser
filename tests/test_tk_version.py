# macOS 上过旧的 Tk（早于 8.6.13）会丢掉鼠标点击，启动时要认出来并在界面上说明。
# 这里不依赖真实的 Tk 版本，用假的 tk_patchLevel 覆盖各种取值。
import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tchmaterial_parser import platform_utils


APP_SOURCE = Path(__file__).resolve().parents[1] / "src" / "tchmaterial_parser" / "app.py"


def fake_root(patchlevel): # 只提供 getvar 的假主窗口；patchlevel 为 None 表示读取时抛错
    def getvar(name):
        if patchlevel is None:
            raise RuntimeError(f"读取 {name} 失败")
        return patchlevel

    return SimpleNamespace(getvar=getvar)


class TkPatchlevelTest(unittest.TestCase):
    def test_version_strings_are_parsed_into_comparable_tuples(self) -> None:
        for raw, expected in {"8.6.12": (8, 6, 12), "8.6.13": (8, 6, 13), "9.0.4": (9, 0, 4), "8.6.18": (8, 6, 18)}.items():
            with self.subTest(patchlevel=raw):
                self.assertEqual(platform_utils.tk_patchlevel(fake_root(raw)), expected)

    def test_prerelease_versions_still_compare(self) -> None:
        # 预发布版的后缀不能把整段版本号作废，否则新版 Tk 上的适配会静默失效
        for raw, expected in {"8.6.12b1": (8, 6, 12), "9.1b1": (9, 1), "9.0.4rc1": (9, 0, 4)}.items():
            with self.subTest(patchlevel=raw):
                self.assertEqual(platform_utils.tk_patchlevel(fake_root(raw)), expected)

        self.assertGreaterEqual(platform_utils.tk_patchlevel(fake_root("9.1b1")), (9,))

    def test_unparsable_versions_yield_an_empty_tuple(self) -> None:
        for raw in ("", "unknown", "8", "8..6", None):
            with self.subTest(patchlevel=raw):
                self.assertEqual(platform_utils.tk_patchlevel(fake_root(raw)), ())


class OutdatedMacosTkTest(unittest.TestCase):
    def test_older_tk_on_macos_is_reported(self) -> None:
        with patch.object(platform_utils, "os_name", "Darwin"):
            self.assertEqual(platform_utils.outdated_macos_tk(fake_root("8.6.12")), "8.6.12")
            self.assertEqual(platform_utils.outdated_macos_tk(fake_root("8.6.9")), "8.6.9")

    def test_fixed_tk_on_macos_is_not_reported(self) -> None:
        with patch.object(platform_utils, "os_name", "Darwin"):
            for raw in ("8.6.13", "8.6.17", "8.6.18", "9.0.3", "9.0.4"):
                with self.subTest(patchlevel=raw):
                    self.assertEqual(platform_utils.outdated_macos_tk(fake_root(raw)), "")

    def test_other_systems_are_never_reported(self) -> None:
        for os_name in ("Windows", "Linux"):
            with self.subTest(os_name=os_name):
                with patch.object(platform_utils, "os_name", os_name):
                    self.assertEqual(platform_utils.outdated_macos_tk(fake_root("8.6.12")), "")

    def test_unreadable_version_neither_warns_nor_raises(self) -> None:
        # 版本号读不到时只能什么都不说：拿空元组去和版本元组比较会直接抛 TypeError
        with patch.object(platform_utils, "os_name", "Darwin"):
            self.assertEqual(platform_utils.outdated_macos_tk(fake_root(None)), "")
            self.assertEqual(platform_utils.outdated_macos_tk(fake_root("unknown")), "")

    def test_an_unreadable_version_is_not_logged_as_an_error(self) -> None:
        # 每次切换主题都会走到这里，读不到版本号不该反复刷出错误级别的调用栈
        with self.assertLogs(platform_utils.logger, level="DEBUG") as captured:
            platform_utils.tk_patchlevel(fake_root("unknown"))

        self.assertEqual([record.levelname for record in captured.records], ["DEBUG"])


class DescriptionRowTest(unittest.TestCase):
    # main() 需要真实主窗口才能跑完，这里用 AST 确认说明行的取值来源与条件
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = next(
            node for node in ast.parse(APP_SOURCE.read_text(encoding="utf-8")).body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

    def test_description_items_are_copied_before_being_appended_to(self) -> None:
        # 直接往模块常量上 append 会让第二次启动重复出现提示行
        appends = [
            node for node in ast.walk(self.main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "description_items"
        ]
        self.assertEqual(len(appends), 1)

        assigned = [
            node for node in ast.walk(self.main)
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "description_items" for target in node.targets)
        ]
        self.assertEqual(len(assigned), 1)
        self.assertIsInstance(assigned[0].value, ast.Call)
        self.assertEqual(assigned[0].value.func.id, "list")

    def guarded_append(self) -> ast.If: # 由 outdated_tk 守卫、且会追加说明行的那个分支
        guards = [
            node for node in ast.walk(self.main)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "outdated_tk"
            and any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "append"
                for child in ast.walk(node)
            )
        ]
        self.assertEqual(len(guards), 1) # 未命中时不追加任何说明行
        return guards[0]

    def test_the_extra_row_is_guarded_by_the_outdated_tk_check(self) -> None:
        self.guarded_append()

    def test_the_extra_row_names_the_version_and_the_way_out(self) -> None:
        guard = self.guarded_append()
        interpolated = {
            child.value.id for child in ast.walk(guard)
            if isinstance(child, ast.FormattedValue) and isinstance(child.value, ast.Name)
        }
        literals = "".join(
            child.value for child in ast.walk(guard)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )

        self.assertIn("outdated_tk", interpolated) # 要说清当前是哪个版本
        self.assertIn("python.org", literals) # uv 装的 3.11 同样是 8.6.12，不写清来源等于没说
        self.assertIn("Releases", literals) # 以及不想折腾 Python 的那条出路

    def test_no_dialog_is_used_for_the_warning(self) -> None:
        # 点击失灵时弹窗上的按钮同样点不动，提示只能留在界面里
        for node in ast.walk(self.main):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "outdated_tk":
                dialogs = [
                    child for child in ast.walk(node)
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and isinstance(child.func.value, ast.Name) and child.func.value.id == "messagebox"
                ]
                self.assertEqual(dialogs, [])


if __name__ == "__main__":
    unittest.main()
