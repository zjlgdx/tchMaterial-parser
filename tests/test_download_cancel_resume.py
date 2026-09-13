from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, call, patch
import inspect
import io
import os
import re
import tempfile
import threading
import time
import unittest

from pypdf import PdfReader, PdfWriter

from src.tchmaterial_parser import bookmarks
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


class SlicedResponse:
    """按 chunk_size 分块吐出真实字节（不是随机/占位字节，方便断言最终文件逐字节正确），
    可选在吐出第 trigger_after_chunks 块之后触发一次回调（例如置位 pause_event），用来
    构造“传输到一半被真实暂停”的场景，而不是靠手工摆状态伪造暂停/续传后的结果——注意
    trigger_after_chunks 是被吐出的块的下标（从 0 起），实际已写入的块数是这个值 + 1
    （例如 trigger_after_chunks=2 会在第 3 块吐出后触发回调，届时已经写入 3 块）。

    `start` 给出时是 206 续传响应，从 full_content 的这个位置切片，带 Content-Range/ETag；
    `start` 为 None 时是 200 完整正文响应，把 body 整份按 chunk_size 分块吐出。"""

    def __init__(self, body: bytes, start: int | None = None, chunk_size: int = 100,
                 headers: dict | None = None, trigger=None, trigger_after_chunks: int | None = None) -> None:
        self.ok = True
        if start is None:
            self.status_code = 200
            self.headers = dict(headers or {})
            self.headers.setdefault("Content-Length", str(len(body)))
            self._body = body
        else:
            total = len(body)
            self.status_code = 206
            self.headers = {
                "Content-Range": f"bytes {start}-{total - 1}/{total}",
                "Content-Length": str(total - start),
                # 这个 ETag 只是凑一个“看起来完整”的 206 响应头，从不会被下游代码读到：
                # _response_usability 判定 "resumed" 时，plan_download_write 对这个分支
                # 恒返回 validator=None（ab 分支“不归它管”），下游断言的是“旧校验子原封
                # 不动”，不是“这个响应头真的被采纳”——调用方若断言 calls[1] 里出现了
                # 这个值，证的不是响应头生效，而是校验子压根没被这次响应动过。
                "ETag": '"v1"',
            }
            self._body = body[start:]
        self._chunk_size = chunk_size
        self._trigger = trigger
        self._trigger_after_chunks = trigger_after_chunks
        self.closed = False

    def iter_content(self, **kwargs) -> object:
        for index, offset in enumerate(range(0, len(self._body), self._chunk_size)):
            if self._trigger is not None and index == self._trigger_after_chunks:
                self._trigger()
            yield self._body[offset:offset + self._chunk_size]

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

    def test_416_returns_immediately_instead_of_rotating_mirrors(self) -> None:
        # 416 是“你要的范围本身不可满足”。各镜像服务的是同一个对象，换一个也同样不可满足；
        # 继续轮换只会让后面镜像的无关错误盖掉这个信号，调用方就再也判不出该走回退路径。
        fake_session = FakeHeaderRecordingSession([FakeRangeResponse(416), FakeRangeResponse(500), FakeRangeResponse(500)])
        panel.session = fake_session
        url = "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf"

        response, attempted_urls = panel.request_download(url, range_from=4000, validator='"etag"')

        self.assertEqual(response.status_code, 416) # 返回的必须是那个 416 本身
        self.assertEqual(attempted_urls, [url])
        self.assertEqual(fake_session.requested_urls, [url]) # r2/r3 一条都不该再打

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
            open_mode, downloaded_size, total_size, validator, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(open_mode, "ab")
        self.assertIs(used_response, response)
        self.assertEqual(total_size, 5000)
        self.assertEqual(downloaded_size, 100)
        self.assertIsNone(validator) # 可续传的 206 不刷新校验子，沿用调用方手上已有的那份

    def test_resume_accumulates_downloaded_size_from_existing_temp_file_offset(self) -> None:
        state = self.existing_state(offset=12345)
        response = FakeRangeResponse(206, {"Content-Range": "bytes 12345-19999/20000", "Content-Length": "7655"})
        with patch.object(panel, "request_download", return_value=(response, [self.url])) as mocked:
            _open_mode, downloaded_size, _total_size, _validator, _response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(downloaded_size, 12345) # 现读的磁盘偏移，不是某个内存里的旧计数
        self.assertEqual(mocked.call_args.kwargs["range_from"], 12345)

    def test_resume_retries_from_scratch_on_416(self) -> None:
        state = self.existing_state(offset=500)
        range_invalid = FakeRangeResponse(416)
        fresh = FakeRangeResponse(200, {"Content-Length": "999", "ETag": '"etag-new"'})
        with patch.object(panel, "request_download", side_effect=[(range_invalid, [self.url]), (fresh, [self.url])]) as mocked:
            open_mode, downloaded_size, total_size, validator, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(range_invalid.closed) # 不信任 416 响应，主动关闭后重来
        self.assertEqual(mocked.call_args_list[1].kwargs.get("range_from"), None)
        self.assertEqual(open_mode, "wb")
        self.assertIs(used_response, fresh)
        self.assertEqual(downloaded_size, 0)
        self.assertEqual(total_size, 999)
        self.assertEqual(validator, '"etag-new"') # 重试拿到的是完整正文，要刷新校验子

    def test_resume_requires_content_range_start_to_match_requested_offset(self) -> None:
        # 206 但起点不匹配，响应体只是那一段，不能直接当整份写下去——必须像 416 一样
        # 关掉这次响应、重新发一次不带 Range 的请求。用两个不同的总长断言最终数值确实来自
        # 那次重试的响应，而不是继续沿用第一次（不可信）响应里的总长凑巧对上。
        state = self.existing_state(offset=1000)
        mismatched_206 = FakeRangeResponse(206, {"Content-Range": "bytes 0-4999/5000", "Content-Length": "5000"})
        fresh_full_200 = FakeRangeResponse(200, {"Content-Length": "9999", "ETag": '"fresh"'})
        with patch.object(panel, "request_download", side_effect=[(mismatched_206, [self.url]), (fresh_full_200, [self.url])]) as mocked:
            open_mode, downloaded_size, total_size, validator, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 2)
        self.assertIsNone(mocked.call_args_list[1].kwargs.get("range_from")) # 第二次是不带 Range 的全新请求
        self.assertTrue(mismatched_206.closed) # 不可信的响应必须被关掉，不能拿它的响应体接着用
        self.assertIs(used_response, fresh_full_200)
        self.assertEqual(open_mode, "wb")
        self.assertEqual(downloaded_size, 0)
        self.assertEqual(total_size, 9999) # 来自重试后的响应，不是第一次那个 5000
        self.assertEqual(validator, '"fresh"')

    def test_malformed_content_range_falls_back_to_full_restart(self) -> None:
        # 同上，只是触发条件换成 Content-Range 解析不出来。
        state = self.existing_state(offset=200)
        unparseable_206 = FakeRangeResponse(206, {"Content-Range": "not-a-content-range", "Content-Length": "42"})
        fresh_full_200 = FakeRangeResponse(200, {"Content-Length": "777", "ETag": '"fresh"'})
        with patch.object(panel, "request_download", side_effect=[(unparseable_206, [self.url]), (fresh_full_200, [self.url])]) as mocked:
            open_mode, downloaded_size, total_size, validator, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(unparseable_206.closed)
        self.assertIs(used_response, fresh_full_200)
        self.assertEqual(open_mode, "wb")
        self.assertEqual(downloaded_size, 0)
        self.assertEqual(total_size, 777) # 不是第一次那个 42
        self.assertEqual(validator, '"fresh"')

    def test_no_validator_never_attempts_a_range_request(self) -> None:
        state = self.existing_state(offset=800, validator=None)
        response = FakeRangeResponse(200, {"Content-Length": "800"})
        with patch.object(panel, "request_download", return_value=(response, [self.url])) as mocked:
            panel.plan_download_write(state, self.temp_path, self.url)

        # 没有校验子时按原有的“单参数”方式调用，不额外声称一次并不存在的 Range 续传
        mocked.assert_called_once_with(self.url, control=None)

    def test_failed_response_returns_none_open_mode_without_a_wasted_retry(self) -> None:
        # 真正的失败（与 Range 无关，例如 404）不该被当成“范围有问题”而多打一次不带 Range 的请求；
        # 直接判定不可信，交给调用方走失败分支。失败响应未必带 headers，这里也不该去碰它们。
        state = self.existing_state(offset=100)

        class FailedResponseWithoutHeaders:
            status_code = 404
            ok = False

            def close(self) -> None:
                pass

        with patch.object(panel, "request_download", return_value=(FailedResponseWithoutHeaders(), [self.url])) as mocked:
            open_mode, downloaded_size, total_size, validator, response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 1) # 没有为一个与 Range 无关的失败多打一次请求
        self.assertFalse(response.ok)
        self.assertIsNone(open_mode) # 调用方看到 None 就知道不能信任这次响应，会走失败分支
        self.assertEqual(downloaded_size, 0)
        self.assertEqual(total_size, 0)
        self.assertIsNone(validator)

    def test_ok_but_not_200_is_not_treated_as_a_usable_full_body(self) -> None:
        # response.ok 是 status_code < 400，204/304 这类“ok 但没有正文”的响应
        # 不该被当成完整正文写成一个零字节的“成功”文件。
        state = self.existing_state(offset=0, validator=None)
        os.remove(self.temp_path)

        class NoContentResponse:
            status_code = 204
            ok = True

            def close(self) -> None:
                pass

        with patch.object(panel, "request_download", return_value=(NoContentResponse(), [self.url])):
            open_mode, downloaded_size, total_size, validator, _response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertIsNone(open_mode)
        self.assertEqual(downloaded_size, 0)
        self.assertEqual(total_size, 0)
        self.assertIsNone(validator)

    def test_second_response_being_an_unexpected_206_is_not_trusted_either(self) -> None:
        # 重试之后的响应也要走同一套“能不能当整份正文用”的判据，不能无条件信任——
        # 一个不规范的 CDN 在不带 Range 的重试上仍然回 206 时，不能把这段 partial body 当整份写下去。
        state = self.existing_state(offset=500)
        range_invalid = FakeRangeResponse(416)
        unexpected_206_on_retry = FakeRangeResponse(206, {"Content-Range": "bytes 0-99/5000", "Content-Length": "100"})
        with patch.object(panel, "request_download", side_effect=[(range_invalid, [self.url]), (unexpected_206_on_retry, [self.url])]):
            open_mode, downloaded_size, total_size, validator, used_response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertIsNone(open_mode)
        self.assertIs(used_response, unexpected_206_on_retry)
        self.assertEqual(downloaded_size, 0)
        self.assertEqual(total_size, 0)
        self.assertIsNone(validator)

    def test_unsolicited_206_on_a_plain_download_is_not_trusted(self) -> None:
        # can_attempt_range 为 False 的普通下载（本地无偏移或无校验子）如果服务端
        # 自发回了 206，同样不能落回“wb + 当次 Content-Length”去信任它。
        state = self.existing_state(offset=0, validator=None)
        os.remove(self.temp_path)
        unsolicited_206 = FakeRangeResponse(206, {"Content-Range": "bytes 0-99/5000", "Content-Length": "100"})

        with patch.object(panel, "request_download", return_value=(unsolicited_206, [self.url])) as mocked:
            open_mode, downloaded_size, total_size, validator, _response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)

        self.assertEqual(mocked.call_count, 1) # 本来就没打算续传，不会为了“救”一个意外的 206 而重试
        self.assertIsNone(open_mode)
        self.assertEqual(downloaded_size, 0)
        self.assertEqual(total_size, 0)
        self.assertIsNone(validator)

    def test_validator_refreshes_whenever_response_is_a_full_body(self) -> None:
        # 首次下载（无 Range）拿到 200 要刷新校验子；带 Range 但被判定失配、回落成 200 的续传请求同样要刷新。
        state = self.existing_state(offset=0, validator=None)
        os.remove(self.temp_path)
        fresh = FakeRangeResponse(200, {"Content-Length": "10", "ETag": '"fresh-etag"'})
        with patch.object(panel, "request_download", return_value=(fresh, [self.url])):
            _open_mode, _downloaded_size, _total_size, validator, _response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)
        self.assertEqual(validator, '"fresh-etag"')

        state = self.existing_state(offset=100, validator='"stale-etag"')
        fallback_200 = FakeRangeResponse(200, {"Content-Length": "999", "Last-Modified": "Tue, 01 Jan 2030 00:00:00 GMT"})
        with patch.object(panel, "request_download", return_value=(fallback_200, [self.url])):
            _open_mode, _downloaded_size, _total_size, validator, _response, _attempted = panel.plan_download_write(state, self.temp_path, self.url)
        self.assertEqual(validator, "Tue, 01 Jan 2030 00:00:00 GMT")


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

    def test_pause_right_after_a_stale_full_body_response_does_not_leak_its_validator(self) -> None:
        # 跨两轮：plan_download_write 已经决定 wb、已经算出新版本的校验子，但 open()
        # 还没真正截断旧 .tmp 之前就被要求暂停——这时不能把新校验子/downloaded_size/total_size
        # 提前写回 current_state，否则磁盘上留着旧正文、内存里却指向新版本，两者不再同源；
        # 下一轮“继续”会带着这份还没被磁盘内容证实过的新校验子发出去，一旦服务端认可，
        # 新内容就会被追加到旧字节后面。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        temp_path = f"{save_path}.tmp"
        old_version = os.urandom(5000)
        offset = 500
        with open(temp_path, "wb") as file:
            file.write(old_version[:offset])
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"old-etag"'
        state["downloaded_size"] = offset
        control = panel.BatchControl()
        state["control"] = control

        new_version = os.urandom(5000)
        calls: list[tuple] = []

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            calls.append((range_from, validator))
            if len(calls) == 1: # 响应到达、plan_download_write 已经决定 wb 之后，用户点了暂停
                control.pause_event.set()
            return FakeRangeResponse(200, {"Content-Length": str(len(new_version)), "ETag": '"new-etag"'}, body=new_version), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state) # 第一轮：继续，中途被暂停

            self.assertFalse(state["finished"])
            self.assertEqual(state["validator"], '"old-etag"') # 没有被提前刷新成新版本的校验子
            self.assertEqual(Path(temp_path).read_bytes(), old_version[:offset]) # 磁盘内容原封不动
            self.assertEqual(state["downloaded_size"], offset) # 计数器也没被提前改动

            control.pause_event.clear()
            panel.download_file(self.url, save_path, None, state) # 第二轮：真正的继续（同一个打桩范围内）

        self.assertEqual(calls[1], (offset, '"old-etag"')) # 第二轮确实还是用暂停时那份旧校验子发起的
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(Path(save_path).read_bytes(), new_version) # 逐字节等于新版本，不是新旧拼接
        self.assertFalse(Path(temp_path).exists())

    def test_ab_resume_preserves_the_validator_across_a_second_pause(self) -> None:
        # 覆盖校验子的判据有两半——“wb 必须无条件覆盖”和“ab 必须完全不碰”。只钉住前一半的话，
        # 把写回代码换成裸的 `current_state["validator"] = planned_validator`（删掉 ab 保护）
        # 全量测试依然全绿：续传（ab）成功后校验子会被覆盖成 None（因为 plan_download_write
        # 对 "resumed" 分支恒返回 planned_validator=None），后果不是损坏，而是已下载的字节
        # 作废、退化成一次没有意义的全量重下。这里钉住 ab 分支必须原封不动地保留旧校验子，
        # 且这条续传本身要真的经历一次暂停/继续，不能靠手工摆状态。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        temp_path = f"{save_path}.tmp"
        total = 2000
        full_content = os.urandom(total)
        offset = 500
        with open(temp_path, "wb") as file:
            file.write(full_content[:offset])
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"v1"'
        state["downloaded_size"] = offset
        control = panel.BatchControl()
        state["control"] = control

        calls: list[tuple] = []

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            calls.append((range_from, validator))
            if len(calls) == 1:
                # 第一轮续传：真实写入 3 块（300 字节）之后被真的暂停打断
                return SlicedResponse(full_content, start=range_from, chunk_size=100,
                                      trigger=control.pause_event.set, trigger_after_chunks=2), [url]
            # 第二轮续传：不再暂停，一次性吐出剩余全部字节，直到完成
            return SlicedResponse(full_content, start=range_from, chunk_size=100), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state) # 第一轮续传：真实写入部分字节后被暂停

            self.assertFalse(state["finished"])
            self.assertEqual(state["validator"], '"v1"') # ab 分支：暂停之后校验子必须原封不动
            self.assertEqual(Path(temp_path).read_bytes(), full_content[:800]) # 500 + 3 * 100
            self.assertEqual(state["downloaded_size"], 800)

            control.pause_event.clear()
            panel.download_file(self.url, save_path, None, state) # 第二轮：真正的继续

        self.assertEqual(calls[0], (offset, '"v1"'))
        self.assertEqual(calls[1], (800, '"v1"')) # 第二轮仍然带着同一份没被抹掉的校验子发起续传
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(Path(save_path).read_bytes(), full_content) # 逐字节完整，不是全量重下的另一份内容
        self.assertFalse(Path(temp_path).exists())

    def test_full_body_without_a_validator_header_clears_the_stale_one_instead_of_keeping_it(self) -> None:
        # plan_download_write 对“206 续传（保留原校验子）”和“200 完整正文但服务端
        # 没给 ETag/Last-Modified（应当清空）”都返回 validator=None，写回时若用
        # `if planned_validator is not None:` 去判断该不该写，会把后一种也当成“不用管”而
        # 跳过——磁盘上已经换成了新正文，内存里的校验子却还是旧版本，下一轮“继续”会带着
        # 这份对不上的旧校验子发起 Range 请求，一旦有镜像仍持有旧版本就会把新旧内容拼接。
        # 正确做法是：能不能续传（ab）决定要不要保留旧校验子；一旦确定是 wb（全新正文），
        # 不论这次响应有没有给校验子，都要用这次的结果无条件覆盖 current_state["validator"]，
        # 该清空就清空成 None。
        #
        # 第一轮必须真的写入部分正文后被真实暂停打断（而不是一次性吐完、成功之后
        # 才去检查校验子），否则测不出“校验子是在 open() 那一刻就被清空”还是“下载成功时
        # 才顺便清空”——后一种写法只要没暂停这条分支就永远不会被走到，以后有人把清空校验子
        # 误移到“下载成功”那一步，这条测试依然会绿。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        temp_path = f"{save_path}.tmp"
        old_version = os.urandom(5000)
        offset = 500
        with open(temp_path, "wb") as file:
            file.write(old_version[:offset])
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = '"old-etag"'
        state["downloaded_size"] = offset
        control = panel.BatchControl()
        state["control"] = control

        new_version = os.urandom(5000)
        calls: list[tuple] = []

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            calls.append((range_from, validator))
            if len(calls) == 1:
                # 第一轮：服务端回的是完整正文（200），没有给 ETag/Last-Modified 中的任何一个，
                # 真实写入 3 块新正文（1500 字节）之后被真的暂停打断——不是一次性吐完再暂停
                return SlicedResponse(new_version, chunk_size=500,
                                      trigger=control.pause_event.set, trigger_after_chunks=2), [url]
            # 第二轮：校验子已被清空，理应是不带 Range 的全新请求；一次性吐出完整正文
            return FakeRangeResponse(200, {"Content-Length": str(len(new_version))}, body=new_version), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state) # 第一轮：真实写入部分新正文后被暂停

            self.assertFalse(state["finished"]) # 真的被暂停了，没有走到成功分支
            self.assertIsNone(state["validator"]) # 校验子在 open() 那一刻就已经清空，不等下载完成
            self.assertEqual(Path(temp_path).read_bytes(), new_version[:1500]) # 3 块 * 500 字节
            self.assertEqual(state["downloaded_size"], 1500)

            control.pause_event.clear()
            panel.download_file(self.url, save_path, None, state) # 第二轮：真正的继续

        self.assertEqual(calls[1], (None, None)) # 校验子已清空：这次是全新请求，不带 Range/If-Range
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(Path(save_path).read_bytes(), new_version)
        self.assertFalse(Path(temp_path).exists())

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

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
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
        # 起点不匹配的 206——响应体只是那一段，一旦被当整份写下去就是静默损坏
        # （文件存在、大小和计数器都对得上、内容却是错的）。必须断言最终文件的字节，
        # 只断言 open_mode/计数器钉不住这个问题：落回 wb 之后计数器本身依然自洽，
        # 唯独磁盘上的字节是错的，只有比对实际内容才能揭穿。
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

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
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

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            calls.append((range_from, validator))
            return (unparseable_response if len(calls) == 1 else FreshFullResponse()), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertEqual(calls, [(11, '"old-etag"'), (None, None)])
        self.assertTrue(unparseable_response.closed)
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(Path(save_path).read_bytes(), full_content)

    def test_416_on_the_first_mirror_still_falls_back_to_a_full_restart(self) -> None:
        # 走真实的 request_download（不打桩），因为要测的正是它内部的镜像轮换：
        # r1 明确回答“是你的范围有问题”，r2/r3 恰好在闹别扭回 500。416 之后若继续轮换，
        # 返回给 plan_download_write 的就只剩那个 500，“不带 Range 重来一次”的回退路径
        # 根本不走，任务判失败、半截 .tmp 被删——而对 r1 发一条不带 Range 的请求本来就会成功。
        url = "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf"
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        temp_path = f"{save_path}.tmp"
        full_content = b"P" * 4000
        with open(temp_path, "wb") as file: # 上一轮留下的半截其实已经满长，续传必然越界
            file.write(full_content)
        state = panel.create_download_state(url, save_path)
        state["validator"] = '"v1"'
        requests_sent: list[tuple[str, bool]] = []

        def fake_get(request_url: str, headers: dict | None = None, **kwargs) -> object:
            has_range = "Range" in (headers or {})
            requests_sent.append((request_url, has_range))
            if not has_range:
                return FakeRangeResponse(200, {"Content-Length": str(len(full_content)), "ETag": '"v1"'}, body=full_content)
            if request_url.startswith("https://r1-"):
                return FakeRangeResponse(416, {"Content-Range": f"bytes */{len(full_content)}"})
            return FakeRangeResponse(500) # r2/r3 恰好在闹别扭

        self.context.enter_context(patch.object(panel, "_MIN_REQUEST_INTERVAL", 0))
        self.context.enter_context(patch.object(panel, "request_headers", lambda request_url: {}))
        self.context.enter_context(patch.object(panel, "session", Mock(get=fake_get)))

        panel.download_file(url, save_path, None, state)

        # 恰好两条：一条带 Range 的（r1，撞 416 就此打住，不再去打 r2/r3），一条不带 Range 的回退
        self.assertEqual([has_range for _request_url, has_range in requests_sent], [True, False])
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertEqual(Path(save_path).read_bytes(), full_content)
        self.assertFalse(Path(temp_path).exists())

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

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
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
        # 请求还在飞的时候用户点了暂停，随后服务端偏偏回了非 ok 状态码。
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

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            control.pause_event.set() # 请求在飞时用户点了暂停，响应此刻还没返回
            return FailingResponse(), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertFalse(state["finished"]) # 暂停契约：finished 必须是 False，留给“继续”重试
        self.assertIsNone(state["failed_reason"]) # 不能被判成 HTTP 503 真失败
        self.assertFalse(Path(f"{save_path}.tmp").exists()) # 从未写过任何字节，不该凭空产生 .tmp

    def test_cancel_in_flight_with_non_ok_response_is_not_treated_as_a_real_failure(self) -> None:
        # 与上面暂停那条对称——请求还在飞的时候用户点了取消，随后服务端偏偏回了非 ok
        # 状态码。取消同样不该被判成真失败，否则进度汇总会把这个被取消的任务算成“1 个失败”。
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

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            control.cancel_event.set() # 请求在飞时用户点了取消，响应此刻还没返回
            return FailingResponse(), [url]

        with patch.object(panel, "request_download", fake_request_download):
            panel.download_file(self.url, save_path, None, state)

        self.assertTrue(state["finished"]) # 取消契约：finished 必须是 True，任务终结
        self.assertIsNone(state["failed_reason"]) # 不能被判成 HTTP 503 真失败，不能算进失败计数
        self.assertEqual(state["downloaded_size"], 0)
        self.assertEqual(state["total_size"], 0)
        self.assertFalse(Path(f"{save_path}.tmp").exists()) # 从未写过任何字节，不该凭空产生 .tmp

    def test_pause_during_finalization_does_not_roll_back_to_a_resumable_state(self) -> None:
        # 这是“正文与校验子不同源”这个物种的第四个变种，藏在传输循环*之外*——
        # add_bookmarks 会把 .tmp 整份重写（字节内容、长度都变了，不再是服务端正文的前缀），
        # 紧接着的 os.replace 若失败（Windows 上目标文件被阅读器/杀软占用很常见），会走进
        # 外层的 except；此时如果 pause_event 恰好在加书签这几秒里被点了，不能不分青红皂白
        # 按 pause_event 分类成“暂停”，把这份已经不是服务端正文前缀的书签重写版 .tmp 留在
        # 磁盘上，state["validator"]/downloaded_size/total_size 却仍然描述着原始服务端正文。
        # 下一轮“继续”会用这份 offset（书签版的文件长度）+ 校验子（原始正文的）发起 Range 请求，
        # 只要服务端仍持有同一版本就会认可，把服务端正文的尾巴接到书签版前缀后面——又是长度
        # 自洽、无失败提示、内容错误的文件。
        #
        # 正确做法：一旦确认传输已经完整、进入“加书签 + 改名”这个收尾阶段，这个任务的下载
        # 本身就已经结束了，收尾阶段发生的暂停请求不能再把它回滚成“可续传”状态——.tmp 已经
        # 不再是服务端正文的前缀，没有“继续”这回事，只能判定为失败，清理掉这份不可信的 .tmp，
        # 逼下一次发起一次全新的下载。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = None
        control = panel.BatchControl()
        state["control"] = control

        server_content = os.urandom(2000)
        bookmarked_content = os.urandom(1600) # 模拟 pypdf 重写之后完全不同的字节与长度

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            return FakeRangeResponse(200, {"Content-Length": str(len(server_content)), "ETag": '"server-etag"'}, body=server_content), [url]

        def fake_add_bookmarks(pdf_path: str, chapters: list[dict]) -> None:
            Path(pdf_path).write_bytes(bookmarked_content) # 真书签会整份重写 .tmp
            control.pause_event.set() # 加书签这几秒里，用户点了暂停

        with patch.object(panel, "request_download", fake_request_download), \
             patch.object(panel, "add_bookmarks", fake_add_bookmarks), \
             patch.object(panel.os, "replace", side_effect=PermissionError("目标文件被占用")):
            panel.download_file(self.url, save_path, [{"title": "第一章", "page_index": 1}], state)

        self.assertTrue(state["finished"]) # 不能停在“暂停”这个可续传状态上——.tmp 已经不可信了
        self.assertIsNotNone(state["failed_reason"]) # 必须报告为失败，而不是悄悄假装暂停
        self.assertFalse(Path(f"{save_path}.tmp").exists()) # 不可信的书签重写版 .tmp 必须被清理掉
        self.assertFalse(Path(save_path).exists())

    def test_cancel_during_finalization_still_delivers_the_finished_file(self) -> None:
        # 与上一条互补：这里“加书签 + 改名”两步都顺利跑完。走到这一刻传输已经确认完整、
        # 书签也已经写成功，这是一份完好的成果——用户恰好在最后这两秒点了取消，不该把它删掉，
        # 那是在丢弃一份已经做完的工作。取消只回收尚未完整的半成品：文件照常改名交付，
        # finished 置为 True，且不写 failed_reason（取消不是失败）。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = None
        control = panel.BatchControl()
        state["control"] = control

        server_content = os.urandom(2000)
        bookmarked_content = server_content + b"%bookmarked%"

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            return FakeRangeResponse(200, {"Content-Length": str(len(server_content)), "ETag": '"server-etag"'}, body=server_content), [url]

        def fake_add_bookmarks(pdf_path: str, chapters: list[dict]) -> None:
            control.cancel_event.set() # 加书签这几秒里，用户点了取消
            Path(pdf_path).write_bytes(bookmarked_content)

        with patch.object(panel, "request_download", fake_request_download), \
             patch.object(panel, "add_bookmarks", fake_add_bookmarks):
            panel.download_file(self.url, save_path, [{"title": "第一章", "page_index": 1}], state)

        self.assertEqual(Path(save_path).read_bytes(), bookmarked_content) # 已经完整的成果照常交付
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        self.assertTrue(state["finished"])
        self.assertIsNone(state["failed_reason"]) # 取消不是失败

    def test_finalizing_must_be_set_before_calling_add_bookmarks_not_after(self) -> None:
        # 与“加书签成功、os.replace 失败”那条互补：那条的异常发生在 add_bookmarks 返回
        # 之后，测不出 finalizing = True 这一行是不是真的在调用 add_bookmarks 之前执行的
        # ——如果被挪到 add_bookmarks 调用之后（比如误以为“只有 os.replace 会失败，加书签
        # 本身失败与我无关”），那条用例依然全绿，测不出这个挪动。这里让 add_bookmarks 自身
        # 抛出异常（真实的 add_bookmarks 会把内部异常都吞掉，但这里是为了钉住“finalizing
        # 必须在调用它之前置位”这条时序规则本身，不依赖它是否真的会抛），异常发生在
        # finalizing 那一行“之后”还是“之前”，决定了这次暂停最终会被判成失败还是被误判成
        # 可续传的暂停。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = None
        control = panel.BatchControl()
        state["control"] = control

        server_content = os.urandom(2000)
        bookmarked_content = os.urandom(1600) # 模拟 pypdf 已经重写了一部分 .tmp 之后才失败

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            return FakeRangeResponse(200, {"Content-Length": str(len(server_content)), "ETag": '"server-etag"'}, body=server_content), [url]

        def fake_add_bookmarks(pdf_path: str, chapters: list[dict]) -> None:
            Path(pdf_path).write_bytes(bookmarked_content) # .tmp 已经不再是服务端正文的前缀
            control.pause_event.set() # 这几秒里用户点了暂停
            raise OSError("写书签失败") # add_bookmarks 自身抛出，不是 os.replace

        with patch.object(panel, "request_download", fake_request_download), \
             patch.object(panel, "add_bookmarks", fake_add_bookmarks):
            panel.download_file(self.url, save_path, [{"title": "第一章", "page_index": 1}], state)

        self.assertTrue(state["finished"]) # finalizing 必须在调用 add_bookmarks 之前就已置位
        self.assertIsNotNone(state["failed_reason"])
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        self.assertFalse(Path(save_path).exists())

    def test_finalizing_is_set_even_when_there_are_no_chapters_to_bookmark(self) -> None:
        # 没有勾选书签时 add_bookmarks 根本不会被调用，.tmp 仍是服务端正文的逐字节完整
        # 副本；但 finalizing 必须在“加书签 + 改名”这个收尾阶段一开始就统一置位，不能
        # 挪进 `if chapters:` 里只在有书签时才生效——否则这条用例（没有章节、os.replace
        # 失败、恰好命中暂停）会被误判成“暂停”而不是失败，留下一个看似可续传、实则下一轮
        # 发起 Range 请求必然撞 416（offset 已经等于全长）的死状态，且用户会一直看到
        # “已暂停”而不是失败提示。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        state["validator"] = None
        control = panel.BatchControl()
        state["control"] = control

        server_content = os.urandom(2000)

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            return FakeRangeResponse(200, {"Content-Length": str(len(server_content)), "ETag": '"server-etag"'}, body=server_content), [url]

        def failing_replace(src: str, dst: str) -> None:
            control.pause_event.set() # os.replace 这一刻恰好被要求暂停
            raise PermissionError("目标文件被占用")

        with patch.object(panel, "request_download", fake_request_download), \
             patch.object(panel.os, "replace", side_effect=failing_replace):
            panel.download_file(self.url, save_path, None, state) # chapters=None，不涉及 add_bookmarks

        self.assertTrue(state["finished"]) # 不能落成“暂停”——没有章节也不例外
        self.assertIsNotNone(state["failed_reason"])
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        self.assertFalse(Path(save_path).exists())

    def test_pause_requested_right_before_a_clean_stream_end_preserves_the_partial_file(self) -> None:
        # 暂停恰好撞上流干净结束（不抛异常）。循环内 break 用的检查不会再被沿用到
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

    def test_pause_exactly_at_full_length_completes_instead_of_staying_paused(self) -> None:
        # 暂停恰好落在最后一块之后——文件其实已经下完，不该判成“暂停”，否则不会改名，
        # 继续时会因为 offset == total_size 触发 416，退化成一次没有必要的全量重下。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        full_content = b"x" * 512

        class PauseAtEofResponse:
            ok = True
            status_code = 200
            headers = {"Content-Length": str(len(full_content))}

            def iter_content(self, **kwargs) -> object:
                yield full_content
                # 消费者写完最后一块、查过 stop_reason()（此时还没暂停）之后才会回来问要下一项；
                # 用户恰好在这段时间点了暂停，随后连接被关掉，迭代干净结束。
                control.pause_event.set()
                return

            def close(self) -> None:
                pass

        with patch.object(panel, "request_download", return_value=(PauseAtEofResponse(), [self.url])):
            panel.download_file(self.url, save_path, None, state)

        self.assertTrue(state["finished"]) # 按完成处理，不是暂停
        self.assertIsNone(state["failed_reason"])
        self.assertEqual(state["downloaded_size"], len(full_content))
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        self.assertEqual(Path(save_path).read_bytes(), full_content)

    def test_cancel_exactly_at_full_length_completes_instead_of_discarding_the_file(self) -> None:
        # 与上一条对称：取消恰好落在最后一块之后——同一时刻、同一磁盘状态，只是按下的按钮不同。
        # 文件其实已经下完，删掉它就是在丢弃一份已经做完的工作；取消只回收尚未下满的半成品。
        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)
        control = panel.BatchControl()
        state["control"] = control
        full_content = b"x" * 512

        class CancelAtEofResponse:
            ok = True
            status_code = 200
            headers = {"Content-Length": str(len(full_content))}

            def iter_content(self, **kwargs) -> object:
                yield full_content
                # 消费者写完最后一块、查过 stop_reason()（此时还没取消）之后才会回来问要下一项；
                # 用户恰好在这段时间点了取消，随后连接被关掉，迭代干净结束。
                control.cancel_event.set()
                return

            def close(self) -> None:
                pass

        with patch.object(panel, "request_download", return_value=(CancelAtEofResponse(), [self.url])):
            panel.download_file(self.url, save_path, None, state)

        self.assertTrue(state["finished"]) # 按完成处理，不是取消清理
        self.assertIsNone(state["failed_reason"]) # 取消不算失败
        self.assertEqual(state["downloaded_size"], len(full_content))
        self.assertFalse(Path(f"{save_path}.tmp").exists())
        self.assertEqual(Path(save_path).read_bytes(), full_content) # 已经完整的成果照常交付

    def test_cancel_without_a_content_length_still_discards_the_partial_file(self) -> None:
        # 反向约束，防止上一条改过头：服务端没给 Content-Length 时 total_size 为 0，
        # 无从判定这份 .tmp 是不是完整的，就不能拿“下满了”当借口交付，仍按半成品清理。
        # 写过若干字节和一个字节都没写，两种都要按半成品处理——后者的 downloaded_size
        # 恰好也等于 total_size（都是 0），正是“下满了”这个判据必须带上 total_size > 0
        # 的原因：少了它，一次什么都没收到的取消会交付一个零字节文件还判成功。
        for written in (512, 0):
            with self.subTest(written=written):
                save_path = str(Path(self.tmp_dir) / f"book-{written}.pdf")
                state = panel.create_download_state(self.url, save_path)
                control = panel.BatchControl()
                state["control"] = control

                class UnknownLengthResponse:
                    ok = True
                    status_code = 200
                    headers: dict = {} # 没有 Content-Length

                    def iter_content(self, **kwargs) -> object:
                        if written:
                            yield b"x" * written
                        control.cancel_event.set()
                        return

                    def close(self) -> None:
                        pass

                with patch.object(panel, "request_download", return_value=(UnknownLengthResponse(), [self.url])):
                    panel.download_file(self.url, save_path, None, state)

                self.assertTrue(state["finished"])
                self.assertIsNone(state["failed_reason"]) # 取消不算失败
                self.assertEqual(state["total_size"], 0)
                self.assertFalse(Path(f"{save_path}.tmp").exists()) # 长度未知：仍是半成品，照常清理
                self.assertFalse(Path(save_path).exists())

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
        # 暂停留下 .tmp 后点“继续”，任务在取得执行机会前又被取消——排队取消分支
        # 必须清理这个“上一轮留下的” .tmp，不能只按“这次有没有产生过” .tmp 来判断该不该
        # 清理：续传场景下，进入这一轮之前就可能已经预先存在一个 .tmp，不是每次都是全新任务。
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
        # plan_download_write 对非 ok 响应的早退不会归零，download_file 必须在这里补上，
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


class BookmarkFailureStillDeliversAnIntactPdfTest(unittest.TestCase):
    """书签写入中途失败时，这次下载从用户视角看仍然是成功的：交付一份完好的、只是没有书签的 PDF。"""

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

    def test_bookmark_write_failure_delivers_the_downloaded_pdf_without_bookmarks(self) -> None:
        # 端到端：走真实的 add_bookmarks（不打桩），让写书签在中途失败。几十 MB 已经完整下完的
        # 正文不能因为一次书签写入的小故障被整个丢弃，也不能交付一份被截断的半截 PDF——
        # 最终必须是一份能被 PdfReader 打开的完好 PDF，failed_reason 为 None。
        writer = PdfWriter()
        for _ in range(5):
            writer.add_blank_page(width=200, height=200)
        buffer = io.BytesIO()
        writer.write(buffer)
        server_content = buffer.getvalue()

        save_path = str(Path(self.tmp_dir) / "book.pdf")
        state = panel.create_download_state(self.url, save_path)

        def fake_request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: panel.BatchControl | None = None):
            return FakeRangeResponse(200, {"Content-Length": str(len(server_content))}, body=server_content), [url]

        def failing_write(self_writer, stream) -> None: # 先写出若干字节，再抛异常
            stream.write(b"%PDF-1.7\n" + b"0" * 4096)
            stream.flush()
            raise OSError("No space left on device")

        with patch.object(panel, "request_download", fake_request_download), \
             patch.object(bookmarks.PdfWriter, "write", failing_write):
            panel.download_file(self.url, save_path, [{"title": "第一章", "page_index": 1}], state)

        self.assertTrue(state["finished"])
        self.assertIsNone(state["failed_reason"]) # 用户视角：这次下载仍然成功，只是没有书签
        self.assertEqual(Path(save_path).read_bytes(), server_content) # 交付的就是服务端正文本身
        with open(save_path, "rb") as file:
            self.assertEqual(len(PdfReader(file).pages), 5) # 且确实是一份能打开的完好 PDF
        self.assertEqual({entry.name for entry in Path(self.tmp_dir).iterdir()}, {"book.pdf"}) # 不留任何临时文件


class WaitOrStopTest(unittest.TestCase):
    """可被唤醒的等待：判据完全由 cancel_event/pause_event 派生，不依赖第三个需要同步维护的标志。"""

    def test_returns_false_after_waiting_out_the_full_timeout(self) -> None:
        control = panel.BatchControl()
        started_at = time.monotonic()

        self.assertFalse(control.wait_or_stop(0.2))

        self.assertGreaterEqual(time.monotonic() - started_at, 0.2) # 没人叫停就得老老实实等满

    def test_returns_immediately_when_stop_was_already_requested(self) -> None:
        for event_name in ("cancel_event", "pause_event"):
            with self.subTest(event=event_name):
                control = panel.BatchControl()
                getattr(control, event_name).set()
                started_at = time.monotonic()

                self.assertTrue(control.wait_or_stop(5))

                self.assertLess(time.monotonic() - started_at, 1)

    def test_wakes_up_while_waiting(self) -> None:
        for event_name in ("cancel_event", "pause_event"): # 暂停和取消都要能叫醒，不能只认取消
            with self.subTest(event=event_name):
                control = panel.BatchControl()
                timer = threading.Timer(0.1, getattr(control, event_name).set)
                timer.start()
                self.addCleanup(timer.cancel)
                started_at = time.monotonic()

                self.assertTrue(control.wait_or_stop(5))

                self.assertLess(time.monotonic() - started_at, 2)

    def test_stop_requested_reads_both_events(self) -> None:
        control = panel.BatchControl()
        self.assertFalse(control.stop_requested())
        control.pause_event.set()
        self.assertTrue(control.stop_requested())
        control.pause_event.clear()
        control.cancel_event.set()
        self.assertTrue(control.stop_requested())


class StopBeforeResponseHeadersTest(unittest.TestCase):
    """响应头到达之前的那段等待——镜像轮换与 400 退避——同样要能被取消/暂停打断。

    没有这些检查点时，一次停止最坏要等“镜像数 × 每个镜像的退避次数”条请求全部走完；
    有了之后上界只剩当前这一条在飞的请求，停止之后不会再打出任何一条新请求。
    """

    PARTIAL = b"HEAD" * 25 # 上一轮暂停留下的半截内容
    REST = b"TAIL" * 25

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
        self.context.enter_context(patch.object(panel, "request_headers", lambda url: {}))
        self.context.enter_context(patch.object(panel, "_MIN_REQUEST_INTERVAL", 0)) # 别把限流间隔混进被测的等待
        # 退避设得远长于下面的 join 超时：等待真能被唤醒时工作线程立刻退出，测试根本不会睡满这几秒；
        # 唤醒机制一旦失效，表现是 join 超时，而不是让测试真的等上十几秒。
        self.context.enter_context(patch.object(panel, "_400_RETRY_DELAYS", (5.0, 5.0)))
        self.url = "https://r1-ndr-private.ykt.cbern.com.cn/book.pdf" # 私有 CDN 才有 r1/r2/r3 三个镜像
        self.save_path = str(Path(self.tmp_dir) / "book.pdf")
        self.temp_path = f"{self.save_path}.tmp"

    def state_with_a_paused_leftover(self) -> tuple[dict, panel.BatchControl]:
        """造一个“上一轮暂停留下半截 .tmp”的任务：这半截和它的校验子是续传的全部依据。"""
        with open(self.temp_path, "wb") as file:
            file.write(self.PARTIAL)
        state = panel.create_download_state(self.url, self.save_path)
        state["validator"] = '"etag-v1"'
        control = panel.BatchControl()
        state["control"] = control
        panel.download_states.append(state)
        return state, control

    def run_until_stopped(self, state: dict, fake_get, stop) -> list[str]:
        """在工作线程里跑 download_file，等第一条请求确实发出之后再发停止请求。"""
        first_request = threading.Event()
        requested_urls: list[str] = []

        def recording_get(url: str, **kwargs) -> object:
            requested_urls.append(url)
            first_request.set()
            return fake_get(url, **kwargs)

        with patch.object(panel, "session", Mock(get=recording_get)):
            worker = threading.Thread(target=panel.download_file, args=(self.url, self.save_path, None, state), daemon=True)
            worker.start()
            self.assertTrue(first_request.wait(timeout=5), "第一条请求都没发出，测试前置条件不成立")
            time.sleep(0.1) # 让工作线程真的进到那段等待里，而不是停在等待之前
            stop()
            worker.join(timeout=2) # 远小于被测的 5 秒退避：没停下就说明那段等待打不断

        self.assertFalse(worker.is_alive(), "停止请求没能打断响应头到达之前的那段等待")
        return requested_urls

    def assert_resumes_to_completion(self, state: dict, control: panel.BatchControl) -> None:
        """暂停留下的半截确实能被“继续”接着下完，而不只是名义上 finished == False。"""
        control.pause_event.clear()
        full_content = self.PARTIAL + self.REST
        resumed = SlicedResponse(full_content, start=len(self.PARTIAL))

        with patch.object(panel, "request_download", return_value=(resumed, [self.url])):
            panel.download_file(self.url, self.save_path, None, state)

        self.assertTrue(state["finished"])
        self.assertIsNone(state["failed_reason"])
        self.assertEqual(Path(self.save_path).read_bytes(), full_content)

    def test_cancel_during_400_backoff_stops_before_the_next_request(self) -> None:
        state, control = self.state_with_a_paused_leftover()

        requested_urls = self.run_until_stopped(
            state,
            lambda url, **kwargs: FakeRangeResponse(400), # 私有 CDN 限流：同地址退避重试
            control.cancel_event.set,
        )

        self.assertEqual(requested_urls, [self.url]) # 取消之后不再打出任何一条请求
        self.assertIsNone(state["failed_reason"]) # 取消不是下载失败
        self.assertTrue(state["finished"])
        self.assertFalse(Path(self.temp_path).exists()) # 取消回收尚未完整的半成品

    def test_pause_during_400_backoff_keeps_the_partial_file_resumable(self) -> None:
        state, control = self.state_with_a_paused_leftover()

        requested_urls = self.run_until_stopped(
            state,
            lambda url, **kwargs: FakeRangeResponse(400),
            control.pause_event.set,
        )

        self.assertEqual(requested_urls, [self.url])
        self.assertIsNone(state["failed_reason"])
        self.assertFalse(state["finished"]) # 暂停：留给“继续”重新提交
        self.assertEqual(Path(self.temp_path).read_bytes(), self.PARTIAL) # 半截原样保留
        self.assertEqual(state["validator"], '"etag-v1"') # 校验子没被这一轮动过，仍与磁盘同源
        self.assert_resumes_to_completion(state, control)

    def make_held_mirror_request(self, release: threading.Event):
        """把第一个镜像的请求卡住，让停止请求确定落在“换下一个镜像之前”那个检查点上。"""
        def fake_get(url: str, **kwargs) -> object:
            self.assertTrue(release.wait(timeout=5), "第一个镜像的请求一直没被放行")
            return FakeRangeResponse(500) # 500 会换下一个镜像，不走 400 的同地址退避
        return fake_get

    def test_cancel_between_mirrors_stops_before_trying_the_next_mirror(self) -> None:
        state, control = self.state_with_a_paused_leftover()
        release = threading.Event()

        def stop() -> None:
            control.cancel_event.set()
            release.set() # 放行第一个镜像，让它带着“已经要停了”回到轮换处

        requested_urls = self.run_until_stopped(state, self.make_held_mirror_request(release), stop)

        self.assertEqual(requested_urls, [self.url]) # r2/r3 一个都不该再打
        self.assertIsNone(state["failed_reason"])
        self.assertTrue(state["finished"])
        self.assertFalse(Path(self.temp_path).exists())

    def test_pause_between_mirrors_keeps_the_partial_file_resumable(self) -> None:
        state, control = self.state_with_a_paused_leftover()
        release = threading.Event()

        def stop() -> None:
            control.pause_event.set()
            release.set()

        requested_urls = self.run_until_stopped(state, self.make_held_mirror_request(release), stop)

        self.assertEqual(requested_urls, [self.url])
        self.assertIsNone(state["failed_reason"])
        self.assertFalse(state["finished"])
        self.assertEqual(Path(self.temp_path).read_bytes(), self.PARTIAL)
        self.assertEqual(state["validator"], '"etag-v1"')
        self.assert_resumes_to_completion(state, control)

    def test_request_download_without_a_control_still_retries_and_rotates(self) -> None:
        # 不传 control 时行为与新增检查点之前逐字一致：400 在同地址退避重试满，500 继续换镜像。
        self.context.enter_context(patch.object(panel, "_400_RETRY_DELAYS", (0, 0)))
        backoff_session = FakeHeaderRecordingSession([FakeRangeResponse(400)])
        with patch.object(panel, "session", backoff_session):
            response, attempted_urls = panel.request_download(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(attempted_urls, [self.url]) # 400 不换镜像
        self.assertEqual(len(backoff_session.requested_urls), 3) # 首次 + 2 次退避重试

        rotation_session = FakeHeaderRecordingSession([FakeRangeResponse(500)])
        with patch.object(panel, "session", rotation_session):
            _response, rotated_urls = panel.request_download(self.url)
        self.assertEqual(len(rotated_urls), 3) # 500 依次走满 r1/r2/r3

    def test_stop_during_the_pacing_wait_sends_no_request_at_all(self) -> None:
        # 限流间隔被几个工作线程争用时会叠起来，停止请求完全可能落在这段等待里。醒来后必须
        # 再看一眼：这条请求一旦发出，响应头到达之前既不在 active_responses 里、也没有别的
        # 检查点，只能等满 REQUEST_TIMEOUT。把 _pace_request 打桩成可控的慢等待，不依赖真实时序。
        # 取消和暂停都要跑一遍：这个检查点读的是“是否已请求停止”，不是“是否已请求取消”。
        for event_name in ("cancel_event", "pause_event"):
            with self.subTest(event=event_name):
                state, control = self.state_with_a_paused_leftover()
                entered_pacing = threading.Event()
                release_pacing = threading.Event()
                requested_urls: list[str] = []

                def blocking_pace() -> None:
                    entered_pacing.set()
                    self.assertTrue(release_pacing.wait(timeout=5), "限流等待一直没被放行")

                def fake_get(url: str, **kwargs) -> object:
                    requested_urls.append(url)
                    return FakeRangeResponse(200, {"Content-Length": "0"})

                with patch.object(panel, "_pace_request", blocking_pace), \
                     patch.object(panel, "session", Mock(get=fake_get)):
                    worker = threading.Thread(target=panel.download_file, args=(self.url, self.save_path, None, state), daemon=True)
                    worker.start()
                    self.assertTrue(entered_pacing.wait(timeout=5), "没能进到限流等待里，测试前置条件不成立")
                    getattr(control, event_name).set() # 用户就在这一小段等待里点了取消/暂停
                    release_pacing.set()
                    worker.join(timeout=5)

                self.assertFalse(worker.is_alive(), "停止请求没能在限流等待之后把这条请求拦下来")
                self.assertEqual(requested_urls, []) # 明知要停，一条请求都不该再发出去
                self.assertIsNone(state["failed_reason"]) # 停止不是下载失败

                if event_name == "cancel_event":
                    self.assertTrue(state["finished"])
                    self.assertFalse(Path(self.temp_path).exists()) # 取消回收尚未完整的半成品
                else:
                    self.assertFalse(state["finished"]) # 暂停：留给“继续”重新提交
                    self.assertEqual(Path(self.temp_path).read_bytes(), self.PARTIAL) # 半截原样保留
                    self.assertEqual(state["validator"], '"etag-v1"') # 校验子没被这一轮动过，仍与磁盘同源
                    self.assert_resumes_to_completion(state, control)

    def test_stop_between_mirrors_is_caught_before_the_pacing_wait(self) -> None:
        # 换镜像之前那个检查点单独的价值：停止已经置位时，下一个镜像连限流等待都不该进，
        # 而不是先白等一轮再被 session.get 之前的复查拦下来。
        control = panel.BatchControl()
        paced = []
        requested_urls: list[str] = []

        def fake_get(url: str, **kwargs) -> object:
            requested_urls.append(url)
            control.cancel_event.set() # 第一个镜像的请求在飞时用户点了取消
            return FakeRangeResponse(500) # 500 会继续换镜像

        self.context.enter_context(patch.object(panel, "_pace_request", lambda: paced.append(1)))
        self.context.enter_context(patch.object(panel, "session", Mock(get=fake_get)))

        with self.assertRaises(panel.BatchStopped):
            panel.request_download(self.url, control=control)

        self.assertEqual(requested_urls, [self.url])
        self.assertEqual(len(paced), 1) # r2 连限流等待都没进

    def test_request_download_closes_the_useless_response_when_it_stops(self) -> None:
        # 命中停止时手上那个已经用不上的响应必须关掉，不能留着不管。
        control = panel.BatchControl()
        unusable = FakeRangeResponse(500)

        def fake_get(url: str, **kwargs) -> object:
            control.cancel_event.set() # 第一个镜像的请求在飞时用户点了取消
            return unusable

        self.context.enter_context(patch.object(panel, "session", Mock(get=fake_get)))

        with self.assertRaises(panel.BatchStopped):
            panel.request_download(self.url, control=control)

        self.assertTrue(unusable.closed)


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

    def test_pause_event_set_but_nothing_left_pending_settles_as_completed(self) -> None:
        # 暂停恰好落在最后一个文件的最后一块之后——批次里已经没有未完成任务了，
        # 不该因为 pause_event 还留着置位就落成“暂停”，否则界面会卡在“已暂停 100%”。
        control = panel.BatchControl()
        control.directory = self.directory
        control.pause_event.set()
        panel.download_states = [self.make_state(finished=True), self.make_state(finished=True)]
        panel._batch_control = control

        panel._run_batch_worker([], control)

        self.assertFalse(control.paused_settled) # 没有落成“暂停”
        self.assertIsNone(panel._batch_control) # 走的是完成分支，控制对象被清空
        self.notice.assert_called_once_with("下载完成", f"文件已下载到：{self.directory}")

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
        # _run_batch_worker 算出 outcome="paused" 之后、ui_call 排队的回调真正执行之前，
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

    def test_askdirectory_cancelled_also_resets_the_progress_bar(self) -> None:
        # 进度条/文案是否清空收口进 set_ui_phase——这条不打桩 set_ui_phase，直接看真实
        # 进度条/文案控件收到的调用。
        #
        # 注意：download() 入口的 set_ui_phase("parsing") 本身就会把进度条清零一次，
        # 如果不把这次入口调用排除掉，assert_any_call(value=0) 在“退出路径根本没有复位”
        # 时也会通过，测不出退出路径本身有没有复位。用户点掉 askdirectory 对话框的这一刻，
        # 才是真正要验证的“退出复位”时机，所以在桩里先清空调用记录，只断言这一刻之后
        # 发生的复位。
        resource_by_url = {
            f"https://example.com/{index}.pdf": ResourceInfo(f"教材{index}", f"https://example.com/{index}.pdf", "pdf", [])
            for index in range(2)
        }
        panel.url_text.get.return_value = "\n".join(resource_by_url)

        def cancelled_askdirectory() -> str:
            panel.download_progress_bar.config.reset_mock()
            panel.progress_label.config.reset_mock()
            return ""

        with patch.object(panel, "parse", side_effect=lambda url, bookmarks: [resource_by_url[url]]):
            with patch.object(panel.filedialog, "askdirectory", side_effect=cancelled_askdirectory):
                panel.download()

        panel.download_progress_bar.config.assert_any_call(value=0)
        panel.progress_label.config.assert_any_call(text="等待下载")

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
        self.context.enter_context(patch.object(panel, "download_states", []))
        self.context.enter_context(patch.object(panel, "_batch_control", None))
        self.context.enter_context(patch.object(panel, "_copy_parse_active", False)) # 这些用例会真的起一次复制解析
        for name in ("copy_btn", "url_text", "progress_label"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        self.context.enter_context(patch.object(panel.messagebox, "showwarning"))
        self.context.enter_context(patch.object(panel.messagebox, "showerror"))

    def test_should_stop_is_none_for_the_copy_flow(self) -> None:
        captured: dict[str, object] = {"called": False}

        def fake_parse_urls_in_background(urls, bookmarks, on_finished, should_stop=None) -> None:
            captured["called"] = True
            captured["should_stop"] = should_stop

        panel.url_text.get.return_value = "https://example.com/1"
        with patch.object(panel, "parse_urls_in_background", fake_parse_urls_in_background):
            panel.parse_and_copy()

        self.assertTrue(captured["called"])
        self.assertIsNone(captured["should_stop"]) # 唯一的实质断言：不能被顺手接上取消

    def test_copy_btn_is_not_re_enabled_while_a_download_batch_is_active(self) -> None:
        # copy_urls 不能无条件把 copy_btn 设回 normal：若这次“解析并复制”的完成回调
        # 恰好在下载流程自己的“解析中”阶段（_batch_control 不是 None）才触发，会把
        # set_ui_phase 刚设好的 disabled 状态抢回来，导致同一个按钮同时在做两件事。
        resource = ResourceInfo("教材", "https://example.com/parsed.pdf", "pdf", [])

        def fake_parse_urls_in_background(urls, bookmarks, on_finished, should_stop=None) -> None:
            panel._batch_control = panel.BatchControl() # 模拟下载流程的“解析中”阶段仍在进行
            on_finished([resource], set())

        panel.url_text.get.return_value = "https://example.com/1"
        with patch.object(panel, "parse_urls_in_background", fake_parse_urls_in_background):
            panel.parse_and_copy()

        self.assertNotIn(call(state="normal"), panel.copy_btn.config.call_args_list)

    def test_progress_label_not_overwritten_while_a_download_batch_is_active(self) -> None:
        # 同一个问题的另一半——紧跟着 copy_btn 那行的 progress_label.config(text="等待
        # 下载") 不能只看 downloads_active()（只反映“下载中”子阶段）：下载流程自己的“解析中”
        # 子阶段这时 download_states 还是空的，会被误判成“没有下载活动”，把“正在解析链接
        # 3/50”这样的文案覆盖掉。
        resource = ResourceInfo("教材", "https://example.com/parsed.pdf", "pdf", [])

        def fake_parse_urls_in_background(urls, bookmarks, on_finished, should_stop=None) -> None:
            panel._batch_control = panel.BatchControl() # 模拟下载流程的“解析中”阶段仍在进行
            on_finished([resource], set())

        panel.url_text.get.return_value = "https://example.com/1"
        with patch.object(panel, "parse_urls_in_background", fake_parse_urls_in_background):
            panel.parse_and_copy()

        self.assertNotIn(call(text="等待下载"), panel.progress_label.config.call_args_list)


class SetUiPhaseProgressResetTest(unittest.TestCase):
    """进度条/文案是否清空收口进 set_ui_phase，不由各个调用方各写一份。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))

    def test_idle_resets_progress_bar_and_label(self) -> None:
        panel.set_ui_phase("idle")

        panel.download_progress_bar.config.assert_any_call(value=0)
        panel.progress_label.config.assert_any_call(text="等待下载")

    def test_parsing_resets_the_progress_bar_but_leaves_the_label_to_show_parse_progress(self) -> None:
        panel.set_ui_phase("parsing")

        panel.download_progress_bar.config.assert_any_call(value=0)
        for recorded_call in panel.progress_label.config.call_args_list:
            self.assertNotIn("text", recorded_call.kwargs) # 解析进度文案交给 show_parse_progress，这里不写死

    def test_downloading_and_paused_do_not_touch_the_progress_widgets(self) -> None:
        panel.set_ui_phase("downloading")
        panel.download_progress_bar.config.assert_not_called()
        panel.progress_label.config.assert_not_called()

        panel.set_ui_phase("paused")
        panel.download_progress_bar.config.assert_not_called()
        panel.progress_label.config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
