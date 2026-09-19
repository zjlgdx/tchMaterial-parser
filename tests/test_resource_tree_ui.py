from contextlib import ExitStack
import io
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
import pytest

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tchmaterial_parser.ui import resource_tree, runtime, theme


visible_tree_rows = resource_tree.visible_tree_rows # 未打桩的实现，供需要真实几何的用例使用
treeview_item = ttk.Treeview.item # 未打桩的实现，供计数包装复用


def counting_item(calls): # 记录 Treeview.item 的调用，用来确认图标刷新只落在可见行上
    def item(self, item_id, option=None, **kwargs):
        calls.append(item_id)
        return treeview_item(self, item_id, option, **kwargs)

    return item


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

MANY_RESOURCES = {"books": {"display_name": "电子教材", "children": { # 末级资源远多于一屏，用来确认刷新只落在可见行上
    "primary": {"display_name": "小学", "children": {
        f"n{index}": {"display_name": f"语文 第 {index} 册", "content_id": f"n{index}"} for index in range(40)
    }},
}}}

NESTED_RESOURCES = {"books": {"display_name": "电子教材", "children": { # 三层分类，用来观察反复展开与列宽
    "primary": {"display_name": "小学", "children": {
        "unit": {"display_name": "第一单元", "children": {
            "a": {"display_name": "语文 一年级上册（含教师教学用书与配套音频）", "content_id": "a"},
        }},
    }},
}}}

COVER_RESOURCES = {"books": {"display_name": "电子教材", "children": { # 带封面的末级多于并发上限，用来观察待载队列
    "primary": {"display_name": "小学", "children": {
        f"c{index}": {
            "display_name": f"语文 第 {index} 册",
            "content_id": f"c{index}",
            "custom_properties": {"thumbnails": [f"https://example.com/cover{index}.png"]},
        }
        for index in range(10)
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

    def build_tree(self, resources):
        pane = ttk.Frame(self.root)
        self.urls = tk.Text(self.root, undo=True)
        resource_tree.build_resource_tree(pane, resources, self.urls)
        self.tree = next(widget for widget in self.descendants(pane) if isinstance(widget, ttk.Treeview))
        self.root.update()
        self.expand("books:primary")
        return self.tree

    def hover(self, tree, item_id):
        y = next((y for y in range(4, 800, 4) if tree.identify_row(y) == item_id), None)
        if y is None: # Tk 9 起，窗口未映射时 identify_row 不再解析出行，这类用例取不到真实几何
            self.skipTest(f"当前 Tk（{self.root.getvar('tk_patchLevel')}）在窗口未映射时不解析 identify_row，取不到 {item_id} 的真实几何")
        tree.event_generate("<Motion>", x=10, y=y)
        self.root.after(600, self.root.quit) # 悬停提示有 450 毫秒延迟
        self.root.mainloop()
        self.root.update()

    def tooltip_labels(self):
        tooltip = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
        return tooltip.winfo_children()[0].winfo_children()

    def install_fake_timers(self):
        # 先把建树排下的真实定时器跑完：产品要是握着真实定时器 id 进入替身阶段，取消就成了空操作
        deadline = time.monotonic() + 2
        while self.root.tk.eval("after info") and time.monotonic() < deadline:
            self.root.update()
        self.assertFalse(self.root.tk.eval("after info"), "建树遗留的真实定时器未到点")

        # 不真正计时：记录下每个定时器，由用例决定何时触发
        timers = []
        enter = self.context.enter_context
        real_after = self.root.after

        def after(delay, callback=None, *args):
            if delay == "idle": # after_idle 走的也是 after，交回真实实现，别记成永不触发的定时器
                return real_after(delay, callback, *args)

            record = [delay, None, f"timer{len(timers)}"]

            def fire(): # Tk 里定时器触发一次就失效，这里照做
                record[1] = None
                callback(*args)

            record[1] = fire
            timers.append(record)
            return record[2]

        def after_cancel(timer_id):
            for timer in timers:
                if timer[2] == timer_id:
                    timer[1] = None

        enter(patch.object(self.root, "after", after))
        enter(patch.object(self.root, "after_cancel", after_cancel))
        return timers

    def live_timers(self, timers):
        return [timer for timer in timers if timer[1] is not None and isinstance(timer[0], int)]

    def scroll(self, tree):
        self.root.tk.call(tree.cget("yscrollcommand"), "0.0", "1.0") # 滚动时 Tk 调用的就是这个回调

    def scan(self, tree):
        self.scroll(tree)
        self.root.after(resource_tree.SCAN_DEBOUNCE_MS * 2, self.root.quit) # 等去抖后的封面扫描跑完
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
        # 真实几何只有被窗口管理器映射后才有；Windows 上隐藏窗口的子窗口不会被映射，所以直接用主窗口。
        self.root.geometry("360x300+60+60")
        self.root.deiconify()
        self.addCleanup(self.root.withdraw)
        tree = ttk.Treeview(self.root, style="Custom.Treeview", show="tree", height=8)
        tree.pack(fill="both", expand=True)
        for index in range(60):
            tree.insert("", "end", iid=f"row{index}", text=f"条目 {index}")

        deadline = time.monotonic() + 5 # 映射由窗口管理器异步完成，等待要有上限
        while not tree.winfo_ismapped() and time.monotonic() < deadline:
            self.root.update()
        if not tree.winfo_ismapped(): # 例如 Windows 服务会话，有 Tk 但没有交互桌面
            self.skipTest("窗口管理器没有映射窗口，取不到真实几何")

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

    def test_checking_a_large_category_only_repaints_visible_rows(self):
        tree = self.build_tree(MANY_RESOURCES)
        visible = ["books:primary:n0", "books:primary:n1"]
        calls = []
        with patch.object(resource_tree, "visible_tree_rows", lambda _treeview: list(visible)), \
                patch.object(ttk.Treeview, "item", counting_item(calls)):
            self.toggle("books")

        self.assertEqual(len(tree.get_children("books:primary")), 40)
        self.assertLessEqual(len(calls), len(visible) + 2)
        self.assert_state("books:primary:n0", "checked")
        self.assert_state("books:primary:n1", "checked")

    def test_rows_scrolled_into_view_show_the_new_check_state(self):
        tree = self.build_tree(MANY_RESOURCES)
        visible = ["books:primary:n0"]
        with patch.object(resource_tree, "SCAN_MAX_WAIT_MS", 60000), \
                patch.object(resource_tree, "visible_tree_rows", lambda _treeview: list(visible)):
            self.toggle("books")
            self.assert_state("books:primary:n0", "checked")
            self.assert_state("books:primary:n20", "unchecked") # 屏幕外的行先保持旧图标

            visible[:] = ["books:primary:n20"]
            self.root.tk.call(tree.cget("yscrollcommand"), "0.0", "1.0") # 滚动时 Tk 调用的就是这个回调
            self.assert_state("books:primary:n20", "checked")

    def test_theme_change_keeps_offscreen_icons_usable(self):
        self.expand("books:primary")
        with patch.object(resource_tree, "visible_tree_rows", lambda _treeview: ["books"]):
            theme.apply_theme("dark")
            self.root.update()

        for item_id in ("books", "books:primary", "books:primary:a", "books:primary:b"):
            self.assertGreater(self.image(item_id).width, 0)
        self.assert_state("books:primary:a", "unchecked") # 屏幕外的行也要换上新配色的复选框

    def test_status_row_gets_no_icon_and_no_cover_request(self):
        requests_before = resource_tree.session.get.call_count
        pane = ttk.Frame(self.root)
        urls = tk.Text(self.root, undo=True)
        resource_tree.build_resource_tree(pane, {}, urls, "正在加载资源列表…")
        tree = next(widget for widget in self.descendants(pane) if isinstance(widget, ttk.Treeview))
        self.root.update()
        self.scan(tree)

        self.assertEqual(tree.item(resource_tree.STATUS_ITEM_ID, "image"), "")
        self.assertEqual(resource_tree.session.get.call_count, requests_before)

    def test_failed_cover_is_not_requested_again(self):
        failed = type("FailedResponse", (), {"ok": False})()
        with patch.object(resource_tree.session, "get", return_value=failed) as failed_get:
            self.expand("books:primary") # 展开后的扫描会请求「语文 一年级下册」的封面
            self.assertEqual(failed_get.call_count, 1)
            self.scan(self.tree)
            self.assertEqual(failed_get.call_count, 1) # 失败已记入负缓存，不再重复请求

    def test_cover_queue_is_replaced_by_the_latest_visible_scan(self):
        visible = [f"books:primary:c{index}" for index in range(6)]
        started = []
        with patch.object(resource_tree, "thread_it", lambda worker, *args: started.append((worker, args))), \
                patch.object(resource_tree, "visible_tree_rows", lambda _treeview: list(visible)):
            tree = self.build_tree(COVER_RESOURCES)
            self.assertEqual(len(started), resource_tree.COVER_WORKERS) # 同时在下载的封面不超过并发上限
            self.assertTrue(all(worker.__name__ == "load_tree_icon" for worker, _args in started))

            visible[:] = [f"books:primary:c{index}" for index in range(6, 10)]
            self.scan(tree)
            self.assertEqual(len(started), resource_tree.COVER_WORKERS)

            for worker, args in list(started): # 前几张下载完成，空出的名额交给最近一次扫描的结果
                worker(*args)
            self.root.update()

        requested = [args[0] for _worker, args in started]
        self.assertEqual(requested[resource_tree.COVER_WORKERS:], [f"books:primary:c{index}" for index in range(6, 10)])
        self.assertNotIn("books:primary:c4", requested) # 滚出视野且还没开始的项被新扫描替换掉
        self.assertNotIn("books:primary:c5", requested)

    def test_evicted_preview_is_fetched_again_on_hover(self):
        with patch.object(resource_tree, "PREVIEW_CACHE_SIZE", 2):
            tree = self.build_tree(COVER_RESOURCES)
            requests_before = resource_tree.session.get.call_count

            self.hover(tree, "books:primary:c0") # 这一册的预览图早已被挤出缓存
            self.assertFalse(any(label.cget("image") for label in self.tooltip_labels()))
            self.assertEqual(resource_tree.session.get.call_count, requests_before + 1)

            self.hover(tree, "books:primary:c1") # 换一行再回来，否则沿用同一次悬停
            self.hover(tree, "books:primary:c0")
            self.assertTrue(any(label.cget("image") for label in self.tooltip_labels()))

    def cover_scan_probe(self, clock):
        # 受控时钟从建树前就生效：建树期间记下的时刻也得来自它，否则调度看到的是两套时间
        # 可见行里放一张还没开始下载的封面：扫描计数看有没有扫，派发记录看扫到的是不是这一次
        started = []
        scans = []
        visible = []
        enter = self.context.enter_context

        def visible_rows(_treeview):
            scans.append(1)
            return list(visible)

        # 只替换资源树模块看到的时钟，别冻住整个进程的 time.monotonic
        enter(patch.object(resource_tree, "time", SimpleNamespace(monotonic=lambda: clock[0])))
        enter(patch.object(resource_tree, "thread_it", lambda _worker, *args: started.append(args[0])))
        enter(patch.object(resource_tree, "visible_tree_rows", visible_rows))
        tree = self.build_tree(COVER_RESOURCES)
        timers = self.install_fake_timers()
        visible.append("books:primary:c0") # 定时器替身就位后再让这张封面可见
        return tree, started, scans, timers

    def test_scroll_events_leave_one_pending_cover_scan(self):
        clock = [1000.0]
        tree, started, scans, timers = self.cover_scan_probe(clock)
        for _index in range(5):
            clock[0] += 0.005
            self.scroll(tree)

        pending = self.live_timers(timers)
        self.assertEqual(len(pending), 1) # 五次事件只留一个待执行的扫描
        self.assertEqual(pending[0][0], resource_tree.SCAN_DEBOUNCE_MS)
        self.assertEqual(started, []) # 到点之前一张封面也不派发

        before = len(scans)
        pending[0][1]()
        self.assertEqual(len(scans) - before, 1) # 到点后只扫一次
        self.assertEqual(started, ["books:primary:c0"])

        clock[0] += 0.005
        self.scroll(tree) # 扫过之后再滚动，重新进入一轮推迟
        pending = self.live_timers(timers)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], resource_tree.SCAN_DEBOUNCE_MS)

    def test_first_event_after_an_idle_period_is_still_deferred(self):
        clock = [1000.0]
        tree, started, _scans, timers = self.cover_scan_probe(clock)

        clock[0] += 10 # 用户停了很久才继续滚动
        self.scroll(tree)

        pending = self.live_timers(timers)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], resource_tree.SCAN_DEBOUNCE_MS) # 新一轮的第一个事件照样要等
        self.assertEqual(started, [])

    def test_continuous_scrolling_scans_at_the_longest_interval(self):
        clock = [1000.0]
        tree, started, _scans, timers = self.cover_scan_probe(clock)

        self.scroll(tree) # 本轮推迟从这里开始计时
        clock[0] += resource_tree.SCAN_MAX_WAIT_MS / 1000 - 0.010
        self.scroll(tree)
        pending = self.live_timers(timers)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][0], 10) # 延迟缩到距最长间隔的剩余时间
        self.assertEqual(started, [])

        clock[0] += 0.020 # 越过最长间隔
        self.scroll(tree)
        self.assertEqual(started, ["books:primary:c0"]) # 立刻扫一次
        self.assertEqual(self.live_timers(timers), []) # 并重新开始计时

        clock[0] += 0.005
        self.scroll(tree)
        pending = self.live_timers(timers)
        self.assertEqual(len(pending), 1) # 下一次事件重新进入一轮推迟
        self.assertEqual(pending[0][0], resource_tree.SCAN_DEBOUNCE_MS)

    def test_second_scan_does_not_repaint_unchanged_rows(self):
        tree = self.build_tree(MANY_RESOURCES)
        with patch.object(resource_tree, "visible_tree_rows", lambda _treeview: ["books:primary:n0", "books:primary:n1"]):
            self.toggle("books")
            self.scan(tree)
            calls = []
            with patch.object(ttk.Treeview, "item", counting_item(calls)):
                self.scan(tree)

        self.assertEqual(calls, []) # 图标没有过期，再扫一次不重绘任何行

    def test_reexpanding_keeps_already_inserted_children(self):
        tree = self.build_tree(NESTED_RESOURCES)
        self.expand("books:primary:unit")
        self.assertEqual(tree.get_children("books:primary:unit"), ("books:primary:unit:a",))

        tree.item("books:primary", open=False)
        self.root.update()
        self.expand("books:primary")

        self.assertEqual(tree.get_children("books:primary"), ("books:primary:unit",)) # 没有重复插入
        self.assertEqual(tree.get_children("books:primary:unit"), ("books:primary:unit:a",))
        self.assertTrue(tree.item("books:primary:unit", "open")) # 子分类保持展开

    def test_expanding_widens_the_tree_column(self):
        tree = self.build_tree(NESTED_RESOURCES)
        width = tree.column("#0", "width")
        self.expand("books:primary:unit") # 这一层的标题更长
        self.assertGreater(tree.column("#0", "width"), width)

    def test_failed_cover_releases_its_slot(self):
        failed = type("FailedResponse", (), {"ok": False})()
        started = []
        with patch.object(resource_tree, "COVER_WORKERS", 1), \
                patch.object(resource_tree, "thread_it", lambda worker, *args: started.append((worker, args))), \
                patch.object(resource_tree, "visible_tree_rows", lambda _treeview: ["books:primary:c0", "books:primary:c1"]):
            self.build_tree(COVER_RESOURCES)
            self.assertEqual([args[0] for _worker, args in started], ["books:primary:c0"]) # 名额只有一个

            with patch.object(resource_tree.session, "get", return_value=failed):
                worker, args = started[0]
                worker(*args) # 第一张下载失败
                self.root.update()

            self.assertEqual([args[0] for _worker, args in started], ["books:primary:c0", "books:primary:c1"])

    def test_cover_downloads_run_on_daemon_threads(self):
        created = []
        real_thread = threading.Thread

        def recording_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            created.append(thread)
            return thread

        with patch.object(resource_tree, "thread_it", runtime.thread_it), \
                patch.object(threading, "Thread", recording_thread):
            self.expand("books:primary") # 展开后的扫描会真的起线程去下载封面

        self.assertTrue(created)
        self.assertTrue(all(thread.daemon for thread in created)) # 关窗时不会被在飞的封面请求拖住

    def test_hover_reload_dispatches_once_while_all_slots_are_busy(self):
        started = []
        with patch.object(resource_tree, "COVER_WORKERS", 1), \
                patch.object(resource_tree, "thread_it", lambda worker, *args: started.append((worker, args))), \
                patch.object(resource_tree, "visible_tree_rows", lambda _treeview: ["books:primary:c1"]):
            tree = self.build_tree(COVER_RESOURCES)
            self.assertEqual([args[0] for _worker, args in started], ["books:primary:c1"]) # 唯一的名额已占满

            self.hover(tree, "books:primary:c0") # 预览图没有缓存，悬停会重新取
            self.hover(tree, "books:primary:c2")
            self.hover(tree, "books:primary:c0")
            dispatched = [args[0] for _worker, args in started]
            self.assertEqual(dispatched.count("books:primary:c0"), 1) # 已在下载的项不重复派发

            self.scan(tree)
            self.assertEqual([args[0] for _worker, args in started], dispatched) # 可见扫描也不会再派发它

    def test_covers_in_flight_are_not_queued_again(self):
        started = []
        with patch.object(resource_tree, "COVER_WORKERS", 2), \
                patch.object(resource_tree, "thread_it", lambda worker, *args: started.append((worker, args))), \
                patch.object(resource_tree, "visible_tree_rows", lambda _treeview: [f"books:primary:c{index}" for index in range(3)]):
            tree = self.build_tree(COVER_RESOURCES)
            self.assertEqual([args[0] for _worker, args in started], ["books:primary:c0", "books:primary:c1"])

            self.scan(tree) # 再扫一次，c0、c1 还在下载，不该重新排队
            worker, args = started[0]
            worker(*args) # c0 完成，空出一个名额
            self.root.update()

            self.assertEqual(
                [args[0] for _worker, args in started],
                ["books:primary:c0", "books:primary:c1", "books:primary:c2"],
            )

    def test_hover_on_a_queued_cover_downloads_it_once(self):
        started = []
        with patch.object(resource_tree, "COVER_WORKERS", 2), \
                patch.object(resource_tree, "thread_it", lambda worker, *args: started.append((worker, args))), \
                patch.object(resource_tree, "visible_tree_rows", lambda _treeview: [f"books:primary:c{index}" for index in range(3)]):
            tree = self.build_tree(COVER_RESOURCES)
            self.assertEqual([args[0] for _worker, args in started], ["books:primary:c0", "books:primary:c1"]) # c2 还在待载列表里

            self.hover(tree, "books:primary:c2")
            self.assertEqual([args[0] for _worker, args in started][-1], "books:primary:c2") # 悬停直接派发

            for worker, args in list(started)[:2]: # c0、c1 先后完成，名额空出来
                worker(*args)
                self.root.update()

            self.assertEqual([args[0] for _worker, args in started].count("books:primary:c2"), 1)

    def test_hover_reload_dispatches_immediately_when_slots_are_free(self):
        started = []
        with patch.object(resource_tree, "thread_it", lambda worker, *args: started.append((worker, args))), \
                patch.object(resource_tree, "visible_tree_rows", lambda _treeview: []):
            tree = self.build_tree(COVER_RESOURCES)
            self.assertEqual(started, []) # 没有可见行就没有封面排队

            self.hover(tree, "books:primary:c0")
            self.assertEqual([args[0] for _worker, args in started], ["books:primary:c0"])
            self.hover(tree, "books:primary:c1")
            self.hover(tree, "books:primary:c0")
            self.assertEqual([args[0] for _worker, args in started].count("books:primary:c0"), 1)

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

    def catalog_tree(self, status="正在加载资源列表…"): # 建一棵仍在等目录的树，返回填入函数与树视图
        pane = ttk.Frame(self.root)
        urls = tk.Text(self.root, undo=True)
        apply_resource_list = resource_tree.build_resource_tree(pane, {}, urls, status)
        widgets = list(self.descendants(pane))
        tree = next(widget for widget in widgets if isinstance(widget, ttk.Treeview))
        self.catalog_search = next(widget for widget in widgets if isinstance(widget, ttk.Entry)) # 这棵树自己的搜索框
        return apply_resource_list, tree

    def catalog_ticks(self, timers): # 仍然有效的目录计时器
        return [timer for timer in timers if timer[0] == resource_tree.CATALOG_TICK_MS and timer[1] is not None]

    def fire_catalog_tick(self, timers):
        self.catalog_ticks(timers)[-1][1]()

    def status_text(self, tree):
        return tree.item(resource_tree.STATUS_ITEM_ID, "text")

    def loading_clock(self):
        clock = [1000.0] # 自造时钟，免得用例真的等上几十秒
        self.context.enter_context(patch.object(resource_tree, "time", SimpleNamespace(monotonic=lambda: clock[0])))
        return clock

    def test_stage_updates_replace_the_status_row(self):
        apply_resource_list, tree = self.catalog_tree()
        self.root.update()

        apply_resource_list(None, "正在下载资源列表（第 2/4 部分）")
        self.root.update()

        self.assertEqual([tree.item(item, "text") for item in tree.get_children()], ["正在下载资源列表（第 2/4 部分）"])

    def test_a_late_stage_update_cannot_replace_a_ready_catalog(self):
        # 目录已经就绪之后再收到阶段提示，不能把整棵树换回一行提示，也不能重新起表
        timers = self.install_fake_timers()
        apply_resource_list, tree = self.catalog_tree()
        self.root.update()
        apply_resource_list(RESOURCES)
        self.root.update()

        apply_resource_list(None, "正在下载资源列表（第 3/4 部分）")
        self.root.update()
        self.catalog_search.insert(0, "语文") # 搜索会整棵重建，过期提示要是留着就会在这里冒出来
        self.root.update()
        for timer in self.live_timers(timers):
            timer[1]()
        self.root.update()

        self.assertFalse(tree.exists(resource_tree.STATUS_ITEM_ID))
        self.assertTrue(tree.exists("books:primary:a"))
        self.assertEqual(self.catalog_ticks(timers), [])

    def test_a_long_status_line_widens_the_tree_column(self):
        # 提示行不经过插入树项那条路径，宽度没人算的话长文案会被裁掉，横向也滚不出来
        apply_resource_list, tree = self.catalog_tree()
        self.root.update()
        narrow = tree.column("#0", "width")

        long_status = "获取资源列表失败，可重新打开本程序重试：" + "ConnectionError：HTTPSConnectionPool(host='example.com', port=443)…"
        apply_resource_list({}, long_status)
        self.root.update()

        self.assertEqual(self.status_text(tree), long_status)
        self.assertGreater(tree.column("#0", "width"), narrow)

    def test_the_widened_column_comes_back_when_the_catalog_arrives(self):
        # 撑开是为了让长提示读得全，目录到了就该按资源本身的宽度重新算，否则横向滚动条一直挂着
        apply_resource_list, tree = self.catalog_tree()
        self.root.update()
        apply_resource_list({}, "获取资源列表失败，可重新打开本程序重试：" + "ConnectionError：HTTPSConnectionPool(host='example.com')…")
        self.root.update()
        widened = tree.column("#0", "width")

        apply_resource_list(RESOURCES)
        self.root.update()

        self.assertFalse(tree.exists(resource_tree.STATUS_ITEM_ID))
        self.assertLess(tree.column("#0", "width"), widened)

    def test_destroying_the_tree_survives_a_failing_timer_cancel(self):
        # 解释器收尾时主窗口可能已经没了，取消定时器会抛 TclError，不能让销毁流程跟着炸
        timers = self.install_fake_timers()
        _apply, tree = self.catalog_tree()
        self.root.update()
        self.assertTrue(self.catalog_ticks(timers))

        def refuse_to_cancel(_timer_id):
            raise tk.TclError('invalid command name "after"')

        with patch.object(self.root, "after_cancel", refuse_to_cancel):
            tree.destroy()
        self.root.update()

        # <Destroy> 回调里漏出来的异常由 tkinter 转交 report_callback_exception，destroy() 自己从不抛，
        # 所以真正能判定护栏在位的是这里：没有异常被转交出来
        self.assertEqual(self.errors, [])

    def test_destroying_the_tree_cancels_the_clock(self):
        timers = self.install_fake_timers()
        _apply, tree = self.catalog_tree()
        self.root.update()
        self.assertTrue(self.catalog_ticks(timers)) # 先确认真的有表在走

        tree.destroy()
        self.root.update()

        self.assertEqual(self.catalog_ticks(timers), [])

    def test_elapsed_time_appears_while_the_catalog_is_loading(self):
        timers = self.install_fake_timers()
        clock = self.loading_clock()
        _apply, tree = self.catalog_tree()
        self.root.update()
        self.assertEqual(self.status_text(tree), "正在加载资源列表…") # 刚开始不显示 0 秒

        clock[0] += 12
        self.fire_catalog_tick(timers)

        self.assertEqual(self.status_text(tree), "正在加载资源列表…（已用 12 秒）")

    def test_a_long_wait_adds_an_actionable_hint(self):
        timers = self.install_fake_timers()
        clock = self.loading_clock()
        _apply, tree = self.catalog_tree()
        self.root.update()

        clock[0] += resource_tree.CATALOG_SLOW_SECONDS
        self.fire_catalog_tick(timers)

        self.assertIn(resource_tree.CATALOG_SLOW_HINT, self.status_text(tree))

    def test_failure_stops_the_clock_and_keeps_its_own_wording(self):
        failure = "获取资源列表失败（连接超时），可在右侧手动填写资源链接下载，或检查网络后重新打开本程序"
        timers = self.install_fake_timers()
        clock = self.loading_clock()
        apply_resource_list, tree = self.catalog_tree()
        self.root.update()
        tick = self.catalog_ticks(timers)[-1][1] # 先抓住回调，取消之后就拿不到了

        apply_resource_list({}, failure)
        self.root.update()

        self.assertEqual(self.catalog_ticks(timers), []) # 终态不再计时
        clock[0] += 600
        tick() # 即便回调被再触发一次，失败原因也不能被秒数改写
        self.assertEqual(self.status_text(tree), failure)

    def test_a_ready_catalog_stops_the_clock(self):
        timers = self.install_fake_timers()
        apply_resource_list, tree = self.catalog_tree()
        self.root.update()

        apply_resource_list(RESOURCES)
        self.root.update()

        self.assertEqual(self.catalog_ticks(timers), [])
        self.assertFalse(tree.exists(resource_tree.STATUS_ITEM_ID))

    def test_macos_replaces_the_card_treeview_field(self):
        # 卡片贴图要逐帧平铺满整个树区域，aqua 上每帧多花十几毫秒
        style = ttk.Style(self.root)
        with patch.object(theme, "os_name", "Darwin"):
            theme.apply_theme("light")

        self.assertIn(theme.FLAT_TREEVIEW_FIELD, style.element_names()) # 元素没建出来时布局只是引用了个空名字
        self.assertNotEqual(style.layout("Custom.Treeview")[0][0], "Treeview.field")
        self.assertEqual(style.lookup("Custom.Treeview", "fieldbackground"), theme.current_colors["surface"])

    def test_other_systems_keep_the_card_treeview_field(self):
        style = ttk.Style(self.root)
        style.layout("Custom.Treeview", style.layout("Treeview")) # 先还原成 sv-ttk 的原始布局
        with patch.object(theme, "os_name", "Windows"):
            theme.apply_theme("light")

        self.assertEqual(style.layout("Custom.Treeview")[0][0], "Treeview.field")

    def test_the_flat_field_draws_no_focus_ring(self):
        # 纯色底板在 Tk 9 上会给取得键盘焦点的控件画一圈蓝边，而原本的卡片贴图从不画
        style = ttk.Style(self.root)
        with patch.object(theme, "os_name", "Darwin"):
            theme.apply_theme("light")

        self.assertEqual(str(style.lookup("Custom.Treeview", "focuswidth")), "0")

    def item_layout(self, style): # 当前生效的树项布局
        return style.layout("Custom.Treeview.Item")

    def indicator_names(self, layout): # 布局里所有指示器元素的名字
        found = []
        for name, options in layout:
            if "indicator" in name:
                found.append(name)
            found.extend(self.indicator_names(options.get("children", [])))
        return found

    def element_names(self, layout):
        found = []
        for name, options in layout:
            found.append(name)
            found.extend(self.element_names(options.get("children", [])))
        return found

    def test_newer_tk_uses_the_builtin_expand_indicator(self):
        # Tk 9 不再把展开状态传给树项元素，贴图做的箭头会一直停在折叠的样子
        style = ttk.Style(self.root)
        with patch.object(theme, "tk_patchlevel", lambda _root: (9, 0, 3)):
            theme.apply_theme("light")

        layout = self.item_layout(style)
        self.assertIn(theme.BUILTIN_TREEITEM_INDICATOR, style.element_names()) # 元素没建出来时布局只是引用了个空名字
        self.assertEqual(self.indicator_names(layout), [theme.BUILTIN_TREEITEM_INDICATOR])
        # 勾选框与封面是画在树项图片上的，换箭头不能把它们挤掉
        self.assertIn("Treeitem.image", self.element_names(layout))
        self.assertIn("Treeitem.text", self.element_names(layout))

    def test_older_tk_keeps_the_themed_expand_indicator(self):
        style = ttk.Style(self.root)
        stock = style.layout("Item")
        style.layout("Custom.Treeview.Item", stock) # 先还原，免得受本进程 Tk 版本影响
        with patch.object(theme, "tk_patchlevel", lambda _root: (8, 6, 12)):
            theme.apply_theme("light")

        self.assertEqual(self.indicator_names(self.item_layout(style)), ["Treeitem.indicator"])

    def test_an_unreadable_tk_version_keeps_the_themed_indicator(self):
        style = ttk.Style(self.root)
        style.layout("Custom.Treeview.Item", style.layout("Item"))
        with patch.object(theme, "tk_patchlevel", lambda _root: ()): # 版本号读不到时不动布局
            theme.apply_theme("light")

        self.assertEqual(self.indicator_names(self.item_layout(style)), ["Treeitem.indicator"])

    def test_switching_themes_repeatedly_keeps_the_builtin_indicator(self):
        style = ttk.Style(self.root)
        with patch.object(theme, "tk_patchlevel", lambda _root: (9, 0, 3)):
            for name in ("light", "dark", "light", "dark"): # 元素按主题注册，重复创建会抛 TclError
                theme.apply_theme(name)
                self.assertEqual(self.indicator_names(self.item_layout(style)), [theme.BUILTIN_TREEITEM_INDICATOR])

    def test_switching_themes_repeatedly_keeps_the_flat_field(self):
        # 元素按 ttk 主题注册，浅色与深色是两个主题；重复创建同名元素会抛 TclError
        style = ttk.Style(self.root)
        with patch.object(theme, "os_name", "Darwin"):
            for name in ("light", "dark", "light", "dark"):
                theme.apply_theme(name)
                self.assertNotEqual(style.layout("Custom.Treeview")[0][0], "Treeview.field")
                self.assertEqual(style.lookup("Custom.Treeview", "fieldbackground"), theme.current_colors["surface"])

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


def test_replace_layout_element_only_swaps_the_named_element():
    layout = [("A.field", {"sticky": "nswe", "children": [
        ("A.padding", {"children": [("A.indicator", {"side": "left"}), ("A.text", {"side": "left"})]}),
    ]})]

    replaced = theme.replace_layout_element(layout, "A.indicator", "B.indicator")

    assert replaced == [("A.field", {"sticky": "nswe", "children": [
        ("A.padding", {"children": [("B.indicator", {"side": "left"}), ("A.text", {"side": "left"})]}),
    ]})]
    assert layout[0][1]["children"][0][1]["children"][0][0] == "A.indicator" # 原布局不被就地改写


def test_replace_layout_element_leaves_unrelated_layouts_alone():
    layout = [("X.field", {"sticky": "nswe"})]
    assert theme.replace_layout_element(layout, "A.indicator", "B.indicator") == layout


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
        reason = result.stdout.strip().splitlines() # 子进程把跳过原因写在标准输出的最后一行
        pytest.skip(reason[-1] if reason else "当前环境没有图形显示服务，需在桌面环境或 Xvfb 中运行")
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    result = unittest.TextTestRunner().run(unittest.TestSuite([ResourceTreeUITest(sys.argv[1])]))
    if result.skipped: # 父进程只看得到退出码与输出，跳过原因要显式送出去
        print(result.skipped[0][1])
    raise SystemExit(77 if result.skipped else int(not result.wasSuccessful()))
