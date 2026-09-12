from contextlib import ExitStack
from unittest.mock import Mock, patch
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


if __name__ == "__main__":
    unittest.main()
