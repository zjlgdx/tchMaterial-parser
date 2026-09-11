# -*- coding: utf-8 -*-
"""启动不阻塞：建窗与目录加载互不等待（A3、任务 9）。需要 Tk，缺 Tk 时跳过。"""

import ast
import os
import threading

import pytest

tk = pytest.importorskip("tkinter")

from tchmaterial_parser.core.catalog import CatalogNode  # noqa: E402
import tchmaterial_parser.ui.app as app_module  # noqa: E402

SAMPLE = {"tag-edu": CatalogNode("tag-edu", "电子教材", children={
    "book-1": CatalogNode("book-1", "语文一年级上册", "assets_document")})}

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "tchmaterial_parser", "ui", "app.py")


@pytest.fixture
def gated_app(monkeypatch):
    """目录加载卡在一个闸门上，由用例决定什么时候放行。"""
    gate = threading.Event()
    entered = threading.Event()
    state = {"calls": 0}

    def blocking_load(client, helper=None, progress_cb=None):
        state["calls"] += 1
        entered.set()
        gate.wait(timeout=10)
        return SAMPLE, False, None

    monkeypatch.setattr(app_module, "load_catalog", blocking_load)

    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)

    app.root.withdraw()
    yield app, gate, entered, state
    gate.set()
    app.catalog_helper.cancel()
    if app.catalog_thread.is_alive():
        app.catalog_thread.join(timeout=5)
    try:
        app.root.destroy()
    except tk.TclError:
        pass # 用例里可能已经自己关掉了


def test_constructor_returns_before_the_catalog_is_loaded(gated_app):
    app, gate, entered, state = gated_app

    # 构造函数已经返回，但加载线程还卡在闸门上
    assert entered.wait(timeout=5), "后台加载线程没有启动"
    assert app.catalog_thread.is_alive()
    assert app.resource_list == {}, "构造函数等到了目录加载完成"

    # 此刻窗口已经建好并且可交互
    app.root.update()
    assert app.root.winfo_exists()
    assert app.download_btn.winfo_exists()
    labels = [app.selector.tree.item(i, "text") for i in app.selector.tree.get_children("")]
    assert labels == [app_module.PLACEHOLDER_TEXT], labels


def test_tree_is_filled_after_the_result_arrives(gated_app):
    app, gate, entered, state = gated_app
    assert entered.wait(timeout=5)

    # 后台线程用 root.after 交回结果，那要求主线程确实在 mainloop 里；
    # 所以先进事件循环，再从循环内部放行闸门
    def watch():
        if app.resource_list:
            app.root.quit()
        else:
            app.root.after(20, watch)

    app.root.after(10, gate.set)
    app.root.after(30, watch)
    app.root.after(5000, app.root.quit) # 兜底，别把用例挂死
    app.root.mainloop()

    assert app.resource_list == SAMPLE
    labels = [app.selector.tree.item(i, "text") for i in app.selector.tree.get_children("")]
    assert labels == ["电子教材"], labels


def test_result_is_dropped_when_the_window_is_already_gone(gated_app):
    """关窗后目录才加载完：丢弃这次界面更新，而不是让线程带着异常死掉。"""
    app, gate, entered, state = gated_app
    assert entered.wait(timeout=5)

    app.root.destroy() # 用户先关了窗
    gate.set()
    app.catalog_thread.join(timeout=5)

    assert not app.catalog_thread.is_alive(), "加载线程没有正常结束"


def test_catalog_load_runs_off_the_main_thread(monkeypatch):
    seen = {}

    def record_thread(client, helper=None, progress_cb=None):
        seen["thread"] = threading.current_thread()
        seen["is_main"] = threading.current_thread() is threading.main_thread()
        return SAMPLE, False, None

    monkeypatch.setattr(app_module, "load_catalog", record_thread)
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)
    app.root.update()

    assert seen["is_main"] is False, "目录加载跑在主线程上"
    assert seen["thread"].daemon is True
    app.root.destroy()


def test_constructor_does_not_fetch_anything_itself():
    """静态兜底：__init__ 里不得出现任何目录抓取调用。"""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    app_cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "App")
    init = next(n for n in app_cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")

    calls = {ast.unparse(n.func) for n in ast.walk(init) if isinstance(n, ast.Call)}
    for forbidden in ("load_catalog", "self.catalog_helper.fetch_tree",
                      "self.catalog_helper.fetch_version", "self.load_resource_list"):
        assert forbidden not in calls, "__init__ 里直接调用了 %s" % forbidden
    assert "self.start_catalog_load" in calls


def test_closing_cancels_the_catalog_helper():
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "on_closing")
    calls = {ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "self.catalog_helper.cancel" in calls, calls
    assert "self.downloads.cancel_all" in calls, calls


def test_result_arriving_before_mainloop_is_not_lost(monkeypatch):
    """命中缓存时加载可能比 mainloop 起得还早，这一次结果绝不能丢。

    直接用 root.after 从工作线程投递会在这种时序下抛 RuntimeError，
    界面就永远停在加载占位上。
    """
    def instant_load(client, helper=None, progress_cb=None):
        return SAMPLE, False, None

    monkeypatch.setattr(app_module, "load_catalog", instant_load)
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()

    # 还没进 mainloop 就让加载线程跑完
    app.catalog_thread.join(timeout=5)
    assert not app.catalog_thread.is_alive()
    assert app.resource_list == {}, "结果不该在 mainloop 之前就被应用"

    def watch():
        if app.resource_list:
            app.root.quit()
        else:
            app.root.after(20, watch)

    app.root.after(20, watch)
    app.root.after(5000, app.root.quit)
    app.root.mainloop()

    assert app.resource_list == SAMPLE, "mainloop 起来之后结果丢了"
    labels = [app.selector.tree.item(i, "text") for i in app.selector.tree.get_children("")]
    assert labels == ["电子教材"], labels
    app.root.destroy()


def test_closing_actually_sets_both_cancel_flags(monkeypatch):
    """行为断言：真的构造 App、真的走关窗流程、两个取消标志都被置位。

    只比对源码文本是测不出回归的——把 cancel_all() 的函数体换成 pass，
    源码断言照样绿。
    """
    monkeypatch.setattr(app_module, "load_catalog",
                        lambda client, helper=None, progress_cb=None: (SAMPLE, False, None))
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)

    assert app.downloads._cancelled.is_set() is False
    assert app.catalog_helper.cancelled.is_set() is False

    destroyed = []
    monkeypatch.setattr(app.root, "destroy", lambda: destroyed.append(True))
    app.on_closing()

    assert app.downloads._cancelled.is_set() is True, "下载线程池的取消标志没有被置位"
    assert app.catalog_helper.cancelled.is_set() is True, "目录加载的取消标志没有被置位"
    assert destroyed == [True], "窗口没有被销毁"

    monkeypatch.undo()
    app.root.destroy()


def test_closing_asks_before_cancelling_live_downloads(monkeypatch):
    """还有任务在飞时要先问用户；用户说不，就什么都不该动。"""
    monkeypatch.setattr(app_module, "load_catalog",
                        lambda client, helper=None, progress_cb=None: (SAMPLE, False, None))
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)

    app.downloads._states.append({"download_url": "u", "save_path": "/tmp/x.pdf",
                                  "downloaded_size": 0, "total_size": 0,
                                  "finished": False, "failed_reason": None})

    asked = []
    monkeypatch.setattr(app_module.messagebox, "askokcancel",
                        lambda *a, **k: asked.append(True) or False)
    destroyed = []
    monkeypatch.setattr(app.root, "destroy", lambda: destroyed.append(True))

    app.on_closing()

    assert asked == [True], "有任务在飞却没有询问用户"
    assert destroyed == [], "用户点了取消，窗口却还是被销毁了"
    assert app.downloads._cancelled.is_set() is False, "用户点了取消，却已经把下载取消了"

    monkeypatch.undo()
    app.downloads._states.clear()
    app.root.destroy()


def test_completion_gate_opens_only_after_the_whole_batch_is_submitted(monkeypatch, tmp_path):
    """开闸必须发生在整批提交之后。

    检查赋值在 AST 的哪一层是测不出回归的——把它移到循环之前，那种检查照样绿，
    而那正是 P0-1 复发的形态。这里改成真的跑一遍 download()：在提交过程中
    每投递一个任务就让轮询器跑一次，断言它期间一次都不判定完成。
    """
    monkeypatch.setattr(app_module, "load_catalog",
                        lambda client, helper=None, progress_cb=None: (SAMPLE, False, None))
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)

    finished = []
    monkeypatch.setattr(app, "finish_downloads", lambda snapshot: finished.append(snapshot))
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(app_module.filedialog, "askdirectory", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(app_module, "parse",
                        lambda client, url: ("https://x/a.pdf", "x", "书"))

    # 每个任务一提交就「已经完成」，并在此刻让轮询器跑一次：
    # 若开闸在循环里，这时它就会看到一个只有一两条的半截快照并判定完成
    during = []

    def submit_and_poll(url, save_path):
        app.downloads._states.append({"download_url": url, "save_path": save_path,
                                      "downloaded_size": 8, "total_size": 8,
                                      "finished": True, "failed_reason": None})
        app.poll_downloads()
        during.append(len(finished))

    monkeypatch.setattr(app.downloads, "submit", submit_and_poll)

    app.url_text.insert("1.0", "\n".join(
        "https://basic.smartedu.cn/tchMaterial/detail?contentId=%d" % i for i in range(4)))
    app.download()

    assert during == [0, 0, 0, 0], "提交过程中就判定了完成：%s" % during
    assert finished == [], "提交还没结束就弹了完成提示"

    # 整批提交完之后，轮询器应当恰好判定一次完成并把按钮还回来
    app.poll_downloads()
    assert len(finished) == 1, "整批提交完之后没有判定完成"
    assert finished[0].total == 4
    # 按钮的恢复由 finish_downloads 负责，这里它被替身接管了，
    # 那条链路另有 test_poller_ignores_snapshots_while_the_gate_is_closed 覆盖

    monkeypatch.undo()
    app.downloads._states.clear()
    app.root.destroy()


def test_poller_ignores_snapshots_while_the_gate_is_closed(monkeypatch):
    """闸门未开时，轮询器不该因为「此刻恰好没有在飞任务」就报完成。"""
    monkeypatch.setattr(app_module, "load_catalog",
                        lambda client, helper=None, progress_cb=None: (SAMPLE, False, None))
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)

    finished = []
    monkeypatch.setattr(app, "finish_downloads", lambda snapshot: finished.append(snapshot))

    # 造出一个「已登记两个、都已完成」的半截快照
    for i in range(2):
        app.downloads._states.append({"download_url": "u%d" % i, "save_path": "/tmp/x.pdf",
                                      "downloaded_size": 8, "total_size": 8,
                                      "finished": True, "failed_reason": None})

    app.download_session = False # 闸门未开
    app.poll_downloads()
    assert finished == [], "闸门没开就报了完成"

    app.download_session = True
    app.poll_downloads()
    assert len(finished) == 1, "闸门开了之后应当恰好报一次完成"

    app.poll_downloads()
    assert len(finished) == 1, "完成通知重复触发"

    monkeypatch.undo()
    app.downloads._states.clear()
    app.root.destroy()


def test_submit_loop_always_opens_the_gate_and_restores_the_button(monkeypatch, tmp_path):
    """投递循环里任何一个意外异常都不该让按钮永久卡死（R2 P1-2）。

    没有 finally 的话：开闸语句被跳过 -> 轮询器不判定 -> finish_downloads
    永远不来 -> 按钮停在 disabled，用户只能重启程序。
    """
    monkeypatch.setattr(app_module, "load_catalog",
                        lambda client, helper=None, progress_cb=None: (SAMPLE, False, None))
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)
    tmp_dir = tmp_path

    # 两条链接才会走「选文件夹」分支；单条会弹保存对话框，那是个真模态窗口
    app.url_text.insert("1.0", "https://basic.smartedu.cn/tchMaterial/detail?contentId=x\n"
                               "https://basic.smartedu.cn/tchMaterial/detail?contentId=y")
    monkeypatch.setattr(app_module, "parse",
                        lambda client, url: ("https://x/a.pdf", "x", "标题"))
    monkeypatch.setattr(app_module, "build_save_path",
                        lambda d, t: (_ for _ in ()).throw(RuntimeError("造出来的意外")))
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(app_module.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(app_module.filedialog, "askdirectory", lambda *a, **k: str(tmp_dir))

    app.download()

    assert str(app.download_btn.cget("state")) == "normal", "按钮卡在 disabled 上"
    assert app.download_session is False

    monkeypatch.undo()
    app.root.destroy()


def test_cancelling_the_save_dialog_restores_the_button(monkeypatch):
    """单链接时用户取消保存对话框，按钮同样要回来。"""
    monkeypatch.setattr(app_module, "load_catalog",
                        lambda client, helper=None, progress_cb=None: (SAMPLE, False, None))
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)

    app.url_text.insert("1.0", "https://basic.smartedu.cn/tchMaterial/detail?contentId=x")
    monkeypatch.setattr(app_module, "parse",
                        lambda client, url: ("https://x/a.pdf", "x", "标题"))
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename", lambda *a, **k: "")

    app.download()

    assert str(app.download_btn.cget("state")) == "normal", "取消保存对话框后按钮没回来"
    assert app.download_session is False

    monkeypatch.undo()
    app.root.destroy()


def test_poller_survives_an_exception_in_poll_downloads(monkeypatch):
    """poll_downloads 抛异常不该把整个轮询器带走（R2 P2-1）。

    它原本排在 root.after 重排之前，一旦抛，重排语句就执行不到，
    目录结果、进度、完成提示全部停摆。
    """
    monkeypatch.setattr(app_module, "load_catalog",
                        lambda client, helper=None, progress_cb=None: (SAMPLE, False, None))
    try:
        app = app_module.App()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    app.root.withdraw()
    app.catalog_thread.join(timeout=5)

    scheduled = []
    real_after = app.root.after

    def spy_after(delay, callback=None, *args):
        # 不能用 is 比较绑定方法：每次属性访问都会新建一个绑定方法对象
        if getattr(callback, "__name__", "") == "drain_ui_queue":
            scheduled.append(delay)
            return "spy" # 不真的排期，免得用例之间互相干扰
        return real_after(delay, callback, *args)

    monkeypatch.setattr(app.root, "after", spy_after)
    monkeypatch.setattr(app, "poll_downloads",
                        lambda: (_ for _ in ()).throw(RuntimeError("造出来的意外")))

    app.drain_ui_queue() # 不该抛

    assert scheduled, "poll_downloads 抛异常之后没有再排下一次 tick，轮询器死了"

    # 队列里的回调同样不该被它连累
    ran = []
    monkeypatch.setattr(app, "poll_downloads",
                        lambda: (_ for _ in ()).throw(RuntimeError("再来一次")))
    app.ui_queue.put(lambda: ran.append(True))
    app.drain_ui_queue()
    assert ran == [True], "队列回调被 poll_downloads 的异常挡住了"

    monkeypatch.undo()
    app.root.destroy()
