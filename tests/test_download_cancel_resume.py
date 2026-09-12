from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
import os
import tempfile
import unittest

from src.tchmaterial_parser.api import ResourceInfo
from src.tchmaterial_parser.ui import download_panel as panel


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

        self.assertIsNone(mocked.call_args.kwargs["range_from"])

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


if __name__ == "__main__":
    unittest.main()
