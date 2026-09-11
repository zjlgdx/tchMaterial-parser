# -*- coding: utf-8 -*-
"""下载：.part 临时文件、原子改名、失败清残件（C3），以及状态锁（B1/B2）。"""

import os
import threading

import pytest

from conftest import FakeResponse, FakeSession
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import naming
from tchmaterial_parser.core.downloader import DownloadManager

URL = "http://example.invalid/a.pdf"


def make_manager(response, **kwargs):
    from tchmaterial_parser.core.http import HttpClient
    client = HttpClient(config=AppConfig(chunk_size=8), session=FakeSession(default=response))
    return DownloadManager(client, config=AppConfig(chunk_size=8), **kwargs)


def test_success_leaves_only_the_final_file(tmp_path):
    seen = []
    chunks = [b"%PDF-1.4 ", b"body-bytes ", b"%%EOF"]
    save_path = str(tmp_path / "数学.pdf")

    response = FakeResponse(200, chunks, on_chunk=lambda i: seen.append(sorted(os.listdir(tmp_path))))
    manager = make_manager(response)
    manager.download_file(URL, save_path)

    # 写入期间只能看到 .part，目标文件不得提前出现
    assert all(entry == ["数学.pdf.part"] for entry in seen), seen
    assert sorted(os.listdir(tmp_path)) == ["数学.pdf"]
    assert open(save_path, "rb").read() == b"".join(chunks)

    state = manager.states()[0]
    assert state["finished"] and state["failed_reason"] is None


@pytest.mark.parametrize("code, fragment", [
    (401, "授权失败"), (403, "授权失败"),
    (404, "服务器返回状态码 404"), (500, "服务器返回状态码 500"),
])
def test_http_error_leaves_no_residue(tmp_path, code, fragment):
    save_path = str(tmp_path / "语文.pdf")
    (tmp_path / "语文.pdf.part").write_text("上一次留下的残件", encoding="utf-8")

    manager = make_manager(FakeResponse(code))
    manager.download_file(URL, save_path)

    assert os.listdir(tmp_path) == []
    state = manager.states()[0]
    assert state["finished"] and fragment in state["failed_reason"]


def test_mid_stream_failure_removes_part_file(tmp_path):
    save_path = str(tmp_path / "英语.pdf")
    manager = make_manager(FakeResponse(200, [b"aaa", b"bbb", b"ccc"], boom_after=2))
    manager.download_file(URL, save_path)

    assert os.listdir(tmp_path) == []
    state = manager.states()[0]
    assert state["finished"] and "模拟的连接中断" in state["failed_reason"]


def test_progress_callback_receives_plain_data(tmp_path):
    """工作线程只向回调交出纯数据，不触碰任何界面对象。"""
    seen = []
    manager = make_manager(FakeResponse(200, [b"ab", b"cd"]),
                           on_progress=lambda progress, text: seen.append((progress, text)))
    manager.download_file(URL, str(tmp_path / "a.pdf"))

    assert seen, "进度回调一次都没有被调用"
    for progress, text in seen:
        assert isinstance(progress, float) and isinstance(text, str)


def test_finish_callback_fires_exactly_once(tmp_path):
    """多个任务并发完成时，完成回调只能触发一次（B2）。"""
    calls = []
    manager = make_manager(FakeResponse(200, [b"x" * 8] * 4),
                           on_finish=lambda dir_path, detail: calls.append((dir_path, detail)))

    threads = []
    for i in range(6):
        save_path = str(tmp_path / ("书%d.pdf" % i))
        threads.append(threading.Thread(target=manager.download_file, args=(URL, save_path)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, "完成回调触发了 %d 次" % len(calls)
    assert manager.all_finished()
    assert manager.in_flight() == 0
    assert len(manager.states()) == 6


def test_reset_refuses_while_tasks_are_in_flight(tmp_path):
    """有任务在飞时不清空状态，否则在飞线程的进度与完成判定会丢失（B3）。"""
    manager = make_manager(FakeResponse(200, [b"x"]))
    manager._states.append({"download_url": URL, "save_path": "x", "downloaded_size": 0,
                            "total_size": 0, "finished": False, "failed_reason": None})
    assert manager.reset() is False
    assert len(manager.states()) == 1

    manager._states[0]["finished"] = True
    assert manager.reset() is True
    assert manager.states() == []


def test_build_save_path_sanitises_and_dedupes(tmp_path):
    naming.clear_reservations()
    try:
        from tchmaterial_parser.core.downloader import build_save_path
        title = "义务教育教科书/英语三年级下册"
        first = build_save_path(str(tmp_path), title)
        second = build_save_path(str(tmp_path), title)
        assert os.path.basename(first) == "义务教育教科书_英语三年级下册.pdf"
        assert os.path.basename(second) == "义务教育教科书_英语三年级下册 (2).pdf"
    finally:
        naming.clear_reservations()
