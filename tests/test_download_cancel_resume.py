from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
import os
import tempfile
import threading
import time
import unittest

from src.tchmaterial_parser.api import ResourceInfo
from src.tchmaterial_parser.ui import download_panel as panel


class BlockingChunkResponse:
    """模拟一个卡住的连接：第一块正常返回，第二块必须等 close() 被调用才会“断开”。"""

    def __init__(self) -> None:
        self.ok = True
        self.status_code = 200
        self.headers = {"Content-Length": "10"}
        self.close_event = threading.Event()
        self.closed = False

    def iter_content(self, **kwargs) -> object:
        yield b"12345"
        if not self.close_event.wait(timeout=5): # 5 秒远小于 REQUEST_TIMEOUT 的 60 秒读超时
            raise AssertionError("close() 一直没被调用，暂停退化成了挂起读线程")
        raise ConnectionError("connection closed") # 模拟主动断连后 iter_content 抛出的异常

    def close(self) -> None:
        self.closed = True
        self.close_event.set()


class FakeRangeResponse:
    """模拟 requests.Response，只暴露续传逻辑需要的 status_code/headers/close。"""

    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = headers or {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeHeaderRecordingSession:
    """按顺序返回预设响应，同时记录每次请求的 URL 与请求头，用于断言续传相关请求头。"""

    def __init__(self, responses: list[FakeRangeResponse]) -> None:
        self.responses = list(responses)
        self.requested_urls: list[str] = []
        self.requested_headers: list[dict] = []

    def get(self, url: str, **kwargs) -> FakeRangeResponse:
        self.requested_urls.append(url)
        self.requested_headers.append(kwargs.get("headers") or {})
        return self.responses[min(len(self.requested_urls) - 1, len(self.responses) - 1)]


class RequestDownloadHeadersTest(unittest.TestCase):
    """坑 3（Accept-Encoding: identity）与坑 8（镜像轮换时 If-Range 是唯一安全网）。"""

    def setUp(self) -> None:
        previous_interval = panel._MIN_REQUEST_INTERVAL
        self.addCleanup(setattr, panel, "_MIN_REQUEST_INTERVAL", previous_interval)
        panel._MIN_REQUEST_INTERVAL = 0
        previous_session = panel.session
        self.addCleanup(setattr, panel, "session", previous_session)

    def test_download_requests_always_set_identity_encoding(self) -> None:
        # 首次下载（无 Range）与续传请求（带 Range/If-Range）都必须是 identity，不分场景。
        fresh_session = FakeHeaderRecordingSession([FakeRangeResponse(200)])
        panel.session = fresh_session
        panel.request_download("https://example.com/book.pdf")
        self.assertEqual(fresh_session.requested_headers[0]["Accept-Encoding"], "identity")

        resume_session = FakeHeaderRecordingSession([FakeRangeResponse(206)])
        panel.session = resume_session
        panel.request_download("https://example.com/book.pdf", range_from=100, validator='"etag"')
        self.assertEqual(resume_session.requested_headers[0]["Accept-Encoding"], "identity")

    def test_resume_request_sets_range_and_if_range_headers(self) -> None:
        fake_session = FakeHeaderRecordingSession([FakeRangeResponse(206)])
        panel.session = fake_session

        panel.request_download("https://example.com/book.pdf", range_from=1024, validator='"etag-1"')

        headers = fake_session.requested_headers[0]
        self.assertEqual(headers["Range"], "bytes=1024-")
        self.assertEqual(headers["If-Range"], '"etag-1"')

    def test_plain_request_without_offset_carries_no_range_headers(self) -> None:
        fake_session = FakeHeaderRecordingSession([FakeRangeResponse(200)])
        panel.session = fake_session

        panel.request_download("https://example.com/book.pdf")

        headers = fake_session.requested_headers[0]
        self.assertNotIn("Range", headers)
        self.assertNotIn("If-Range", headers)

    def test_resume_across_mirror_rotation_still_sends_if_range(self) -> None:
        # r1 打不通，r2 才回 206；两次请求都必须带着同一个 If-Range，
        # 正确性交给服务端按 HTTP 语义判断，不依赖“猜哪个镜像会命中”。
        fake_session = FakeHeaderRecordingSession([FakeRangeResponse(500), FakeRangeResponse(206)])
        panel.session = fake_session
        url = "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf"

        response, attempted_urls = panel.request_download(url, range_from=2048, validator='"etag-2"')

        self.assertEqual(response.status_code, 206)
        self.assertEqual([u.split("/", 3)[2] for u in attempted_urls], [
            "r1-ndr-private.ykt.cbern.com.cn",
            "r2-ndr-private.ykt.cbern.com.cn",
        ])
        self.assertTrue(all(headers["If-Range"] == '"etag-2"' for headers in fake_session.requested_headers))
        self.assertTrue(all(headers["Range"] == "bytes=2048-" for headers in fake_session.requested_headers))


class MultiFileCountPromptTest(unittest.TestCase):
    """“下载”按钮触发的解析完成后，多文件提示要写明具体数量（issue 的“必须做到 3”）。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(panel, "download_states", []))
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn", "url_text", "bookmark_var"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.notice = self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        self.context.enter_context(patch.object(panel, "thread_it", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        previous_token = panel.config.access_token
        self.addCleanup(setattr, panel.config, "access_token", previous_token)
        panel.config.access_token = None
        panel.bookmark_var.get.return_value = False

    def test_prompt_states_the_exact_file_count(self) -> None:
        resource_by_url = {
            f"https://example.com/{index}.pdf": ResourceInfo(f"教材{index}", f"https://example.com/{index}.pdf", "pdf", [])
            for index in range(3)
        }
        panel.url_text.get.return_value = "\n".join(resource_by_url)

        with patch.object(panel, "parse", side_effect=lambda url, bookmarks: [resource_by_url[url]]):
            with patch.object(panel.filedialog, "askdirectory", return_value=""): # 直接放弃选择目录，避免真的启动下载
                panel.download()

        self.notice.assert_called_once()
        _title, message = self.notice.call_args.args
        self.assertEqual(
            message,
            "您将下载 3 个文件，请选择要下载文件的位置。本程序将在该文件夹中按教材分类创建子文件夹，并以资源名称命名文件。",
        )

    def test_single_file_skips_the_count_prompt(self) -> None:
        resource = ResourceInfo("教材", "https://example.com/only.pdf", "pdf", [])
        panel.url_text.get.return_value = resource.url

        with patch.object(panel, "parse", return_value=[resource]):
            with patch.object(panel.filedialog, "asksaveasfilename", return_value=""):
                panel.download()

        self.notice.assert_not_called()


class BatchControlTest(unittest.TestCase):
    """新批次的控制状态默认全部处于空闲/未请求，且 paused_settled 只是一个普通布尔值。"""

    def test_fresh_control_starts_idle(self) -> None:
        control = panel.BatchControl()

        self.assertFalse(control.cancel_event.is_set())
        self.assertFalse(control.pause_event.is_set())
        self.assertFalse(control.paused_settled)
        self.assertIsNone(control.directory)
        self.assertEqual(control.active_responses, {})

    def test_module_starts_without_an_active_batch(self) -> None:
        self.assertIsNone(panel._batch_control)


class CreateDownloadStateChaptersTest(unittest.TestCase):
    """chapters 挪进状态字典，续传/继续时只靠 download_states 就能重新提交任务。"""

    def test_chapters_travel_with_the_state_dict(self) -> None:
        chapters = [{"id": "chapter-1"}]
        state = panel.create_download_state("https://example.com/book.pdf", "book.pdf", chapters)
        self.assertIs(state["chapters"], chapters)

    def test_defaults_to_no_chapters(self) -> None:
        state = panel.create_download_state("https://example.com/book.pdf", "book.pdf")
        self.assertIsNone(state["chapters"])


class ChaptersTravelThroughBatchSubmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.directory = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        self.context.enter_context(patch.object(panel, "download_states", []))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        self.context.enter_context(patch.object(panel, "thread_it", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        for name in ("progress_label", "download_progress_bar", "download_btn"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        self.context.enter_context(patch.object(panel.messagebox, "showwarning"))

    def test_batch_submits_download_file_with_the_chapters_stored_on_the_state(self) -> None:
        chapters = [{"id": "c1"}]
        resource = ResourceInfo("教材", "https://example.com/book.pdf", "pdf", chapters)
        save_path = str(Path(self.directory) / "book.pdf")
        received: list[object] = []

        def fake_download_file(url: str, path: str, chapters_arg: object, state: dict) -> None:
            received.append(chapters_arg)
            state["finished"] = True

        with patch.object(panel, "download_file", fake_download_file):
            panel.start_download_batch([(resource, save_path)], self.directory)

        self.assertEqual(received, [chapters])
        self.assertIs(panel.download_states[0]["chapters"], chapters)


class PlanDownloadWriteTest(unittest.TestCase):
    """坑 1/2/4/6/7/8/9：追加/截断、downloaded_size、total_size、校验子刷新是同一个决定的输出。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.tmp_dir = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        self.temp_path = str(Path(self.tmp_dir) / "book.pdf.tmp")
        self.url = "https://example.com/book.pdf"

    def existing_state(self, offset: int, validator: str | None = '"etag-old"') -> dict:
        with open(self.temp_path, "wb") as file:
            file.write(b"x" * offset)
        state = panel.create_download_state(self.url, str(Path(self.tmp_dir) / "book.pdf"))
        state["validator"] = validator
        return state

    def test_206_total_size_comes_from_content_range_not_content_length(self) -> None:
        state = self.existing_state(offset=100)
        response = FakeRangeResponse(206, {"Content-Range": "bytes 100-4999/5000", "Content-Length": "4900"})
        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            open_mode, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(open_mode, "ab")
        self.assertIs(used_response, response)
        self.assertEqual(state["total_size"], 5000)
        self.assertEqual(state["downloaded_size"], 100)

    def test_resume_accumulates_downloaded_size_from_existing_temp_file_offset(self) -> None:
        state = self.existing_state(offset=12345)
        response = FakeRangeResponse(206, {"Content-Range": "bytes 12345-19999/20000", "Content-Length": "7655"})
        with patch.object(panel, "request_download", return_value=(response, [self.url])) as mocked:
            panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(state["downloaded_size"], 12345) # 现读的磁盘偏移，不是某个内存里的旧计数
        self.assertEqual(mocked.call_args.kwargs["range_from"], 12345)

    def test_resume_retries_from_scratch_on_416(self) -> None:
        state = self.existing_state(offset=500)
        range_invalid = FakeRangeResponse(416)
        fresh = FakeRangeResponse(200, {"Content-Length": "999", "ETag": '"etag-new"'})
        with patch.object(panel, "request_download", side_effect=[(range_invalid, [self.url]), (fresh, [self.url])]) as mocked:
            open_mode, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(range_invalid.closed) # 不信任 416 响应，主动关闭后重来
        self.assertEqual(mocked.call_args_list[1].kwargs.get("range_from"), None)
        self.assertEqual(open_mode, "wb")
        self.assertIs(used_response, fresh)
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 999)

    def test_resume_requires_content_range_start_to_match_requested_offset(self) -> None:
        state = self.existing_state(offset=1000)
        # 服务端回了 206，但起点是 0 而不是我们请求的 1000（例如被服务端 clamp），
        # 这段接不上我们本地已有的字节，不能当成可以追加续传。
        mismatched_206 = FakeRangeResponse(206, {"Content-Range": "bytes 0-4999/5000", "Content-Length": "5000"})
        with patch.object(panel, "request_download", return_value=(mismatched_206, [self.url])):
            open_mode, _response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)
        self.assertEqual(open_mode, "wb")
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 5000) # 落回按 Content-Length 处理，不使用 Content-Range 的总长

    def test_malformed_content_range_falls_back_to_full_restart(self) -> None:
        state = self.existing_state(offset=200)
        response = FakeRangeResponse(206, {"Content-Range": "not-a-content-range", "Content-Length": "42"})
        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            open_mode, _response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(open_mode, "wb")
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 42)

    def test_no_validator_never_attempts_a_range_request(self) -> None:
        state = self.existing_state(offset=800, validator=None)
        response = FakeRangeResponse(200, {"Content-Length": "800"})
        with patch.object(panel, "request_download", return_value=(response, [self.url])) as mocked:
            panel.plan_download_write(state, self.temp_path, self.url)

        # 没有校验子时按原有的“单参数”方式调用，不额外声称一次并不存在的 Range 续传
        mocked.assert_called_once_with(self.url)

    def test_failed_response_skips_content_range_and_validator_handling(self) -> None:
        # 失败响应未必带 headers；plan_download_write 不应该在这种响应上做续传相关的判断
        state = self.existing_state(offset=100)

        class FailedResponseWithoutHeaders:
            status_code = 404
            ok = False

            def close(self) -> None:
                pass

        with patch.object(panel, "request_download", return_value=(FailedResponseWithoutHeaders(), [self.url])):
            open_mode, response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertFalse(response.ok)
        self.assertEqual(open_mode, "wb") # 调用方看到 response.ok 为假就会走失败分支，这个值本身不会被用到

    def test_validator_refreshes_whenever_response_is_a_full_body(self) -> None:
        # 首次下载（无 Range）拿到 200 要刷新校验子；带 Range 但被判定失配、回落成 200 的续传请求同样要刷新。
        state = self.existing_state(offset=0, validator=None)
        os.remove(self.temp_path)
        fresh = FakeRangeResponse(200, {"Content-Length": "10", "ETag": '"fresh-etag"'})
        with patch.object(panel, "request_download", return_value=(fresh, [self.url])):
            panel.plan_download_write(state, self.temp_path, self.url)
        self.assertEqual(state["validator"], '"fresh-etag"')

        state = self.existing_state(offset=100, validator='"stale-etag"')
        fallback_200 = FakeRangeResponse(200, {"Content-Length": "999", "Last-Modified": "Tue, 01 Jan 2030 00:00:00 GMT"})
        with patch.object(panel, "request_download", return_value=(fallback_200, [self.url])):
            panel.plan_download_write(state, self.temp_path, self.url)
        self.assertEqual(state["validator"], "Tue, 01 Jan 2030 00:00:00 GMT")


class DownloadFileResumeIntegrationTest(unittest.TestCase):
    """接入 plan_download_write 之后，download_file 端到端的续传行为（坑 2、坑 7）。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.tmp_dir = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        self.url = "https://example.com/book.pdf"
        self.context.enter_context(patch.object(panel, "download_states", []))
        for name in ("progress_label", "download_progress_bar"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))

    def test_resume_fallback_to_full_restart_resets_downloaded_size_and_keeps_the_file(self) -> None:
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        temp_path = f"{save_path}.tmp"
        with open(temp_path, "wb") as file: # 上一次暂停留下的半截内容
            file.write(b"OLDOLD")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"old-etag"'

        new_content = b"HELLO WORLD" # 远端已变化，续传请求会被服务端判定失配，回落成整份新内容

        class FullBodyResponse:
            ok = True
            status_code = 200
            headers = {"Content-Length": str(len(new_content)), "ETag": '"new-etag"'}

            def iter_content(self, **kwargs) -> object:
                yield new_content

            def close(self) -> None:
                pass

        calls: list[tuple] = []

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None):
            calls.append((url, range_from, validator))
            return FullBodyResponse(), [url]

        with patch.object(panel, "request_download", side_effect=fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertEqual(calls, [(self.url, 6, '"old-etag"')]) # 确实按现读的偏移尝试过续传
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(state["downloaded_size"], len(new_content))
        self.assertEqual(state["total_size"], len(new_content))
        self.assertEqual(Path(save_path).read_bytes(), new_content) # 是新内容整份，不是旧内容+新内容拼接
        self.assertFalse(Path(temp_path).exists())
        self.assertEqual(state["validator"], '"new-etag"')

    def test_resume_completes_integrity_check_successfully(self) -> None:
        save_path = str(Path(self.tmp_dir) / "book2.pdf")
        temp_path = f"{save_path}.tmp"
        with open(temp_path, "wb") as file:
            file.write(b"HELLO ")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"etag"'

        remaining = b"WORLD" # 恰好补全成 "HELLO WORLD"

        class PartialResponse:
            ok = True
            status_code = 206
            headers = {"Content-Range": "bytes 6-10/11", "Content-Length": "5"}

            def iter_content(self, **kwargs) -> object:
                yield remaining

            def close(self) -> None:
                pass

        calls: list[tuple] = []

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None):
            calls.append((url, range_from, validator))
            return PartialResponse(), [url]

        with patch.object(panel, "request_download", side_effect=fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertEqual(calls, [(self.url, 6, '"etag"')])
        self.assertIsNone(state["failed_reason"])
        self.assertEqual(state["downloaded_size"], 11)
        self.assertEqual(state["total_size"], 11)
        self.assertEqual(Path(save_path).read_bytes(), b"HELLO WORLD")
        self.assertFalse(Path(temp_path).exists())


class CancelAndPauseInDownloadFileTest(unittest.TestCase):
    """坑 5（暂停必须断连，不能挂起读线程）、坑 9（信号量不泄漏）、坑 10 前置的 active_responses 登记/注销。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.tmp_dir = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        self.context.enter_context(patch.object(panel, "download_states", []))
        for name in ("progress_label", "download_progress_bar"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        self.url = "https://example.com/book.pdf"

    def acquire_slots_without_blocking(self, count: int = 3) -> None:
        for _ in range(count):
            self.assertTrue(panel._download_slots.acquire(timeout=0.5), "_download_slots 许可数没有恢复，泄漏了")
        for _ in range(count):
            panel._download_slots.release()

    def wait_until_first_chunk_written(self, state: dict) -> None:
        deadline = time.monotonic() + 2
        while state["downloaded_size"] < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(state["downloaded_size"], 5, "第一块都没写完，测试前置条件不成立")

    def test_pause_disconnects_instead_of_blocking_the_reader(self) -> None:
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        response = BlockingChunkResponse()

        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            worker = threading.Thread(target=panel.download_file, args=(self.url, save_path, None, state))
            started_at = time.monotonic()
            worker.start()
            self.wait_until_first_chunk_written(state)

            control.pause_event.set()
            panel.close_active_responses(control)
            worker.join(timeout=5)
            elapsed = time.monotonic() - started_at

        self.assertFalse(worker.is_alive(), "暂停没有让工作线程退出")
        self.assertLess(elapsed, 10) # 远小于 REQUEST_TIMEOUT 的 60 秒读超时
        self.assertTrue(response.closed)
        self.assertFalse(state["finished"]) # 暂停：finished 保持 False，留给“继续”
        self.assertEqual(Path(f"{save_path}.tmp").read_bytes(), b"12345") # 半截内容保留

    def test_active_responses_registered_then_unregistered_on_the_disconnect_path(self) -> None:
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        response = BlockingChunkResponse()

        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            worker = threading.Thread(target=panel.download_file, args=(self.url, save_path, None, state))
            worker.start()
            self.wait_until_first_chunk_written(state)

            self.assertEqual(list(control.active_responses.values()), [response]) # 中途确实登记过

            control.pause_event.set()
            panel.close_active_responses(control)
            worker.join(timeout=5)

        self.assertEqual(control.active_responses, {}) # finally 里一定会注销，不会残留已关闭的响应

    def test_active_responses_unregistered_when_write_raises(self) -> None:
        # 用会记录 set/pop 的字典代替 active_responses，证明确实“先登记、后注销”了一次，
        # 而不是碰巧全程没登记过、最终自然是空字典。
        class RecordingDict(dict):
            def __init__(self) -> None:
                super().__init__()
                self.events: list[tuple[str, int]] = []

            def __setitem__(self, key: int, value: object) -> None:
                self.events.append(("set", key))
                super().__setitem__(key, value)

            def pop(self, key: int, default: object = None) -> object:
                self.events.append(("pop", key))
                return super().pop(key, default)

        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        control.active_responses = RecordingDict()
        state["control"] = control

        class RaisingResponse:
            ok = True
            status_code = 200
            headers = {"Content-Length": "5"}

            def iter_content(self, **kwargs) -> object:
                raise OSError("模拟磁盘写入失败")

            def close(self) -> None:
                pass

        with patch.object(panel, "request_download", return_value=(RaisingResponse(), [self.url])):
            panel.download_file(self.url, save_path, None, state)

        registered_key = id(state)
        self.assertEqual(control.active_responses.events, [("set", registered_key), ("pop", registered_key)])
        self.assertEqual(control.active_responses, {}) # 写入抛错也要注销，不能只在“正常路径”上配对
        self.assertTrue(state["finished"])
        self.assertIsNotNone(state["failed_reason"])

    def test_cancelled_before_request_releases_the_slot_and_marks_finished(self) -> None:
        state = panel.create_download_state(self.url, str(Path(self.tmp_dir) / "a.pdf"))
        control = panel.BatchControl()
        control.cancel_event.set()
        state["control"] = control

        panel.download_file(self.url, state["save_path"], None, state)

        self.assertTrue(state["finished"])
        self.assertFalse(Path(f"{state['save_path']}.tmp").exists())
        self.acquire_slots_without_blocking()

    def test_paused_before_request_releases_the_slot_and_leaves_unfinished(self) -> None:
        state = panel.create_download_state(self.url, str(Path(self.tmp_dir) / "b.pdf"))
        control = panel.BatchControl()
        control.pause_event.set()
        state["control"] = control

        panel.download_file(self.url, state["save_path"], None, state)

        self.assertFalse(state["finished"]) # 排队中就被暂停：这个任务本身还没跑，留给“继续”
        self.acquire_slots_without_blocking()

    def test_pause_and_cancel_do_not_leak_download_slots(self) -> None:
        # 在飞中被暂停（走主动断连那条路径）之后，信号量的许可数必须完整恢复到 3
        save_path = str(Path(self.tmp_dir) / "c.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        response = BlockingChunkResponse()

        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            worker = threading.Thread(target=panel.download_file, args=(self.url, save_path, None, state))
            worker.start()
            self.wait_until_first_chunk_written(state)

            control.pause_event.set()
            panel.close_active_responses(control)
            worker.join(timeout=5)

        self.acquire_slots_without_blocking()

    def test_close_active_responses_ignores_a_response_whose_close_raises(self) -> None:
        control = panel.BatchControl()

        class ExplodingResponse:
            def close(self) -> None:
                raise RuntimeError("已经断开的连接再关一次")

        control.active_responses[1] = ExplodingResponse()
        panel.close_active_responses(control) # 不应该向上抛出

    def test_close_active_responses_does_nothing_when_empty(self) -> None:
        control = panel.BatchControl()
        panel.close_active_responses(control) # 不应该抛出


if __name__ == "__main__":
    unittest.main()
