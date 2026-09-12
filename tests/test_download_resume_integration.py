# -*- coding: utf-8 -*-
# 取消/暂停续传的端到端实测：起一个真正实现 Range/If-Range/ETag/416 语义的本地服务
# （绑 port 0，纯 loopback，不依赖外网），走 download_panel 的真实网络栈，
# 证明续传是字节级精确衔接，而不是只是长度凑巧对上。

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
import hashlib
import os
import tempfile
import threading
import time
import unittest

from _range_server import start_range_server, stop_range_server

from src.tchmaterial_parser.ui import download_panel as panel


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ResumeIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.tmp_dir = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))

        self.content = os.urandom(2 * 1024 * 1024) # 2 MB 随机内容，逐字节比对才有意义
        self.etag = f'"{sha256(self.content)}"'
        self.server, self.server_thread, self.url = start_range_server(self.content, self.etag)
        self.addCleanup(stop_range_server, self.server, self.server_thread)

        self.context.enter_context(patch.object(panel, "download_states", []))
        for name in ("progress_label", "download_progress_bar"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        previous_interval = panel._MIN_REQUEST_INTERVAL
        self.addCleanup(setattr, panel, "_MIN_REQUEST_INTERVAL", previous_interval)
        panel._MIN_REQUEST_INTERVAL = 0

    def save_path(self, name: str) -> str:
        return str(Path(self.tmp_dir) / name)

    def wait_until(self, predicate, timeout: float = 5.0, message: str = "等待超时") -> None:
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(predicate(), message)

    def pause_mid_flight(self, state: dict, control: "panel.BatchControl", target_ratio: float = 1 / 3) -> None:
        """等真的写下一部分字节（且明显没写完）之后再暂停，制造真正的“中途”，不是巧合的“恰好完成”。"""
        target = int(len(self.content) * target_ratio)
        self.wait_until(lambda: state["downloaded_size"] >= target, message="没能等到中途进度，测试前置条件不成立")
        self.assertLess(state["downloaded_size"], len(self.content), "文件已经下完了，没有制造出中途暂停")
        control.pause_event.set()
        panel.close_active_responses(control)

    def test_pause_then_resume_produces_a_byte_identical_file(self) -> None:
        save_path = self.save_path("book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control

        worker = threading.Thread(target=panel.download_file, args=(self.url, save_path, None, state))
        worker.start()
        self.pause_mid_flight(state, control)
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive(), "暂停没有让工作线程退出")
        self.assertFalse(state["finished"]) # 暂停：留给“继续”
        paused_size = state["downloaded_size"]
        self.assertGreater(paused_size, 0)
        self.assertEqual(Path(f"{save_path}.tmp").stat().st_size, paused_size) # 磁盘上的半截内容与计数器一致

        requests_before_resume = len(self.server.requests)
        control.pause_event.clear() # “继续”：resume_current_batch 会先清掉这个标志
        panel.download_file(self.url, save_path, None, state)

        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(state["downloaded_size"], len(self.content))
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        final_bytes = Path(save_path).read_bytes()
        self.assertEqual(len(final_bytes), len(self.content))
        self.assertEqual(sha256(final_bytes), sha256(self.content)) # 哈希一致
        self.assertEqual(final_bytes, self.content) # 逐字节完全相同，不只是长度/哈希对上

        # 字节一致不代表真的走了 Range 续传——退化成不带 Range 的全量重下，字节一样会一致。
        # 直接核对服务端记录到的请求头与它实际回的状态码，证明确实是从暂停偏移续传上的。
        resume_requests = self.server.requests[requests_before_resume:]
        self.assertEqual(len(resume_requests), 1)
        self.assertEqual(resume_requests[0]["range"], f"bytes={paused_size}-")
        self.assertEqual(resume_requests[0]["if_range"], self.etag)
        self.assertEqual(resume_requests[0]["status"], 206)

    def test_cancel_mid_download_deletes_the_temp_file(self) -> None:
        save_path = self.save_path("book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control

        worker = threading.Thread(target=panel.download_file, args=(self.url, save_path, None, state))
        worker.start()
        target = len(self.content) // 3
        self.wait_until(lambda: state["downloaded_size"] >= target)
        control.cancel_event.set()
        panel.close_active_responses(control)
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertTrue(state["finished"])
        self.assertIsNone(state["failed_reason"])
        self.assertEqual(state["downloaded_size"], 0)
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        self.assertFalse(Path(save_path).exists())

    def test_remote_content_changed_between_pause_and_resume_restarts_with_new_content(self) -> None:
        save_path = self.save_path("book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control

        worker = threading.Thread(target=panel.download_file, args=(self.url, save_path, None, state))
        worker.start()
        self.pause_mid_flight(state, control)
        worker.join(timeout=5)
        self.assertFalse(state["finished"])

        paused_size = state["downloaded_size"]
        old_etag = self.etag

        # 远端在暂停期间变了：换一份不同长度、不同内容的新文件和新 ETag
        new_content = os.urandom(1 * 1024 * 1024 + 12345)
        new_etag = f'"{sha256(new_content)}"'
        self.server.content = new_content
        self.server.etag = new_etag

        requests_before_resume = len(self.server.requests)
        control.pause_event.clear()
        panel.download_file(self.url, save_path, None, state) # If-Range 不匹配，服务端应该回整份新内容

        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(state["downloaded_size"], len(new_content))
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        final_bytes = Path(save_path).read_bytes()
        self.assertEqual(final_bytes, new_content) # 是全新内容整份，不是旧内容 + 新内容拼接
        self.assertEqual(state["validator"], new_etag) # 校验子跟着刷新，下次续传用得上

        # 确认客户端确实带着旧偏移/旧校验子发起过续传请求，是服务端按 If-Range 语义判定失配后
        # 主动回落成 200，而不是客户端自己放弃续传、一开始就发了个全量请求。
        resume_requests = self.server.requests[requests_before_resume:]
        self.assertEqual(len(resume_requests), 1)
        self.assertEqual(resume_requests[0]["range"], f"bytes={paused_size}-")
        self.assertEqual(resume_requests[0]["if_range"], old_etag)
        self.assertEqual(resume_requests[0]["status"], 200)

    def test_offset_beyond_shrunk_remote_content_retries_from_scratch_after_416(self) -> None:
        save_path = self.save_path("book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control

        worker = threading.Thread(target=panel.download_file, args=(self.url, save_path, None, state))
        worker.start()
        self.pause_mid_flight(state, control, target_ratio=0.6) # 暂停在超过一半的位置
        worker.join(timeout=5)
        paused_size = state["downloaded_size"]

        # 服务端把内容换短了，但校验子（ETag）保持不变——刻意制造“If-Range 能匹配，
        # 但这次的偏移本身越界”这种场景，与上一条“校验子不匹配 → 200”是两条不同的代码
        # 路径（前者是 plan_download_write 里的 416 分支，后者是 If-Range 分支），要分开验证。
        shrunk_content = os.urandom(paused_size // 2)
        self.assertLess(len(shrunk_content), paused_size)
        self.server.content = shrunk_content

        requests_before_resume = len(self.server.requests)
        control.pause_event.clear()
        panel.download_file(self.url, save_path, None, state)

        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(state["downloaded_size"], len(shrunk_content))
        final_bytes = Path(save_path).read_bytes()
        self.assertEqual(final_bytes, shrunk_content)

        # 先 416（带着旧偏移/旧校验子续传，越界），再一次不带 Range 的全新请求——不是别的顺序。
        resume_requests = self.server.requests[requests_before_resume:]
        self.assertEqual(len(resume_requests), 2)
        self.assertEqual(resume_requests[0]["range"], f"bytes={paused_size}-")
        self.assertEqual(resume_requests[0]["if_range"], self.etag)
        self.assertEqual(resume_requests[0]["status"], 416)
        self.assertIsNone(resume_requests[1]["range"]) # 第二次不带 Range，按全新下载处理
        self.assertEqual(resume_requests[1]["status"], 200)


if __name__ == "__main__":
    unittest.main()
