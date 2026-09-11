# -*- coding: utf-8 -*-
"""下载：.part + 原子改名 + 失败清残件（C3）、状态锁（B1/B2）、
并发上限与重试续传（B7/C3）。"""

import os
import threading
import time

import pytest

from conftest import FakeResponse, FakeSession
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import naming
from tchmaterial_parser.core.downloader import (DownloadManager, content_range_starts_at,
                                                 new_download_state, response_validator)
from tchmaterial_parser.core.http import HttpClient

URL = "http://example.invalid/a.pdf"


class ScriptedSession:
    """按顺序吐出预置响应，并记录每次请求的头部。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.proxies = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("headers") or {}))
        if not self.responses:
            raise AssertionError("请求次数超出脚本预设")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_manager(session, config=None):
    config = config or AppConfig(chunk_size=8, max_retries=3)
    client = HttpClient(config=config, session=session)
    return DownloadManager(client, config=config)


@pytest.fixture(autouse=True)
def _clear_reservations():
    naming.clear_reservations()
    yield
    naming.clear_reservations()


# ---- C3：.part、原子改名、失败清残件 ----

def test_success_leaves_only_the_final_file(tmp_path):
    seen = []
    chunks = [b"%PDF-1.4 ", b"body-bytes ", b"%%EOF"]
    save_path = str(tmp_path / "数学.pdf")

    response = FakeResponse(200, chunks, on_chunk=lambda i: seen.append(sorted(os.listdir(tmp_path))))
    manager = make_manager(FakeSession(default=response))
    manager.download_file(URL, save_path)

    assert all(entry == ["数学.pdf.part"] for entry in seen), seen
    assert sorted(os.listdir(tmp_path)) == ["数学.pdf"]
    assert open(save_path, "rb").read() == b"".join(chunks)
    assert manager.states()[0]["failed_reason"] is None


@pytest.mark.parametrize("code, fragment", [
    (401, "授权失败"), (403, "授权失败"),
    (404, "服务器返回状态码 404"), (500, "服务器返回状态码 500"),
])
def test_http_error_leaves_no_residue(tmp_path, code, fragment):
    save_path = str(tmp_path / "语文.pdf")
    (tmp_path / "语文.pdf.part").write_text("上一次留下的残件", encoding="utf-8")

    manager = make_manager(FakeSession(default=FakeResponse(code)),
                           config=AppConfig(chunk_size=8, max_retries=0))
    manager.download_file(URL, save_path)

    assert os.listdir(tmp_path) == []
    assert fragment in manager.states()[0]["failed_reason"]


def test_mid_stream_failure_removes_part_file(tmp_path):
    save_path = str(tmp_path / "英语.pdf")
    session = ScriptedSession([FakeResponse(200, [b"aaa", b"bbb", b"ccc"], boom_after=2)] * 4)
    manager = make_manager(session, config=AppConfig(chunk_size=8, max_retries=0))
    manager.download_file(URL, save_path)

    assert os.listdir(tmp_path) == []
    assert "模拟的连接中断" in manager.states()[0]["failed_reason"]


# ---- B7：重试与 If-Range 续传 ----

def test_retry_sends_range_and_if_range(tmp_path, monkeypatch):
    """瞬时错误重试；第 2 次起必须同时带 Range 与 If-Range。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    first = FakeResponse(200, [b"AAAA", b"BBBB", b"CCCC"], boom_after=2,
                         headers={"ETag": "v1", "Content-Length": "12"})
    # 真实的 206 一定带 Content-Range（RFC 9110 §15.3.7），替身少一个头就会
    # 把「缺头该不该信」这类判断测成假绿
    second = FakeResponse(206, [b"CCCC"], headers={"ETag": "v1", "Content-Length": "4",
                                                   "Content-Range": "bytes 8-11/12"})
    session = ScriptedSession([first, second])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    assert manager.states()[0]["failed_reason"] is None
    assert manager.states()[0]["attempts"] == 2

    assert session.calls[0][1] == {}                       # 首次不带 Range
    resume_headers = session.calls[1][1]
    assert resume_headers["Range"] == "bytes=8-"           # 已落盘 8 字节
    assert resume_headers["If-Range"] == "v1"

    assert open(save_path, "rb").read() == b"AAAABBBBCCCC"
    assert os.listdir(tmp_path) == ["书.pdf"]


def test_changed_etag_restarts_from_scratch(tmp_path, monkeypatch):
    """续传时文件已变：最终内容必须是新文件全文，而不是新旧拼接。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    old_prefix = [b"OLDOLDOL", b"DXXX"]
    first = FakeResponse(200, old_prefix, boom_after=1,
                         headers={"ETag": "v1", "Content-Length": "12"})
    # 服务端接受了 Range，但校验子已经变了
    stale = FakeResponse(206, [b"NEWTAIL!"], headers={"ETag": "v2", "Content-Length": "8"})
    fresh = FakeResponse(200, [b"NEWNEWNE", b"WFULL!!!"],
                         headers={"ETag": "v2", "Content-Length": "16"})
    session = ScriptedSession([first, stale, fresh])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    content = open(save_path, "rb").read()
    assert content == b"NEWNEWNEWFULL!!!", content
    assert b"OLD" not in content, "拼出了旧文件前缀 + 新文件后缀"
    assert manager.states()[0]["failed_reason"] is None

    # 结果对还不够，路径也要对：必须是「发现校验子不符 -> 当场丢弃 -> 无 Range 重下」，
    # 而不是靠又一次重试碰巧撞对
    assert session.calls[1][1]["If-Range"] == "v1"
    assert session.calls[2][1] == {}, "重下时不该再带 Range"
    assert stale.closed is True, "过期的续传响应没有被关掉"
    assert manager.states()[0]["attempts"] == 2, "重下不该额外消耗一次重试"


def test_missing_validator_degrades_to_full_restart(tmp_path, monkeypatch):
    """三个校验子都拿不到时降级为不续传，任务仍要成功。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    no_validator = {"Content-Length": ""}
    first = FakeResponse(200, [b"AAAA", b"BBBB"], boom_after=1, headers=no_validator)
    second = FakeResponse(200, [b"AAAA", b"BBBB"], headers=no_validator)
    first.headers.pop("Content-Length", None)
    second.headers.pop("Content-Length", None)

    session = ScriptedSession([first, second])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    assert response_validator(second) is None, "这个响应本不该有校验子"
    assert session.calls[1][1] == {}, "没有校验子却仍然发了 Range"
    assert open(save_path, "rb").read() == b"AAAABBBB"
    assert manager.states()[0]["failed_reason"] is None


def test_auth_failure_is_not_retried(tmp_path):
    save_path = str(tmp_path / "书.pdf")
    session = ScriptedSession([FakeResponse(401)])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    assert len(session.calls) == 1, "401 不该重试"
    assert manager.states()[0]["attempts"] == 1


def test_retries_give_up_after_the_configured_count(tmp_path, monkeypatch):
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")
    session = ScriptedSession([FakeResponse(500)] * 10)
    manager = make_manager(session, config=AppConfig(chunk_size=8, max_retries=2))
    manager.download_file(URL, save_path)

    assert len(session.calls) == 3, session.calls # 首次 + 2 次重试
    assert manager.states()[0]["attempts"] == 3
    assert os.listdir(tmp_path) == []


# ---- M7：预留的生命周期 ----

def test_release_path_returns_the_original_name(tmp_path):
    """失败后重下同一本教材，仍要拿回不带序号的原名。"""
    from tchmaterial_parser.core.downloader import build_save_path

    first = build_save_path(str(tmp_path), "语文一年级上册")
    assert os.path.basename(first) == "语文一年级上册.pdf"

    session = ScriptedSession([FakeResponse(404)])
    manager = make_manager(session, config=AppConfig(chunk_size=8, max_retries=0))
    manager.download_file(URL, first)

    assert os.listdir(tmp_path) == [], "失败后不该留下文件"
    assert first not in naming.reserved_paths(), "预留没有被归还"

    again = build_save_path(str(tmp_path), "语文一年级上册")
    assert os.path.basename(again) == "语文一年级上册.pdf", "重下拿到了幽灵序号"


def test_successful_download_releases_its_reservation(tmp_path):
    """成功路径也要归还预留。

    只断言「下次拿到 (2)」是测不出回归的：成功的文件已经躺在磁盘上，
    即使 release_path 被删掉，让号也照样会发生。
    """
    from tchmaterial_parser.core.downloader import build_save_path

    first = build_save_path(str(tmp_path), "数学")
    assert first in naming.reserved_paths()

    manager = make_manager(FakeSession(default=FakeResponse(200, [b"x" * 8])))
    manager.download_file(URL, first)

    assert first not in naming.reserved_paths(), "成功后没有归还预留"
    assert os.path.basename(build_save_path(str(tmp_path), "数学")) == "数学 (2).pdf"


# ---- 并发上限 ----

def test_concurrency_never_exceeds_the_configured_cap(tmp_path):
    """提交 100 个任务，同时在飞的线程数不得超过配置值。"""
    config = AppConfig(chunk_size=8, max_retries=0, max_download_workers=4)

    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def on_chunk(i):
        with lock:
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.001)

    class SlowSession(FakeSession):
        def get(self, url, **kwargs):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            try:
                return FakeResponse(200, [b"x" * 8] * 3, on_chunk=on_chunk)
            finally:
                pass

    session = SlowSession()
    manager = make_manager(session, config=config)

    original = manager.download_file

    def counted(url, save_path, state=None):
        try:
            return original(url, save_path, state)
        finally:
            with lock:
                live["now"] -= 1

    manager.download_file = counted

    futures = [manager.submit(URL, str(tmp_path / ("书%d.pdf" % i))) for i in range(100)]
    for f in futures:
        f.result(timeout=60)

    assert live["peak"] <= config.max_download_workers, live["peak"]
    assert len(manager.states()) == 100
    assert manager.in_flight() == 0
    manager.cancel_all()


def test_cancel_stops_the_chunk_loop(tmp_path):
    save_path = str(tmp_path / "书.pdf")
    manager = make_manager(FakeSession(default=FakeResponse(200, [b"x" * 8] * 100)),
                           config=AppConfig(chunk_size=8, max_retries=0))
    manager._cancelled.set()
    manager.download_file(URL, save_path)

    assert os.listdir(tmp_path) == []
    assert manager.states()[0]["failed_reason"]


# ---- B1 / B2：状态与回调 ----

def test_snapshot_is_plain_immutable_data(tmp_path):
    """快照只含纯数据，界面读它就够了。"""
    import dataclasses

    manager = make_manager(FakeSession(default=FakeResponse(200, [b"ab", b"cd"])))
    manager.download_file(URL, str(tmp_path / "a.pdf"))

    snapshot = manager.snapshot()
    assert dataclasses.is_dataclass(snapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.total = 99

    assert snapshot.total == 1 and snapshot.finished == 1 and snapshot.in_flight == 0
    assert snapshot.downloaded_size == 4 and snapshot.total_size == 4
    assert snapshot.percent == 100.0
    assert snapshot.all_finished is True
    assert snapshot.failures == ()
    assert "1/1" in snapshot.progress_text()


def test_empty_snapshot_reads_as_idle():
    manager = make_manager(FakeSession(default=FakeResponse(200, [b"x"])))
    snapshot = manager.snapshot()
    assert snapshot.total == 0 and snapshot.all_finished is False
    assert snapshot.progress_text() == "等待下载"


def test_worker_thread_never_touches_the_ui(tmp_path):
    """设计点名的哨兵：把「被碰到即失败」的对象放在工作线程够得着的地方。

    哨兵必须真的接在调用路径上——挂一个没人会碰的属性只是装饰。
    这里把每个界面钩子名都换成哨兵：工作线程一旦取用或调用，立刻炸。
    """
    manager = make_manager(FakeSession(default=FakeResponse(200, [b"x" * 8] * 4)))

    # 管理器上不许存在任何可被工作线程调用的界面钩子
    for name in ("on_progress", "on_finish"):
        assert not hasattr(manager, name), "DownloadManager 仍持有界面回调 %s" % name

    touched = []

    class Sentinel:
        def __getattr__(self, name):
            touched.append(name)
            raise AssertionError("工作线程碰了界面对象：%s" % name)

        def __call__(self, *args, **kwargs):
            # 必须显式定义：obj() 走类型上的 __call__，不经过 __getattr__，
            # 少了它，「工作线程直接调用界面回调」这一格就漏掉了
            touched.append("__call__")
            raise AssertionError("工作线程调用了界面回调")

    for name in ("on_progress", "on_finish", "root", "progress_bar",
                 "progress_label", "download_btn", "update_progress", "finish_downloads"):
        setattr(manager, name, Sentinel())

    manager.download_file(URL, str(tmp_path / "a.pdf"))

    assert touched == [], "工作线程碰了界面钩子：%s" % touched
    state = manager.states()[0]
    assert state["finished"] and state["failed_reason"] is None, state
    assert manager.snapshot().finished == 1


def test_snapshot_is_not_complete_until_every_task_is_registered(tmp_path):
    """P0-1 的回归门。

    真实时序是「解析一条链接（一次网络往返）-> 投递」，第一个任务完全来得及
    在第二条链接解析完之前就跑完。此时快照绝不能报「全部完成」。
    """
    gate = threading.Event()
    started = threading.Event()

    class GatedSession(FakeSession):
        def __init__(self):
            super().__init__()
            self.n = 0
            self.guard = threading.Lock()

        def get(self, url, **kwargs):
            with self.guard:
                self.n += 1
                index = self.n
            if index > 1: # 第一个任务立刻完成，其余卡住
                started.set()
                gate.wait(timeout=10)
            return FakeResponse(200, [b"x" * 8])

    config = AppConfig(chunk_size=8, max_retries=0, max_download_workers=2)
    manager = make_manager(GatedSession(), config=config)

    manager.submit(URL, str(tmp_path / "书0.pdf"))
    deadline = time.monotonic() + 5
    while manager.snapshot().finished < 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    # 第一个已经跑完，但这一批还没提交完
    assert manager.snapshot().finished == 1
    assert manager.snapshot().all_finished is True, "单个任务的快照本就该是完成的"

    for i in range(1, 4):
        manager.submit(URL, str(tmp_path / ("书%d.pdf" % i)))

    snapshot = manager.snapshot()
    assert snapshot.total == 4, "提交处没有登记任务：%s" % snapshot
    assert snapshot.in_flight == 3
    assert snapshot.all_finished is False, "只登记了一部分任务就报全部完成"

    assert started.wait(timeout=5)
    gate.set()
    deadline = time.monotonic() + 10
    while manager.snapshot().in_flight and time.monotonic() < deadline:
        time.sleep(0.01)

    final = manager.snapshot()
    assert final.total == 4 and final.finished == 4 and final.all_finished is True
    manager.cancel_all()


def test_state_is_registered_synchronously_by_submit(tmp_path):
    """submit 返回时状态必须已经登记，哪怕工作线程还没被调度。"""
    gate = threading.Event()

    class BlockedSession(FakeSession):
        def get(self, url, **kwargs):
            gate.wait(timeout=10)
            return FakeResponse(200, [b"x"])

    manager = make_manager(BlockedSession(), config=AppConfig(chunk_size=8, max_retries=0,
                                                             max_download_workers=1))
    for i in range(3):
        manager.submit(URL, str(tmp_path / ("书%d.pdf" % i)))

    snapshot = manager.snapshot()
    assert snapshot.total == 3, "submit 没有同步登记状态"
    assert snapshot.in_flight == 3
    assert snapshot.all_finished is False

    gate.set()
    manager.cancel_all()


def test_failures_are_carried_in_the_snapshot(tmp_path):
    manager = make_manager(FakeSession(default=FakeResponse(404)),
                           config=AppConfig(chunk_size=8, max_retries=0))
    manager.download_file(URL, str(tmp_path / "书.pdf"))

    snapshot = manager.snapshot()
    assert len(snapshot.failures) == 1
    url, reason = snapshot.failures[0]
    assert url == URL and "404" in reason
    assert "404" in snapshot.failure_detail()


def test_reset_refuses_while_tasks_are_in_flight(tmp_path):
    manager = make_manager(FakeSession(default=FakeResponse(200, [b"x"])))
    manager._states.append(new_download_state(URL, "x"))
    assert manager.reset() is False
    assert len(manager.states()) == 1

    manager._states[0]["finished"] = True
    assert manager.reset() is True
    assert manager.states() == []


def test_build_save_path_sanitises_and_dedupes(tmp_path):
    from tchmaterial_parser.core.downloader import build_save_path
    title = "义务教育教科书/英语三年级下册"
    first = build_save_path(str(tmp_path), title)
    second = build_save_path(str(tmp_path), title)
    assert os.path.basename(first) == "义务教育教科书_英语三年级下册.pdf"
    assert os.path.basename(second) == "义务教育教科书_英语三年级下册 (2).pdf"


# ---- P0-2：续传校验必须严格 ----

@pytest.mark.parametrize("first_headers, resume_headers, label", [
    ({"ETag": "v1"}, {}, "206 不带任何校验子"),
    # 值也不同，走的是值检查那一半
    ({"ETag": "v1"}, {"Last-Modified": "Sun, 18 May 2025 12:00:00 GMT"},
     "206 只带另一种校验子且值也不同"),
    # 值相同但头不同：只有类型检查拦得住它。
    # 不能拿 Content-Length 当「不同类型」——它已不在 VALIDATOR_HEADERS 里，
    # response_validator 对它返回 None，走的还是第一格的 current is None 分支
    ({"ETag": "same-token"}, {"Last-Modified": "same-token"},
     "206 换了一种校验子却给了相同的值"),
])
def test_resume_without_matching_validator_restarts(tmp_path, monkeypatch,
                                                    first_headers, resume_headers, label):
    """服务端接受 Range 却忽略 If-Range 时，必须丢弃 .part 从零重下。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    first = FakeResponse(200, [b"OLDOLDOL", b"DXXX"], boom_after=1,
                         headers=dict(first_headers, **{"Content-Length": "12"}))
    stale = FakeResponse(206, [b"NEWTAIL!"])
    stale.headers.clear()
    stale.headers.update(resume_headers)
    fresh = FakeResponse(200, [b"NEWNEWNE", b"WFULL!!!"],
                         headers={"ETag": "v2", "Content-Length": "16"})

    session = ScriptedSession([first, stale, fresh])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    content = open(save_path, "rb").read()
    assert b"OLD" not in content, "%s：拼出了旧文件前缀 + 新文件后缀" % label
    assert content == b"NEWNEWNEWFULL!!!", content
    assert stale.closed is True, "%s：没有关掉那个不可信的续传响应" % label
    assert session.calls[2][1] == {}, "%s：重下时仍带着 Range" % label


def test_416_restarts_from_scratch(tmp_path, monkeypatch):
    """.part 比远端还长时服务端回 416：必须整份重下，而不是反复撞 416。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    first = FakeResponse(200, [b"AAAA", b"BBBB"], boom_after=1,
                         headers={"ETag": "v1", "Content-Length": "8"})
    too_long = FakeResponse(416, headers={"ETag": "v1"})
    fresh = FakeResponse(200, [b"CCCC"], headers={"ETag": "v1", "Content-Length": "4"})

    session = ScriptedSession([first, too_long, fresh])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    assert open(save_path, "rb").read() == b"CCCC"
    assert manager.states()[0]["failed_reason"] is None
    assert session.calls[2][1] == {}, "416 之后仍带着 Range"
    assert too_long.closed is True


def test_content_length_alone_is_not_a_validator():
    """206 的 Content-Length 是剩余长度，不能拿来当 If-Range。"""
    from tchmaterial_parser.core.downloader import VALIDATOR_HEADERS, response_validator

    assert "Content-Length" not in VALIDATOR_HEADERS
    assert response_validator(FakeResponse(200, [b"abcd"])) is None
    assert response_validator(FakeResponse(200, [b"abcd"], headers={"ETag": "v1"})) == ("ETag", "v1")


# ---- P1-4：只重试真正的网络类失败 ----

@pytest.mark.parametrize("code", [400, 404, 410, 451])
def test_client_errors_are_not_retried(tmp_path, code):
    """404 这类答案重试三次也还是同一个，不该白等 1+2+4 秒。"""
    session = ScriptedSession([FakeResponse(code)] * 8)
    manager = make_manager(session) # 默认 max_retries=3
    manager.download_file(URL, str(tmp_path / "书.pdf"))

    assert len(session.calls) == 1, "%d 被重试了 %d 次" % (code, len(session.calls) - 1)
    assert manager.states()[0]["attempts"] == 1
    assert str(code) in manager.states()[0]["failed_reason"]


@pytest.mark.parametrize("code", [408, 429, 500, 503])
def test_transient_server_errors_are_retried(tmp_path, monkeypatch, code):
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    session = ScriptedSession([FakeResponse(code), FakeResponse(code), FakeResponse(200, [b"ok"])])
    manager = make_manager(session)
    manager.download_file(URL, str(tmp_path / "书.pdf"))

    assert len(session.calls) == 3
    assert manager.states()[0]["failed_reason"] is None


def test_local_disk_errors_are_not_retried(tmp_path, monkeypatch):
    """写盘失败（磁盘满、权限）不是网络抖动，重试没有意义。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    session = ScriptedSession([FakeResponse(200, [b"abcd"])] * 8)
    manager = make_manager(session)

    import builtins
    real_open = builtins.open

    def failing_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(".part"):
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing_open)
    manager.download_file(URL, str(tmp_path / "书.pdf"))
    monkeypatch.undo()

    assert len(session.calls) == 1, "磁盘错误被当成网络抖动重试了"
    assert "No space left" in manager.states()[0]["failed_reason"]


# ---- P1-7：取消拦得住重试与退避 ----

def test_cancel_during_backoff_returns_immediately(tmp_path, monkeypatch):
    """关窗时不该陪着退避把 1+2+4 秒等完。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (30.0, 30.0, 30.0))
    session = ScriptedSession([FakeResponse(500)] * 8)
    manager = make_manager(session)

    def cancel_soon():
        time.sleep(0.2)
        manager.cancel_all()

    threading.Thread(target=cancel_soon, daemon=True).start()

    started = time.monotonic()
    manager.download_file(URL, str(tmp_path / "书.pdf"))
    elapsed = time.monotonic() - started

    assert elapsed < 5, "退避期间没有响应取消，等了 %.1f 秒" % elapsed
    assert manager.states()[0]["failed_reason"] == "下载已取消"


def test_cancel_stops_further_requests(tmp_path, monkeypatch):
    """取消之后不该再发新的请求。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    session = ScriptedSession([FakeResponse(500)] * 8)
    manager = make_manager(session)
    manager.cancel_all()

    manager.download_file(URL, str(tmp_path / "书.pdf"))
    assert session.calls == [], "取消后仍然发出了请求"
    assert manager.states()[0]["failed_reason"] == "下载已取消"


def test_cancel_between_the_two_requests_of_one_attempt(tmp_path, monkeypatch):
    """一次尝试可能发两个请求：续传被判不可信之后还要整份重下。

    取消若在这两者之间到达，第二个请求不该再发出去——只在重试循环顶上
    检查是拦不住它的。
    """
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    first = FakeResponse(200, [b"AAAA", b"BBBB"], boom_after=1,
                         headers={"ETag": "v1", "Content-Length": "8"})
    stale = FakeResponse(206, [b"CCCC"], headers={"ETag": "v2", "Content-Length": "4"})

    session = ScriptedSession([first, stale])
    manager = make_manager(session)

    original = manager._stream
    calls = {"n": 0}

    def stream_then_cancel(url, headers=None):
        calls["n"] += 1
        response = original(url, headers=headers)
        if calls["n"] == 2: # 刚拿到那个不可信的 206，此刻关窗
            manager._cancelled.set()
        return response

    manager._stream = stream_then_cancel
    manager.download_file(URL, save_path)

    assert len(session.calls) == 2, "取消之后仍然发出了整份重下的请求"
    assert manager.states()[0]["failed_reason"] == "下载已取消"
    assert not os.path.exists(save_path)


def test_suggested_name_does_not_reserve_a_path(tmp_path):
    """单链接保存对话框的建议名只做清洗，不占预留（R1 P1-3）。

    用 build_save_path 取建议名会在当前工作目录登记一条永不归还的预留，
    用户取消或改名之后，下次下载同一本书的建议名就变成了 xxx (2).pdf。
    """
    import ast
    import os as _os

    from tchmaterial_parser.core.naming import sanitize_filename

    src_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             "src", "tchmaterial_parser", "ui", "app.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "download")

    dialog = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and "asksaveasfilename" in ast.unparse(n.func)]
    assert dialog, "找不到保存对话框调用"
    initialfile = [k for k in dialog[0].keywords if k.arg == "initialfile"]
    assert initialfile, "保存对话框没有给建议名"

    expression = ast.unparse(initialfile[0].value)
    assert "build_save_path" not in expression, expression
    assert "sanitize_filename" in expression, expression

    # 连取两次建议名不该产生序号，也不该登记任何预留
    title = "义务教育教科书/数学一年级上册"
    before = naming.reserved_paths()
    assert sanitize_filename(title) == sanitize_filename(title)
    assert naming.reserved_paths() == before, "取建议名登记了预留"


# ---- R2-P1-1：跨重试的校验子不许留成陈旧值 ----

def test_stale_validator_is_cleared_on_a_full_restart(tmp_path, monkeypatch):
    """整份重下且这一轮没有校验子时，必须把记着的校验子清掉。

    否则 .part 里换成了这一轮的字节，而记着的还是上一轮的 ETag，下次续传
    三项检查全过，直接拼出一个「看起来成功」的损坏文件。
    """
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    no_validator = FakeResponse(200, [b"CCCC", b"XXXX"], boom_after=1)
    no_validator.headers.clear()

    session = ScriptedSession([
        FakeResponse(200, [b"AAAA", b"XXXX"], boom_after=1,
                     headers={"ETag": "v1", "Content-Length": "8"}),
        FakeResponse(206, [b"ZZZZ"], headers={"ETag": "v2", "Content-Length": "4"}),
        no_validator,
        FakeResponse(200, [b"FINAL!!!"], headers={"Content-Length": "8"}),
    ])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    content = open(save_path, "rb").read()
    assert b"CCCC" not in content or content == b"CCCC", "拼出了两份响应的组合：%r" % content
    assert content == b"FINAL!!!", content
    # 第 4 个请求不该再带那个陈旧的 v1
    assert session.calls[3][1] == {}, "拿陈旧的校验子去续传了：%r" % (session.calls[3][1],)


# ---- R2-P1-3：非 206 先分类 ----

@pytest.mark.parametrize("code", [404, 410, 451])
def test_client_error_during_resume_is_classified_not_reissued(tmp_path, monkeypatch, code):
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    session = ScriptedSession([
        FakeResponse(200, [b"AAAA", b"XXXX"], boom_after=1,
                     headers={"ETag": "v1", "Content-Length": "8"}),
        FakeResponse(code),
    ])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    # 首次 + 一次续传，就该停下：错误响应要走永久失败分类，而不是再发一个整份请求
    assert len(session.calls) == 2, "续传路径上的 %d 被吞掉后重发了：%s" % (code, session.calls)
    assert str(code) in manager.states()[0]["failed_reason"]
    assert os.listdir(tmp_path) == []


def test_server_falling_back_to_200_on_a_range_request_is_consumed(tmp_path, monkeypatch):
    """设计点名的用例：服务端对 Range 回 200 时从头重写，而不是关掉再下一遍。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    first = FakeResponse(200, [b"AAAA", b"XXXX"], boom_after=1,
                         headers={"ETag": "v1", "Content-Length": "8"})
    full_again = FakeResponse(200, [b"NEWNEWNE"], headers={"ETag": "v2", "Content-Length": "8"})

    session = ScriptedSession([first, full_again])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    assert len(session.calls) == 2, "把一个合法的 200 关掉又重下了一遍：%s" % session.calls
    assert session.calls[1][1]["Range"] == "bytes=4-"
    assert open(save_path, "rb").read() == b"NEWNEWNE", "没有从头重写"
    # 「没被关掉再重下一遍」由上面的请求次数断言保证；读完之后它当然要关
    assert full_again.closed is True
    assert manager.states()[0]["failed_reason"] is None


# ---- R2-P2-4：206 的 Content-Range 起点 ----

def test_206_with_wrong_content_range_restarts(tmp_path, monkeypatch):
    """服务端回 206 却给了 bytes 0-，接着 append 会拼出带重复前缀的文件。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    liar = FakeResponse(206, [b"AAAABBBB"],
                        headers={"ETag": "v1", "Content-Length": "8",
                                 "Content-Range": "bytes 0-7/8"})
    session = ScriptedSession([
        FakeResponse(200, [b"AAAA", b"XXXX"], boom_after=1,
                     headers={"ETag": "v1", "Content-Length": "8"}),
        liar,
        FakeResponse(200, [b"AAAABBBB"], headers={"ETag": "v1", "Content-Length": "8"}),
    ])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    assert open(save_path, "rb").read() == b"AAAABBBB"
    assert liar.closed is True, "没有关掉那个起点不符的响应"
    assert session.calls[2][1] == {}


def test_206_with_matching_content_range_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    session = ScriptedSession([
        FakeResponse(200, [b"AAAA", b"XXXX"], boom_after=1,
                     headers={"ETag": "v1", "Content-Length": "8"}),
        FakeResponse(206, [b"BBBB"],
                     headers={"ETag": "v1", "Content-Length": "4",
                              "Content-Range": "bytes 4-7/8"}),
    ])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    assert open(save_path, "rb").read() == b"AAAABBBB"
    assert manager.states()[0]["failed_reason"] is None


# ---- R2-P2-3：抛错前关闭响应 ----

@pytest.mark.parametrize("code", [401, 404, 500])
def test_error_responses_are_closed(tmp_path, code):
    """stream=True 的响应不关掉，连接要等 GC 才归还。"""
    bad = FakeResponse(code)
    session = ScriptedSession([bad] + [FakeResponse(code) for _ in range(8)])
    manager = make_manager(session, config=AppConfig(chunk_size=8, max_retries=0))
    manager.download_file(URL, str(tmp_path / "书.pdf"))

    assert bad.closed is True, "%d 的响应没有被关闭" % code


# ---- R2-P2-5：没有 Content-Length 时进度不掉回 0 ----

def test_progress_survives_a_missing_content_length(tmp_path):
    response = FakeResponse(200, [b"abcd", b"efgh"])
    response.headers.clear() # 服务端不给 Content-Length

    manager = make_manager(FakeSession(default=response))
    manager.download_file(URL, str(tmp_path / "书.pdf"))

    snapshot = manager.snapshot()
    assert snapshot.total_size == 8, snapshot
    assert snapshot.downloaded_size == 8, snapshot
    assert snapshot.percent == 100.0
    assert "0.0 字节/0.0 字节" not in snapshot.progress_text()


# ---- R3-P0-1：不写盘的响应不得动 .part 的身份 ----

def test_transient_error_does_not_destroy_the_resume_state(tmp_path, monkeypatch):
    """一个瞬时 503 不该让已落盘的字节作废。

    错误响应压根不写盘，却曾经参与设置校验子——于是重试时续传头整个丢掉，
    40 MB 下到一半的教材要从零重来。
    """
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    blip = FakeResponse(503)
    blip.headers.clear() # 错误页不带 ETag

    session = ScriptedSession([
        FakeResponse(200, [b"AAAAAAAA", b"XXXXXXXX"], boom_after=1,
                     headers={"ETag": "v1", "Content-Length": "16"}),
        blip,
        FakeResponse(206, [b"BBBBBBBB"],
                     headers={"ETag": "v1", "Content-Length": "8",
                              "Content-Range": "bytes 8-15/16"}),
    ])
    manager = make_manager(session)
    manager.download_file(URL, save_path)

    third = session.calls[2][1]
    assert third.get("Range") == "bytes=8-", "503 之后续传头丢了：%r" % third
    assert third.get("If-Range") == "v1", "503 之后 If-Range 丢了：%r" % third
    assert open(save_path, "rb").read() == b"AAAAAAAABBBBBBBB"
    assert manager.states()[0]["failed_reason"] is None


def test_error_page_etag_does_not_poison_the_validator(tmp_path, monkeypatch):
    """错误页自带 ETag 时，不得把它当成 .part 里字节的身份。

    否则下一轮 If-Range 用的是错误页的 ETag，远端新版本恰好同值就会通过校验，
    把新版本的后半段接在旧版本前缀上。
    """
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    session = ScriptedSession([
        FakeResponse(200, [b"OLDX", b"XXXX"], boom_after=1,
                     headers={"ETag": "v1", "Content-Length": "8"}),
        FakeResponse(502, headers={"ETag": "v2"}), # 错误页带着新版本的 ETag
        FakeResponse(206, [b"NEW2"],
                     headers={"ETag": "v2", "Content-Length": "4",
                              "Content-Range": "bytes 4-7/8"}),
        FakeResponse(200, [b"NEW1", b"NEW2"], headers={"ETag": "v2", "Content-Length": "8"}),
    ])
    manager = make_manager(session, config=AppConfig(chunk_size=4, max_retries=3))
    manager.download_file(URL, save_path)

    assert session.calls[2][1].get("If-Range") == "v1", \
        "错误页的 ETag 污染了校验子：%r" % (session.calls[2][1],)

    content = open(save_path, "rb").read()
    assert content != b"OLDXNEW2", "拼出了「旧版本前缀 + 新版本后半段」"
    assert content == b"NEW1NEW2", content
    assert manager.states()[0]["failed_reason"] is None


def test_part_file_binds_bytes_and_identity(tmp_path):
    """PartFile 的不变量：校验子描述的永远是文件里此刻那些字节。"""
    from tchmaterial_parser.core.downloader import PartFile

    part = PartFile(str(tmp_path / "x.part"))
    assert part.validator is None and part.size == 0

    with part.open_fresh(("ETag", "v1")) as f:
        f.write(b"AAAA")
    assert part.validator == ("ETag", "v1")
    assert part.size == 4

    # 续写不改身份：文件里的字节仍属于同一份资源
    with part.open_append() as f:
        f.write(b"BBBB")
    assert part.validator == ("ETag", "v1")
    assert part.size == 8

    # 换一份内容，身份必须跟着换
    with part.open_fresh(None) as f:
        f.write(b"CC")
    assert part.validator is None
    assert part.size == 2

    # 丢弃残件，身份随之作废
    part.discard()
    assert part.validator is None and part.size == 0


def test_open_fresh_leaves_no_identity_when_the_file_cannot_be_created(tmp_path):
    """建不出文件就不该留下身份：磁盘满、无写权限时 open() 会抛。

    否则这个对象短暂地描述着一个并不存在的文件——修法的全部价值就在于
    身份与字节一体，类内部不该先破一次再指望外面兜底。
    """
    from tchmaterial_parser.core.downloader import PartFile

    part = PartFile(str(tmp_path / "没有这个目录" / "x.part"))
    with pytest.raises(OSError):
        part.open_fresh(("ETag", "v1"))

    assert part.validator is None, "文件没建出来，身份却留下了：%r" % (part.validator,)


def test_part_file_promote_clears_identity(tmp_path):
    from tchmaterial_parser.core.downloader import PartFile

    target = str(tmp_path / "书.pdf")
    part = PartFile(target + ".part")
    with part.open_fresh(("ETag", "v1")) as f:
        f.write(b"DONE")

    part.promote(target)
    assert open(target, "rb").read() == b"DONE"
    assert part.validator is None
    assert not os.path.exists(part.path)


def _writes_to_validator(node) -> bool:
    """这个节点是否在给某个对象的 validator 赋值。

    要认全赋值的各种语法形态，漏一种这条守卫就形同虚设：属性赋值只是最常见
    的一种，setattr、带注解的赋值、绕过 __setattr__ 直写 __dict__ 同样能改。
    """
    import ast

    targets = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]

    for target in targets:
        if isinstance(target, ast.Attribute) and target.attr == "validator":
            return True
        # part.__dict__["validator"] = x / vars(part)["validator"] = x
        if (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                and target.slice.value == "validator"):
            return True

    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else \
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name in ("setattr", "__setattr__") and len(node.args) >= 2:
            key = node.args[1]
            if isinstance(key, ast.Constant) and key.value == "validator":
                return True
    return False


def test_validator_is_only_assigned_inside_partfile():
    """静态守卫：源码里不存在 PartFile 之外给 validator 赋值的写法。

    赋值点一旦散出去，「记着的身份」与「文件里的字节」就能各自变化，续传随时
    可能把两份不同版本拼在一起。守住「赋值点全在 PartFile 内部」这条，就不必
    逐个分支去检查有没有漏掉同步。

    这条只管语法形态，不能替代行为回归：错误响应污染校验子的那两条端到端用例
    仍然是主要防线，别因为有了这条就把它们删掉。
    """
    import ast
    import glob

    src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "src", "tchmaterial_parser")
    offenders = []
    for path in sorted(glob.glob(os.path.join(src_dir, "**", "*.py"), recursive=True)):
        tree = ast.parse(open(path, encoding="utf-8").read())
        part_cls = next((n for n in ast.walk(tree)
                         if isinstance(n, ast.ClassDef) and n.name == "PartFile"), None)
        inside = range(part_cls.lineno, part_cls.end_lineno + 1) if part_cls else ()
        for node in ast.walk(tree):
            if _writes_to_validator(node) and node.lineno not in inside:
                offenders.append("%s:%d" % (os.path.basename(path), node.lineno))

    assert not offenders, "PartFile 之外有人在改 validator：%s" % offenders


# ---- R3-P2-1：Content-Range 的可信度判定 ----

def test_a_206_without_content_range_is_not_trusted(tmp_path, monkeypatch):
    """缺 Content-Range 时整份重下，而不是按可信处理。

    相同的校验子只说明资源版本没变，证明不了正文是从我们请求的偏移开始的。
    """
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    session = ScriptedSession([
        FakeResponse(200, [b"AAAA", b"XXXX"], boom_after=1,
                     headers={"ETag": "v1", "Content-Length": "8"}),
        # 服务端回 206、校验子也对，但没说这段正文从哪开始
        FakeResponse(206, [b"WHOKNOWS"], headers={"ETag": "v1", "Content-Length": "8"}),
        FakeResponse(200, [b"AAAABBBB"], headers={"ETag": "v1", "Content-Length": "8"}),
    ])
    manager = make_manager(session, config=AppConfig(chunk_size=4, max_retries=3))
    manager.download_file(URL, save_path)

    assert len(session.calls) == 3, "没有整份重下：%s" % (session.calls,)
    assert session.calls[2][1] == {}, "重下这一次不该再带续传头：%r" % (session.calls[2][1],)
    assert open(save_path, "rb").read() == b"AAAABBBB"


@pytest.mark.parametrize("raw", ["bytes 4-7/8", "Bytes 4-7/8", "BYTES 4-7/8", " bytes 4-7/8"])
def test_content_range_unit_is_case_insensitive(raw):
    """RFC 9110 §14.4：range-unit 大小写不敏感，别把合法的 206 判成不可信。"""
    assert content_range_starts_at(FakeResponse(206, headers={"Content-Range": raw}), 4)


@pytest.mark.parametrize("raw", [None, "", "items 4-7/8", "bytes */8", "4-7/8"])
def test_content_range_that_proves_nothing_is_rejected(raw):
    headers = {} if raw is None else {"Content-Range": raw}
    response = FakeResponse(206, headers=headers)
    response.headers.pop("Content-Length", None)
    assert content_range_starts_at(response, 4) is False


# ---- R3-P2-2：每一条出路都要关掉响应 ----

def test_the_response_is_closed_on_every_exit(tmp_path, monkeypatch):
    """成功、中途断流、错误状态码——三条出路都要归还连接。

    stream=True 的响应不关掉，连接要等 GC 才归还；中途断流恰恰是这条路径上
    最常见的一种，续传就是为它存在的。
    """
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    broken = FakeResponse(200, [b"AAAA", b"XXXX"], boom_after=1,
                          headers={"ETag": "v1", "Content-Length": "8"})
    server_error = FakeResponse(503)
    done = FakeResponse(200, [b"AAAABBBB"], headers={"ETag": "v1", "Content-Length": "8"})

    session = ScriptedSession([broken, server_error, done])
    manager = make_manager(session, config=AppConfig(chunk_size=4, max_retries=3))
    manager.download_file(URL, save_path)

    assert broken.closed is True, "中途断流的响应没关"
    assert server_error.closed is True, "错误状态码的响应没关"
    assert done.closed is True, "正常读完的响应没关"


def test_the_response_is_closed_when_the_download_is_cancelled(tmp_path):
    """关窗取消时同样要关：否则每关一次窗就漏一条连接。"""
    save_path = str(tmp_path / "书.pdf")
    manager = make_manager(FakeSession())

    response = FakeResponse(200, [b"AAAA", b"BBBB"], headers={"Content-Length": "8"},
                            on_chunk=lambda i: manager.cancel_all())
    manager.client.session.default = response
    manager.download_file(URL, save_path)

    assert manager.states()[0]["failed_reason"] == "下载已取消"
    assert response.closed is True, "取消时响应没关"


# ---- R3-P2-5：替身的状态字典不许比真实的少一个键 ----

def test_submit_registers_exactly_the_documented_state(tmp_path):
    """真实投递登记的键集就是 new_download_state 给的那一份。"""
    manager = make_manager(FakeSession(default=FakeResponse(200, [b"x"])))
    manager.submit(URL, str(tmp_path / "书.pdf")).result()

    assert set(manager.states()[0]) == set(new_download_state(URL, "x"))


def test_no_test_hand_builds_a_download_state():
    """测试里不许再手拼下载状态字典。

    替身比真实对象少一个字段，是本轮反复踩到的那个坑：哪天 poll_downloads
    读到 attempts，就是一个只在测试里不存在的 KeyError，而套件是绿的。
    """
    import ast
    import glob

    tests_dir = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for path in sorted(glob.glob(os.path.join(tests_dir, "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "dict":
                # dict(download_url=..., save_path=...) 与字面量等价，一并拦下
                keys = {kw.arg for kw in node.keywords}
            else:
                continue
            if "download_url" in keys and "save_path" in keys:
                offenders.append("%s:%d" % (os.path.basename(path), node.lineno))

    assert not offenders, "手拼的下载状态字典：%s（改用 new_download_state）" % offenders
