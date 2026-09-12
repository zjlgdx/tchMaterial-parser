from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, call, patch
import inspect
import os
import re
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


class WellBehavedStoppableResponse:
    """连续吐块、close() 不抛错的“良民”响应：证明分块循环里的协作式检查本身在起作用，
    不依赖“断连之后 iter_content 恰好抛出异常”这条路径——真实的流未必会在断连后立刻报错。"""

    def __init__(self, trigger, trigger_after_chunks: int = 3, chunk_count: int = 200, chunk_size: int = 100) -> None:
        self.ok = True
        self.status_code = 200
        self.headers = {"Content-Length": str(chunk_count * chunk_size)}
        self._trigger = trigger
        self._trigger_after_chunks = trigger_after_chunks
        self._chunk_size = chunk_size
        self._chunk_count = chunk_count
        self.closed = False

    def iter_content(self, **kwargs) -> object:
        for index in range(self._chunk_count):
            if index == self._trigger_after_chunks: # 模拟用户在下载中途点了暂停/取消
                self._trigger()
            yield b"x" * self._chunk_size

    def close(self) -> None:
        self.closed = True # 不抛错


class TriggerThenRaiseResponse:
    """吐几块正常数据后，先触发回调、再抛出异常——模拟主动断连确实让 iter_content 报错的场景，
    但报错发生在“取次一块”时，而不是紧跟在已经写入的那块后面。"""

    def __init__(self, trigger, chunk_count: int = 3, chunk_size: int = 100) -> None:
        self.ok = True
        self.status_code = 200
        self.headers = {"Content-Length": str((chunk_count + 50) * chunk_size)}
        self._trigger = trigger
        self._chunk_count = chunk_count
        self._chunk_size = chunk_size

    def iter_content(self, **kwargs) -> object:
        for _ in range(self._chunk_count):
            yield b"x" * self._chunk_size
        self._trigger()
        raise ConnectionError("connection reset")

    def close(self) -> None:
        pass


class FakeRangeResponse:
    """模拟 requests.Response：status_code/headers/close，外加可选的 body 供 iter_content 吐出。"""

    def __init__(self, status_code: int, headers: dict | None = None, body: bytes = b"") -> None:
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = headers or {}
        self.body = body
        self.closed = False

    def iter_content(self, **kwargs) -> object:
        if self.body:
            yield self.body

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
        self.context.enter_context(patch.object(panel, "_batch_control", None))
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
        self.context.enter_context(patch.object(panel, "_batch_control", None))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        self.context.enter_context(patch.object(panel, "thread_it", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn"):
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
        # P0-1：206 但起点不匹配，响应体只是那一段，不能直接当整份写下去——必须像 416 一样
        # 关掉这次响应、重新发一次不带 Range 的请求。用两个不同的总长断言最终数值确实来自
        # 那次重试的响应，而不是继续沿用第一次（不可信）响应里的总长凑巧对上。
        state = self.existing_state(offset=1000)
        mismatched_206 = FakeRangeResponse(206, {"Content-Range": "bytes 0-4999/5000", "Content-Length": "5000"})
        fresh_full_200 = FakeRangeResponse(200, {"Content-Length": "9999", "ETag": '"fresh"'})
        with patch.object(panel, "request_download", side_effect=[(mismatched_206, [self.url]), (fresh_full_200, [self.url])]) as mocked:
            open_mode, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 2)
        self.assertIsNone(mocked.call_args_list[1].kwargs.get("range_from")) # 第二次是不带 Range 的全新请求
        self.assertTrue(mismatched_206.closed) # 不可信的响应必须被关掉，不能拿它的响应体接着用
        self.assertIs(used_response, fresh_full_200)
        self.assertEqual(open_mode, "wb")
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 9999) # 来自重试后的响应，不是第一次那个 5000

    def test_malformed_content_range_falls_back_to_full_restart(self) -> None:
        # 同上，只是触发条件换成 Content-Range 解析不出来。
        state = self.existing_state(offset=200)
        unparseable_206 = FakeRangeResponse(206, {"Content-Range": "not-a-content-range", "Content-Length": "42"})
        fresh_full_200 = FakeRangeResponse(200, {"Content-Length": "777", "ETag": '"fresh"'})
        with patch.object(panel, "request_download", side_effect=[(unparseable_206, [self.url]), (fresh_full_200, [self.url])]) as mocked:
            open_mode, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(unparseable_206.closed)
        self.assertIs(used_response, fresh_full_200)
        self.assertEqual(open_mode, "wb")
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 777) # 不是第一次那个 42

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

    def test_unusable_206_is_never_written_as_if_it_were_the_full_file(self) -> None:
        # P0-1：起点不匹配的 206——响应体只是那一段，一旦被当整份写下去就是静默损坏
        # （文件存在、大小和计数器都对得上、内容却是错的）。必须断言最终文件的字节，
        # 只断言 open_mode/计数器钉不住这个 bug：旧实现落回 wb 之后计数器照样能自洽。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        temp_path = f"{save_path}.tmp"
        with open(temp_path, "wb") as file: # 半截内容：11 字节
            file.write(b"OLD-PARTIAL")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"old-etag"'

        full_content = os.urandom(5000)
        # 服务端声称这段从 2000 开始，但我们请求的是 11——起点接不上本地已有的字节
        unusable_body = full_content[2000:]

        class UnusableRangeResponse:
            ok = True
            status_code = 206
            headers = {"Content-Range": "bytes 2000-4999/5000", "Content-Length": str(len(unusable_body))}

            def __init__(self) -> None:
                self.closed = False

            def iter_content(self, **kwargs) -> object:
                yield unusable_body

            def close(self) -> None:
                self.closed = True

        class FreshFullResponse:
            ok = True
            status_code = 200
            headers = {"Content-Length": str(len(full_content)), "ETag": '"fresh-etag"'}

            def iter_content(self, **kwargs) -> object:
                yield full_content

            def close(self) -> None:
                pass

        unusable_response = UnusableRangeResponse()
        calls: list[tuple] = []

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None):
            calls.append((range_from, validator))
            return (unusable_response if len(calls) == 1 else FreshFullResponse()), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertEqual(calls, [(11, '"old-etag"'), (None, None)]) # 第二次是不带 Range 的全新请求
        self.assertTrue(unusable_response.closed) # 不可信的响应必须被关掉，不能拿它的响应体接着用
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(state["downloaded_size"], len(full_content))
        self.assertEqual(state["total_size"], len(full_content))
        self.assertFalse(Path(temp_path).exists())
        self.assertEqual(Path(save_path).read_bytes(), full_content) # 逐字节比对，不是长度/计数器凑巧一致

    def test_206_with_unparseable_content_range_is_never_written_as_if_it_were_the_full_file(self) -> None:
        # 同上，触发条件换成 Content-Range 解析不出来。
        save_path = str(Path(self.tmp_dir) / "book2.pdf")
        temp_path = f"{save_path}.tmp"
        with open(temp_path, "wb") as file:
            file.write(b"OLD-PARTIAL")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"old-etag"'

        full_content = os.urandom(4200)

        class UnparseableRangeResponse:
            ok = True
            status_code = 206
            headers = {"Content-Range": "bytes */5000", "Content-Length": "999"}

            def __init__(self) -> None:
                self.closed = False

            def iter_content(self, **kwargs) -> object:
                yield b"x" * 999

            def close(self) -> None:
                self.closed = True

        class FreshFullResponse:
            ok = True
            status_code = 200
            headers = {"Content-Length": str(len(full_content)), "ETag": '"fresh-etag"'}

            def iter_content(self, **kwargs) -> object:
                yield full_content

            def close(self) -> None:
                pass

        unparseable_response = UnparseableRangeResponse()
        calls: list[tuple] = []

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None):
            calls.append((range_from, validator))
            return (unparseable_response if len(calls) == 1 else FreshFullResponse()), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertEqual(calls, [(11, '"old-etag"'), (None, None)])
        self.assertTrue(unparseable_response.closed)
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(Path(save_path).read_bytes(), full_content)

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

    def test_pause_in_flight_with_non_ok_response_is_not_treated_as_a_real_failure(self) -> None:
        # P0-2：请求还在飞的时候用户点了暂停，随后服务端偏偏回了非 ok 状态码。
        # 响应是否 ok 已经不重要——这一轮不管拿到什么，都该按暂停收场，不能判成真失败。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control

        class FailingResponse:
            ok = False
            status_code = 503
            content = b""

            def close(self) -> None:
                pass

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None):
            control.pause_event.set() # 请求在飞时用户点了暂停，响应此刻还没返回
            return FailingResponse(), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertFalse(state["finished"]) # 暂停契约：finished 必须是 False，留给“继续”重试
        self.assertIsNone(state["failed_reason"]) # 不能被判成 HTTP 503 真失败
        self.assertFalse(Path(f"{save_path}.tmp").exists()) # 从未写过任何字节，不该凭空产生 .tmp

    def test_pause_requested_right_before_a_clean_stream_end_preserves_the_partial_file(self) -> None:
        # P0-3：暂停恰好撞上流干净结束（不抛异常）。循环内 break 用的检查不会再被沿用到
        # 循环之后的分类判断上——分类必须重新读一次 stop_reason()，否则会被当成“下载不完整”，
        # 把好不容易保住的半截文件删掉，直接打掉暂停功能本身的意义。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control

        class EofOnPauseResponse:
            ok = True
            status_code = 200
            headers = {"Content-Length": "2048"} # 声称全长 2048，但连接会在暂停后干净结束

            def __init__(self) -> None:
                self.closed = False

            def iter_content(self, **kwargs) -> object:
                yield b"x" * 512
                # 消费者写完这块、查过 stop_reason()（此时还没暂停）之后，才会回来问生成器要下一项；
                # 用户正是在“等待下一块”这段时间点了暂停，随后连接被关掉，迭代干净结束（不抛异常）。
                control.pause_event.set()
                return

            def close(self) -> None:
                self.closed = True

        with patch.object(panel, "request_download", return_value=(EofOnPauseResponse(), [self.url])):
            panel.download_file(self.url, save_path, None, state)

        self.assertFalse(state["finished"]) # 暂停契约
        self.assertIsNone(state["failed_reason"]) # 不能被判成“文件下载不完整”
        self.assertEqual(state["downloaded_size"], 512)
        self.assertTrue(Path(f"{save_path}.tmp").exists()) # 半截文件必须保留，不能被完整性校验删掉
        self.assertEqual(Path(f"{save_path}.tmp").read_bytes(), b"x" * 512)

    def test_pause_stops_reading_via_cooperative_check_even_when_the_stream_never_raises(self) -> None:
        # “良民”流：close() 之后不抛错，会一直正常吐块。暂停必须靠分块循环里的协作式检查
        # 提前 break，而不是像上一条那样恰好等到断连异常。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        chunk_count, chunk_size = 200, 100
        response = WellBehavedStoppableResponse(control.pause_event.set, trigger_after_chunks=3, chunk_count=chunk_count, chunk_size=chunk_size)

        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            panel.download_file(self.url, save_path, None, state)

        total_size = chunk_count * chunk_size
        self.assertGreater(state["downloaded_size"], 0)
        self.assertLess(state["downloaded_size"], total_size // 2) # 远小于全长，证明是协作式检查提前停下的
        self.assertFalse(state["finished"]) # 暂停：留给“继续”
        self.assertTrue(Path(f"{save_path}.tmp").exists())
        self.assertEqual(Path(f"{save_path}.tmp").stat().st_size, state["downloaded_size"])
        self.assertFalse(Path(save_path).exists())

    def test_cancel_in_flight_deletes_the_partially_written_temp_file(self) -> None:
        # 良民流路径：分块循环正常 break 出来（不经过 except），必须清理掉已经写了一部分的 .tmp。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        response = WellBehavedStoppableResponse(control.cancel_event.set, trigger_after_chunks=3, chunk_count=200, chunk_size=100)

        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            panel.download_file(self.url, save_path, None, state)

        self.assertTrue(state["finished"])
        self.assertIsNone(state["failed_reason"]) # 取消不算失败
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 0)
        self.assertFalse(Path(f"{save_path}.tmp").exists()) # 之前确实写过若干字节，取消后必须清理掉
        self.assertFalse(Path(save_path).exists())

    def test_cancel_via_disconnect_exception_also_deletes_the_partial_temp_file(self) -> None:
        # 断连异常路径：iter_content 在取下一块时才抛错（except 分支），同样要清理 .tmp。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        response = TriggerThenRaiseResponse(control.cancel_event.set, chunk_count=3, chunk_size=100)

        with patch.object(panel, "request_download", return_value=(response, [self.url])):
            panel.download_file(self.url, save_path, None, state)

        self.assertTrue(state["finished"])
        self.assertIsNone(state["failed_reason"]) # 取消不算失败，不应该走进真失败那条分支
        self.assertEqual(state["downloaded_size"], 0)
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        self.assertFalse(Path(save_path).exists())

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

        with patch.object(panel, "request_download") as mocked_request_download:
            panel.download_file(self.url, state["save_path"], None, state)

        mocked_request_download.assert_not_called() # 排队中被取消：不发起网络请求，不是“发了请求随后按取消收尾”
        self.assertTrue(state["finished"])
        self.assertFalse(Path(f"{state['save_path']}.tmp").exists())
        self.acquire_slots_without_blocking()

    def test_cancelled_before_request_cleans_up_tmp_left_over_from_a_previous_pause(self) -> None:
        # P1-5：暂停留下 .tmp 后点“继续”，任务在取得执行机会前又被取消——排队取消分支
        # 必须清理这个“上一轮留下的” .tmp，不能只在“这次有没有产生过” .tmp 上打转
        # （改动前这条路径永远拿到一个全新任务，不可能预先存在 .tmp，现在续传场景下会）。
        save_path = str(Path(self.tmp_dir) / "resumed.pdf")
        with open(f"{save_path}.tmp", "wb") as file:
            file.write(b"leftover-from-a-previous-pause")
        state = panel.create_download_state(self.url, save_path)
        state["downloaded_size"] = 31 # 与半截内容对应，模拟暂停时记下的旧计数器
        control = panel.BatchControl()
        control.cancel_event.set()
        state["control"] = control

        with patch.object(panel, "request_download") as mocked_request_download:
            panel.download_file(self.url, save_path, None, state)

        mocked_request_download.assert_not_called()
        self.assertTrue(state["finished"])
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 0)
        self.assertFalse(Path(f"{save_path}.tmp").exists())

    def test_non_ok_response_cleans_up_tmp_and_counters_instead_of_leaving_them_stale(self) -> None:
        # P1-5：plan_download_write 对非 ok 响应的早退不会归零，download_file 必须在这里补上，
        # 否则失败文件的残留字节会被计进批次总进度，且残留 .tmp 会让同一次运行里重下该资源
        # 被 allocate_download_paths 误判成“已存在”，改名成 book (2).pdf。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        with open(f"{save_path}.tmp", "wb") as file:
            file.write(b"leftover")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"old-etag"'

        class FailingResponse:
            ok = False
            status_code = 503
            content = b""

            def close(self) -> None:
                pass

        with patch.object(panel, "request_download", return_value=(FailingResponse(), [self.url])):
            panel.download_file(self.url, save_path, None, state)

        self.assertTrue(state["finished"])
        self.assertIsNotNone(state["failed_reason"])
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 0)
        self.assertFalse(Path(f"{save_path}.tmp").exists())

    def test_paused_before_request_releases_the_slot_and_leaves_unfinished(self) -> None:
        state = panel.create_download_state(self.url, str(Path(self.tmp_dir) / "b.pdf"))
        control = panel.BatchControl()
        control.pause_event.set()
        state["control"] = control

        with patch.object(panel, "request_download") as mocked_request_download:
            panel.download_file(self.url, state["save_path"], None, state)

        mocked_request_download.assert_not_called() # 排队中被暂停同样不发起网络请求
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


class BatchOutcomeTest(unittest.TestCase):
    """_run_batch_worker 的终态判定，以及 handle_batch_outcome 对 _batch_control/paused_settled 的处置。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.directory = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        self.context.enter_context(patch.object(panel, "download_states", []))
        self.context.enter_context(patch.object(panel, "_batch_control", None))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.notice = self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        self.warning = self.context.enter_context(patch.object(panel.messagebox, "showwarning"))

    def make_state(self, finished: bool = True, failed_reason: str | None = None) -> dict:
        state = panel.create_download_state("https://example.com/book.pdf", str(Path(self.directory) / "book.pdf"))
        state["finished"] = finished
        state["failed_reason"] = failed_reason
        return state

    def test_outcome_is_completed_when_neither_flag_is_set(self) -> None:
        control = panel.BatchControl()
        control.directory = self.directory
        panel.download_states = [self.make_state()]
        panel._batch_control = control

        panel._run_batch_worker([], control) # 空列表：没有任务要跑，只关心终态判定本身

        self.notice.assert_called_once_with("下载完成", f"文件已下载到：{self.directory}")
        self.assertIsNone(panel._batch_control)

    def test_outcome_is_cancelled_when_cancel_event_is_set(self) -> None:
        control = panel.BatchControl()
        control.directory = self.directory
        control.cancel_event.set()
        panel.download_states = [self.make_state()]
        panel._batch_control = control

        panel._run_batch_worker([], control)

        self.notice.assert_not_called() # 取消不弹“下载完成”
        self.warning.assert_not_called()
        self.assertIsNone(panel._batch_control)

    def test_outcome_is_paused_when_only_pause_event_is_set(self) -> None:
        control = panel.BatchControl()
        control.directory = self.directory
        control.pause_event.set()
        panel.download_states = [self.make_state(finished=False)]
        panel._batch_control = control

        panel._run_batch_worker([], control)

        self.assertTrue(control.paused_settled)
        self.assertIs(panel._batch_control, control) # 暂停不清空控制对象，留给“继续”使用
        self.notice.assert_not_called()

    def test_cancel_takes_priority_over_pause_when_both_are_set(self) -> None:
        control = panel.BatchControl()
        control.directory = self.directory
        control.pause_event.set()
        control.cancel_event.set()
        panel.download_states = [self.make_state(finished=False)]
        panel._batch_control = control

        panel._run_batch_worker([], control)

        self.assertFalse(control.paused_settled) # 没有走到“暂停”分支
        self.assertIsNone(panel._batch_control) # 走的是取消分支

    def test_reclassifies_as_cancelled_when_cancel_arrives_after_outcome_was_computed(self) -> None:
        # P1-4：_run_batch_worker 算出 outcome="paused" 之后、ui_call 排队的回调真正执行之前，
        # 批次线程已经退出，用户在这个窗口点了取消——cancel_event 此刻已经置位，但传进 handle_batch_outcome
        # 的 outcome 参数仍然是算出来时的旧值 "paused"。不重判的话，取消会被静默吞掉：paused_settled
        # 置位、.tmp 全留、finished 仍是 False、cancel_event 也没清，用户再点“继续”会立刻走排队取消分支。
        control = panel.BatchControl()
        control.directory = self.directory
        state = self.make_state(finished=False)
        Path(f"{state['save_path']}.tmp").write_bytes(b"partial")
        panel.download_states = [state]
        panel._batch_control = control

        control.cancel_event.set() # 在 outcome 参数被算出之后才发生，函数收到的 outcome 还是旧的

        panel.handle_batch_outcome("paused", control)

        self.assertFalse(control.paused_settled) # 没有落成“暂停”
        self.assertIsNone(panel._batch_control) # 按取消收尾，控制对象被清空，不留给“继续”
        self.assertTrue(state["finished"])
        self.assertFalse(Path(f"{state['save_path']}.tmp").exists()) # 半截 .tmp 被清理，不是“全留”
        self.notice.assert_not_called() # 不弹“下载完成”

    def test_cancelled_outcome_force_finishes_any_leftover_unfinished_state(self) -> None:
        # 批次生命周期边界上的最后一道保险：正常情况下 download_file 内部已经处理过，这里只兜底。
        control = panel.BatchControl()
        control.directory = self.directory
        control.cancel_event.set()
        leftover = self.make_state(finished=False)
        panel.download_states = [leftover]
        panel._batch_control = control

        panel._run_batch_worker([], control)

        self.assertTrue(leftover["finished"])
        self.assertEqual(leftover["downloaded_size"], 0)
        self.assertEqual(leftover["total_size"], 0)


class PausedSettledInvariantTest(unittest.TestCase):
    """paused_settled 只能被置位（= True）一次；置为 False 属于“开始新一轮”的合法复位
    （BatchControl 的初始值、resume_current_batch 里的复位），不算第二个置位点。"""

    def test_only_handle_batch_outcome_ever_sets_it_to_true(self) -> None:
        source = inspect.getsource(panel)
        true_assignments = re.findall(r"\.paused_settled\s*=\s*True\b", source)
        self.assertEqual(len(true_assignments), 1, f"paused_settled 应该只有一处被置为 True，实际找到 {len(true_assignments)} 处")
        self.assertIn("paused_settled = True", inspect.getsource(panel.handle_batch_outcome))


class BatchControlActionsTest(unittest.TestCase):
    """暂停/取消/继续三个按钮回调；重点是 paused_settled 划分出的两条取消路径不能走错。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.tmp_dir = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        self.context.enter_context(patch.object(panel, "download_states", []))
        self.context.enter_context(patch.object(panel, "_batch_control", None))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        self.context.enter_context(patch.object(panel, "thread_it", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.notice = self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        self.warning = self.context.enter_context(patch.object(panel.messagebox, "showwarning"))

    def make_in_progress_state(self, content: bytes = b"partial") -> dict:
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        with open(f"{save_path}.tmp", "wb") as file:
            file.write(content)
        state = panel.create_download_state("https://example.com/book.pdf", save_path)
        state["finished"] = False
        return state

    def test_no_op_when_idle(self) -> None:
        panel._batch_control = None
        panel.cancel_current_batch() # 不应该抛出
        panel.pause_current_batch()
        panel.resume_current_batch()

    def test_pause_sets_the_event_and_closes_active_responses(self) -> None:
        control = panel.BatchControl()
        panel._batch_control = control
        closed = []

        class FakeResponse:
            def close(self) -> None:
                closed.append(True)

        control.active_responses[1] = FakeResponse()

        panel.pause_current_batch()

        self.assertTrue(control.pause_event.is_set())
        self.assertEqual(closed, [True])

    def test_cancel_sets_the_event_and_closes_active_responses(self) -> None:
        # 与暂停对称：协作式检查只在收到一块数据之后才触发，连接卡住不吐数据时
        # （限流、网络中断但 TCP 未断）只能靠主动断连尽快停下，否则要等到 60 秒读超时，
        # 取消就不再是“随时可用”的逃生口。
        control = panel.BatchControl()
        panel._batch_control = control
        closed = []

        class FakeResponse:
            def close(self) -> None:
                closed.append(True)

        control.active_responses[1] = FakeResponse()

        panel.cancel_current_batch()

        self.assertTrue(control.cancel_event.is_set())
        self.assertEqual(closed, [True])

    def test_cancel_while_batch_thread_still_running_does_not_touch_files_or_go_idle(self) -> None:
        control = panel.BatchControl() # 从未请求过暂停：典型的“下载中点取消”
        panel._batch_control = control
        state = self.make_in_progress_state()
        panel.download_states = [state]

        with patch.object(panel, "set_ui_phase") as mocked_set_ui_phase:
            panel.cancel_current_batch()

        self.assertTrue(control.cancel_event.is_set())
        self.assertIs(panel._batch_control, control) # 批次线程还活着，控制对象留给它自己收尾
        self.assertTrue(Path(f"{state['save_path']}.tmp").exists()) # 没有被这次调用删掉
        self.assertFalse(state["finished"])
        mocked_set_ui_phase.assert_not_called() # 界面复位交给 handle_batch_outcome，这里不越权

    def test_cancel_immediately_after_pause_request_is_handled_by_the_batch_thread(self) -> None:
        # 设计文档 (d) 描述的窗口：已经点了暂停（pause_event 置位），但批次线程还没退出、
        # handle_batch_outcome 还没跑（paused_settled 仍是 False）——这一刻点取消。
        control = panel.BatchControl()
        control.pause_event.set()
        self.assertFalse(control.paused_settled) # 前置条件：确实还在窗口里，不是已经停稳
        panel._batch_control = control
        state = self.make_in_progress_state()
        panel.download_states = [state]

        with patch.object(panel, "set_ui_phase") as mocked_set_ui_phase:
            panel.cancel_current_batch()

        self.assertTrue(control.cancel_event.is_set()) # 批次线程稍后会自己发现并按取消收尾
        self.assertIs(panel._batch_control, control) # 没有被同步清空——批次线程仍然存活
        self.assertTrue(Path(f"{state['save_path']}.tmp").exists()) # 没有被主线程删掉
        self.assertFalse(state["finished"]) # 没有被主线程强制终结
        mocked_set_ui_phase.assert_not_called() # 没有走同步收尾分支

    def test_cancel_while_truly_paused_cleans_up_without_a_live_batch_thread(self) -> None:
        control = panel.BatchControl()
        control.pause_event.set()
        control.paused_settled = True # 已经由 handle_batch_outcome 确认批次线程退出
        panel._batch_control = control
        state = self.make_in_progress_state()
        panel.download_states = [state]

        panel.cancel_current_batch()

        self.assertIsNone(panel._batch_control) # 没有线程会来清空，只能自己清
        self.assertFalse(Path(f"{state['save_path']}.tmp").exists())
        self.assertTrue(state["finished"])
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 0)
        panel.download_btn.config.assert_called_once_with(text="下载", state="normal", command=panel.download)

    def test_resume_does_nothing_before_the_batch_has_settled_as_paused(self) -> None:
        control = panel.BatchControl()
        control.pause_event.set() # 已经请求暂停，但还没停稳
        panel._batch_control = control
        panel.download_states = [self.make_in_progress_state()]

        with patch.object(panel, "_run_batch_worker") as mocked_worker:
            panel.resume_current_batch()

        mocked_worker.assert_not_called()
        self.assertTrue(control.pause_event.is_set()) # 没有被这次误判的“继续”清掉

    def test_resume_clears_flags_and_resubmits_only_unfinished_states(self) -> None:
        control = panel.BatchControl()
        control.pause_event.set()
        control.paused_settled = True
        panel._batch_control = control
        finished_state = panel.create_download_state("https://example.com/done.pdf", str(Path(self.tmp_dir) / "done.pdf"))
        finished_state["finished"] = True
        pending_state = self.make_in_progress_state()
        panel.download_states = [finished_state, pending_state]
        submitted: list[tuple] = []

        def fake_run_batch_worker(states_to_run: list[dict], passed_control: "panel.BatchControl") -> None:
            submitted.append((states_to_run, passed_control))

        with patch.object(panel, "_run_batch_worker", fake_run_batch_worker):
            panel.resume_current_batch()

        self.assertFalse(control.pause_event.is_set())
        self.assertFalse(control.paused_settled)
        self.assertEqual(len(submitted), 1)
        resubmitted_states, passed_control = submitted[0]
        self.assertEqual(resubmitted_states, [pending_state]) # 只续传未完成的
        self.assertIs(passed_control, control)

    def test_resume_with_no_pending_states_settles_as_completed_defensively(self) -> None:
        control = panel.BatchControl()
        control.pause_event.set()
        control.paused_settled = True
        panel._batch_control = control
        finished_state = panel.create_download_state("https://example.com/done.pdf", str(Path(self.tmp_dir) / "done.pdf"))
        finished_state["finished"] = True
        panel.download_states = [finished_state]

        with patch.object(panel, "_run_batch_worker") as mocked_worker:
            panel.resume_current_batch()

        mocked_worker.assert_not_called()
        self.assertIsNone(panel._batch_control) # 走了 handle_batch_outcome("completed", ...) 的收尾


class DownloadEntryIdleResetTest(unittest.TestCase):
    """download() 里“放弃这次下载”（不是取消）的五条路径，逐条验证真的回到空闲。

    断言的是 set_ui_phase 被以什么参数调用过（行为），不是等它跑完再看某个控件的最终文案——
    后者在“压根没触发复位”和“复位了但选错了阶段”两种情况下都可能凑巧对。
    """

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(panel, "download_states", []))
        self.context.enter_context(patch.object(panel, "_batch_control", None))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        self.context.enter_context(patch.object(panel, "thread_it", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn", "url_text", "bookmark_var"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.warning = self.context.enter_context(patch.object(panel.messagebox, "showwarning"))
        self.notice = self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        previous_token = panel.config.access_token
        self.addCleanup(setattr, panel.config, "access_token", previous_token)
        panel.config.access_token = None
        panel.bookmark_var.get.return_value = False

    def test_token_with_non_ascii_characters_resets_to_idle(self) -> None:
        panel.config.access_token = "无效token"
        panel.url_text.get.return_value = "https://example.com/1"

        with patch.object(panel, "set_ui_phase") as mocked_phase:
            panel.download()

        self.assertEqual(mocked_phase.call_args_list, [call("parsing"), call("idle")])
        self.assertIsNone(panel._batch_control)

    def test_empty_urls_resets_to_idle(self) -> None:
        panel.url_text.get.return_value = "   \n  "

        with patch.object(panel, "set_ui_phase") as mocked_phase:
            panel.download()

        self.assertEqual(mocked_phase.call_args_list, [call("parsing"), call("idle")])
        self.assertIsNone(panel._batch_control)

    def test_askdirectory_cancelled_resets_to_idle(self) -> None:
        resource_by_url = {
            f"https://example.com/{index}.pdf": ResourceInfo(f"教材{index}", f"https://example.com/{index}.pdf", "pdf", [])
            for index in range(2)
        }
        panel.url_text.get.return_value = "\n".join(resource_by_url)

        with patch.object(panel, "parse", side_effect=lambda url, bookmarks: [resource_by_url[url]]):
            with patch.object(panel.filedialog, "askdirectory", return_value=""):
                with patch.object(panel, "set_ui_phase") as mocked_phase:
                    panel.download()

        self.assertEqual(mocked_phase.call_args_list, [call("parsing"), call("idle")])
        self.assertIsNone(panel._batch_control)

    def test_asksaveasfilename_cancelled_resets_to_idle(self) -> None:
        resource = ResourceInfo("教材", "https://example.com/only.pdf", "pdf", [])
        panel.url_text.get.return_value = resource.url

        with patch.object(panel, "parse", return_value=[resource]):
            with patch.object(panel.filedialog, "asksaveasfilename", return_value=""):
                with patch.object(panel, "set_ui_phase") as mocked_phase:
                    panel.download()

        self.assertEqual(mocked_phase.call_args_list, [call("parsing"), call("idle")])
        self.assertIsNone(panel._batch_control)

    def test_no_parseable_resources_resets_to_idle(self) -> None:
        panel.url_text.get.return_value = "https://example.com/bad"

        with patch.object(panel, "parse", return_value=None):
            with patch.object(panel, "set_ui_phase") as mocked_phase:
                panel.download()

        self.assertEqual(mocked_phase.call_args_list, [call("parsing"), call("idle")])
        self.assertIsNone(panel._batch_control)
        self.warning.assert_called_once() # 解析失败清单仍然要提示


class ParsePhaseCancellationTest(unittest.TestCase):
    """“取消”要覆盖解析阶段：命中后不再解析下一条 URL，也不弹任何对话框。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(panel, "download_states", []))
        self.context.enter_context(patch.object(panel, "_batch_control", None))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        self.context.enter_context(patch.object(panel, "thread_it", lambda fn, *args, **kwargs: fn(*args, **kwargs)))
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn", "url_text", "bookmark_var"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.warning = self.context.enter_context(patch.object(panel.messagebox, "showwarning"))
        self.notice = self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        previous_token = panel.config.access_token
        self.addCleanup(setattr, panel.config, "access_token", previous_token)
        panel.config.access_token = None
        panel.bookmark_var.get.return_value = False

    def test_cancel_during_parsing_stops_early_and_skips_all_dialogs(self) -> None:
        urls = [f"https://example.com/{index}" for index in range(5)]
        panel.url_text.get.return_value = "\n".join(urls)
        parsed_calls: list[str] = []

        def fake_parse(url: str, bookmarks: bool) -> list[ResourceInfo]:
            parsed_calls.append(url)
            if len(parsed_calls) == 2: # 模拟解析到第二条时用户点了取消
                panel._batch_control.cancel_event.set()
            return [ResourceInfo(url, url, "pdf", [])]

        with patch.object(panel, "parse", fake_parse):
            with patch.object(panel.filedialog, "askdirectory") as mocked_askdirectory:
                with patch.object(panel, "set_ui_phase") as mocked_phase:
                    panel.download()

        self.assertLess(len(parsed_calls), len(urls)) # 没有解析完剩下的 URL
        mocked_askdirectory.assert_not_called() # 取消命中后不再弹任何对话框
        self.notice.assert_not_called()
        self.assertEqual(mocked_phase.call_args_list, [call("parsing"), call("idle")])
        self.assertIsNone(panel._batch_control)


class ParseAndCopyDoesNotWireCancellationTest(unittest.TestCase):
    """“解析并复制”是独立路径，不该被顺手接上取消——should_stop 必须保持 None。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        for name in ("copy_btn", "url_text"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))

    def test_should_stop_is_none_for_the_copy_flow(self) -> None:
        captured: dict[str, object] = {"called": False}

        def fake_parse_urls_in_background(urls, bookmarks, on_finished, should_stop=None) -> None:
            captured["called"] = True
            captured["should_stop"] = should_stop

        panel.url_text.get.return_value = "https://example.com/1"
        with patch.object(panel, "parse_urls_in_background", fake_parse_urls_in_background):
            panel.parse_and_copy()

        self.assertTrue(captured["called"])
        self.assertIsNone(captured["should_stop"])


if __name__ == "__main__":
    unittest.main()
