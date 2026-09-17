from contextlib import ExitStack
import io
from pathlib import Path
import re
import subprocess
import sys
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image
import pytest

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tchmaterial_parser.ui import resource_tree, runtime, theme


visible_tree_rows = resource_tree.visible_tree_rows # 未打桩的实现，供需要真实几何的用例使用


def all_tree_rows(treeview): # 隐藏窗口没有真实几何，测试里把已插入的树项全部视为可见
    def walk(parent):
        for item in treeview.get_children(parent):
            yield item
            yield from walk(item)

    return list(walk(""))

RESOURCES = {"books": {"display_name": "电子教材", "children": {
    "primary": {"display_name": "小学", "children": {
        "a": {"display_name": "语文 一年级上册", "content_id": "a"},
        "b": {"display_name": "语文 一年级下册", "content_id": "b", "custom_properties": {"thumbnails": ["https://example.com/cover.png"]}},
    }},
}}}


class ResourceTreeUITest(unittest.TestCase):
    __test__ = False # 由下方 pytest 入口在独立进程运行真实 Tk 交互测试

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:
            if "no display name" in str(error) or "couldn't connect to display" in str(error):
                raise unittest.SkipTest(f"当前环境没有图形显示服务：{error}") from error
            raise
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.addCleanup(self.destroy_widgets)
        enter = self.context.enter_context
        enter(patch.object(runtime, "root", self.root, create=True))
        enter(patch.object(runtime, "ui_scale", 1.0))
        enter(patch.object(runtime, "app_closing", False))
        enter(patch.object(theme, "ui_font_family", "TkDefaultFont", create=True))
        for name, value in (("theme_actions", []), ("themed_widgets", set()), ("current_colors", {}), ("current_theme", "light"), ("switched_theme", "light")):
            enter(patch.object(theme, name, value))
        theme.apply_theme("light")
        enter(patch.object(resource_tree, "thread_it", lambda fn, *args: fn(*args)))
        # 隐藏测试窗口，单独模拟封面进入可视区域，仍执行真实的加载与图片合成代码。
        enter(patch.object(resource_tree, "visible_tree_rows", all_tree_rows))
        cover = io.BytesIO()
        Image.new("RGB", (80, 112), "#c4ded2").save(cover, format="PNG")
        response = type("CoverResponse", (), {"ok": True, "content": cover.getvalue()})()
        enter(patch.object(resource_tree.session, "get", return_value=response))
        self.pane = ttk.Frame(self.root)
        self.urls = tk.Text(self.root, undo=True)
        self.errors = []
        self.root.report_callback_exception = lambda _type, error, _traceback: self.errors.append(error)
        resource_tree.build_resource_tree(self.pane, RESOURCES, self.urls)
        widgets = list(self.descendants(self.pane))
        self.tree = next(widget for widget in widgets if isinstance(widget, ttk.Treeview))
        self.search = next(widget for widget in widgets if isinstance(widget, ttk.Entry))
        self.count = next(widget for widget in widgets if isinstance(widget, ttk.Label) and widget.grid_info().get("column") == 1)
        self.root.update()

    def tearDown(self):
        if hasattr(self, "errors"):
            self.assertEqual(self.errors, [])

    def destroy_widgets(self):
        self.root.update()
        for widget in self.root.winfo_children():
            widget.destroy()

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def toggle(self, item_id):
        self.tree.focus(item_id)
        # 执行已注册的空格事件回调，无需让隐藏窗口抢占键盘焦点。
        command = re.search(r"\[([^\s]+)", self.tree.bind("<space>")).group(1)
        self.root.tk.call(command, "unused")
        self.root.update()

    def expand(self, item_id):
        # 复刻 Tk 展开分类的顺序：先设焦点并发出事件，再把该项置为展开。
        self.tree.focus(item_id)
        self.tree.event_generate("<<TreeviewOpen>>")
        self.tree.item(item_id, open=True)
        self.root.after(resource_tree.SCAN_DEBOUNCE_MS * 2, self.root.quit) # 等去抖后的可见行扫描跑完
        self.root.mainloop()
        self.root.update()

    def filter(self, query):
        self.search.delete(0, "end")
        self.search.insert(0, query)
        self.root.after(180, self.root.quit)
        self.root.mainloop()
        self.root.update()

    def url(self, suffix):
        return resource_tree.build_resource_url(f"books:primary:{suffix}", RESOURCES["books"]["children"]["primary"]["children"][suffix])

    def lines(self):
        return {line.strip() for line in self.urls.get("1.0", "end").splitlines() if line.strip()}

    def image(self, item_id):
        name = str(self.tree.item(item_id, "image")[0])
        self.assertIn(name, self.root.tk.call("image", "names"))
        return Image.open(io.BytesIO(self.root.tk.call(name, "data", "-format", "png")))

    def assert_state(self, item_id, state):
        image = self.image(item_id)
        expected = resource_tree.draw_checkbox_image(18, state, theme.current_colors)
        top = (image.height - expected.height) // 2
        self.assertEqual(image.crop((0, top, 18, top + 18)).convert("RGBA").tobytes(), expected.tobytes())

    def test_visible_rows_follow_real_scrolling(self):
        window = tk.Toplevel(self.root)
        window.geometry("360x300+60+60")
        tree = ttk.Treeview(window, style="Custom.Treeview", show="tree", height=8)
        tree.pack(fill="both", expand=True)
        for index in range(60):
            tree.insert("", "end", iid=f"row{index}", text=f"条目 {index}")
        self.root.update()
        tree.yview_scroll(1, "units")
        self.root.update()

        rows = visible_tree_rows(tree)
        self.assertTrue(rows)
        self.assertTrue(tree.bbox(rows[0]))
        self.assertNotIn("row0", rows) # 上边框里取到的是视口上方那一行，不能算可见

    def test_category_children_are_inserted_on_first_expand(self):
        self.assertTrue(self.tree.exists("books:primary"))
        self.assertFalse(self.tree.exists("books:primary:a"))
        self.assertEqual(self.tree.get_children("books:primary"), (f"books:primary{resource_tree.PLACEHOLDER_SUFFIX}",))
        self.expand("books:primary")
        self.assertEqual(self.tree.get_children("books:primary"), ("books:primary:a", "books:primary:b"))

    def test_expanded_children_show_current_check_state(self):
        self.urls.insert("1.0", self.url("a"))
        self.root.update()
        self.expand("books:primary")
        self.assert_state("books:primary:a", "checked")
        self.assert_state("books:primary:b", "unchecked")

    def test_cover_and_checkbox_survive_search_clear_and_theme_changes(self):
        self.expand("books:primary")
        width = self.image("books:primary:b").width
        self.assertGreater(width, 24)
        self.toggle("books:primary:b")
        for name in ("dark", "light"):
            theme.apply_theme(name)
            for query in ("下册", ""):
                self.filter(query)
                if not query: # 清除搜索后树回到只展开一级的状态
                    self.expand("books:primary")
                self.assertEqual(self.image("books:primary:b").width, width)
                self.assert_state("books:primary:b", "checked")

    def test_filtered_category_only_toggles_visible_resources(self):
        self.filter("下册")
        self.toggle("books:primary")
        self.assertEqual(self.lines(), {self.url("b")})
        self.assert_state("books:primary", "checked")
        self.filter("")
        self.expand("books:primary")
        self.assert_state("books:primary", "partial")
        self.assert_state("books:primary:a", "unchecked")

    def test_filtered_uncheck_preserves_hidden_selection(self):
        self.toggle("books:primary")
        self.filter("下册")
        self.toggle("books:primary")
        self.assertEqual(self.lines(), {self.url("a")})
        self.assert_state("books:primary", "unchecked")
        self.filter("")
        self.assert_state("books:primary", "partial")

    def test_manual_deletion_then_parent_selection_restores_both_urls(self):
        self.expand("books:primary")
        self.toggle("books:primary:b")
        self.urls.delete("1.0", "end")
        self.root.update()
        self.assert_state("books:primary:b", "unchecked")
        self.assertEqual(self.count.cget("text"), "")
        self.toggle("books:primary")
        self.assertEqual(self.lines(), {self.url("a"), self.url("b")})
        self.assertEqual(self.count.cget("text"), "已选 2 项")

    def test_pending_catalog_shows_placeholder_until_resources_arrive(self):
        pane = ttk.Frame(self.root)
        urls = tk.Text(self.root, undo=True)
        apply_resource_list = resource_tree.build_resource_tree(pane, {}, urls, "正在加载资源列表…")
        tree = next(widget for widget in self.descendants(pane) if isinstance(widget, ttk.Treeview))
        self.root.update()
        self.assertEqual([tree.item(item, "text") for item in tree.get_children()], ["正在加载资源列表…"])

        tree.event_generate("<Motion>", x=10, y=10) # 悬停在占位行上不应报错
        self.root.after(600, self.root.quit)
        self.root.mainloop()
        self.root.update()

        urls.insert("1.0", self.url("a")) # 目录还没到时用户就先粘贴了链接
        self.root.update()

        self.tree = tree # 让 assert_state 作用于这棵新建的树
        apply_resource_list(RESOURCES)
        self.root.update()
        self.assertEqual([tree.item(item, "text") for item in tree.get_children()], ["电子教材"])
        self.expand("books:primary")
        self.assert_state("books:primary:a", "checked") # 目录到达后按输入框里的链接恢复勾选
        count = next(widget for widget in self.descendants(pane) if isinstance(widget, ttk.Label) and widget.grid_info().get("column") == 1)
        self.assertEqual(count.cget("text"), "已选 1 项")

        tree.selection_set("books:primary:b") # 目录到达后点选资源仍能写回链接
        tree.focus("books:primary:b")
        command = re.search(r"\[([^\s]+)", tree.bind("<space>")).group(1)
        self.root.tk.call(command, "unused")
        self.root.update()
        pasted = {line.strip() for line in urls.get("1.0", "end").splitlines() if line.strip()}
        self.assertEqual(pasted, {self.url("a"), self.url("b")})

        apply_resource_list({}, "获取资源列表失败，请手动填写资源链接，或重新打开本程序")
        self.root.update()
        self.assertEqual([tree.item(item, "text") for item in tree.get_children()], ["获取资源列表失败，请手动填写资源链接，或重新打开本程序"])

    def test_paste_whitespace_and_undo_sync_without_changing_other_urls(self):
        self.expand("books:primary")
        external = "https://example.com/manual"
        self.urls.insert("1.0", f"  {self.url('b')}  \n{external}")
        self.urls.edit_separator()
        self.root.update()
        self.assert_state("books:primary:b", "checked")
        self.urls.delete("1.0", "end")
        self.urls.edit_separator()
        self.root.update()
        self.assert_state("books:primary:b", "unchecked")
        self.urls.edit_undo()
        self.root.update()
        self.assertIn(self.url("b"), self.lines())
        self.assert_state("books:primary:b", "checked")
        self.toggle("books:primary:b")
        self.assertEqual(self.lines(), {external})


class FakeTreeview: # 按 Tk 的真实几何模拟树视图：上边框内取不到可见行，bbox 是唯一的可见性判据
    def __init__(self, item_count, height=460, first_row=0, border=4, row_height=38):
        self.items = [f"n{index}" for index in range(item_count)]
        self.height = height
        self.first_row = first_row
        self.border = border
        self.row_height = row_height
        self.identify_calls = []

    def get_children(self, item=""):
        return tuple(self.items) if not item else ()

    def winfo_height(self):
        return self.height

    def identify_row(self, y):
        self.identify_calls.append(y)
        # 上边框带：未滚动时取不到行，滚动后取到的是视口上方那一行
        index = self.first_row - 1 if y < self.border else self.first_row + (y - self.border) // self.row_height
        return self.items[index] if 0 <= index < len(self.items) else ""

    def bbox(self, item):
        index = self.items.index(item)
        top = self.border + (index - self.first_row) * self.row_height
        return "" if index < self.first_row or top >= self.height else (0, top, 100, self.row_height)


def test_visible_rows_start_below_the_top_border():
    tree = FakeTreeview(50)
    assert resource_tree.visible_tree_rows(tree) == [f"n{index}" for index in range(12)]
    assert max(tree.identify_calls) < tree.height


def test_visible_rows_skip_the_row_above_the_viewport_after_scrolling():
    tree = FakeTreeview(50, first_row=1)
    assert resource_tree.visible_tree_rows(tree) == [f"n{index}" for index in range(1, 13)]


def test_visible_rows_probe_the_top_border_a_bounded_number_of_times():
    tree = FakeTreeview(50, border=6)
    assert resource_tree.visible_tree_rows(tree)[0] == "n0"
    assert [y for y in tree.identify_calls if y < tree.border] == [0, 2, 4]


def test_visible_rows_are_empty_without_items():
    tree = FakeTreeview(0)
    assert resource_tree.visible_tree_rows(tree) == []
    assert tree.identify_calls == []


@pytest.mark.parametrize("case", unittest.defaultTestLoader.getTestCaseNames(ResourceTreeUITest))
def test_resource_tree_interaction(case):
    # 隔离 Tk 全局状态，并避免 pytest 切换标准流文件描述符干扰 Windows Tcl 的文件读取。
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), case],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    if result.returncode == 77:
        pytest.skip("当前环境没有图形显示服务，需在桌面环境或 Xvfb 中运行")
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    result = unittest.TextTestRunner().run(unittest.TestSuite([ResourceTreeUITest(sys.argv[1])]))
    raise SystemExit(77 if result.skipped else int(not result.wasSuccessful()))
