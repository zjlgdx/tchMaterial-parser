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
from tchmaterial_parser.core.downloader import DownloadManager, response_validator
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
    second = FakeResponse(206, [b"CCCC"], headers={"ETag": "v1", "Content-Length": "4"})
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
    assert manager.peak_concurrency() <= config.max_download_workers, manager.peak_concurrency()
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
    """设计点名的哨兵：工作线程手里不该有任何一个会碰界面的回调。"""
    def sentinel(*args, **kwargs):
        raise AssertionError("工作线程调用了界面回调")

    manager = make_manager(FakeSession(default=FakeResponse(200, [b"x" * 8] * 4)))
    # 管理器上不许存在任何可被工作线程调用的界面钩子
    for name in ("on_progress", "on_finish"):
        assert not hasattr(manager, name), "DownloadManager 仍持有界面回调 %s" % name
    manager.sentinel = sentinel

    manager.download_file(URL, str(tmp_path / "a.pdf"))
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
    manager._states.append({"download_url": URL, "save_path": "x", "downloaded_size": 0,
                            "total_size": 0, "finished": False, "failed_reason": None})
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

@pytest.mark.parametrize("resume_headers, label", [
    ({}, "206 不带任何校验子"),
    ({"Content-Length": "8"}, "206 只带与首次不同类型的校验子"),
])
def test_resume_without_matching_validator_restarts(tmp_path, monkeypatch, resume_headers, label):
    """服务端接受 Range 却忽略 If-Range 时，必须丢弃 .part 从零重下。"""
    monkeypatch.setattr("tchmaterial_parser.core.downloader.RETRY_BACKOFF", (0, 0, 0))
    save_path = str(tmp_path / "书.pdf")

    first = FakeResponse(200, [b"OLDOLDOL", b"DXXX"], boom_after=1,
                         headers={"ETag": "v1", "Content-Length": "12"})
    stale = FakeResponse(206, [b"NEWTAIL!"], headers=dict(resume_headers))
    stale.headers.pop("Content-Length", None)
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
