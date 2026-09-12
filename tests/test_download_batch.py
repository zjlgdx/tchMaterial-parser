from pathlib import Path
from contextlib import ExitStack
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, call, patch

from src.tchmaterial_parser.api import ResourceInfo
from src.tchmaterial_parser.ui import download_panel as panel


class DownloadBatchTest(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.callbacks = queue.Queue()
        self.threads = []
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.directory = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        self.context.enter_context(patch.object(panel, "download_states", []))
        self.context.enter_context(patch.object(panel, "_batch_control", None))
        for name in ("progress_label", "download_progress_bar", "download_btn", "copy_btn"):
            self.context.enter_context(patch.object(panel, name, Mock(), create=True))
        self.notice = self.context.enter_context(patch.object(panel.messagebox, "showinfo"))
        self.warning = self.context.enter_context(patch.object(panel.messagebox, "showwarning"))
        self.context.enter_context(patch.object(panel, "ui_call", lambda fn, *args, **kwargs: self.callbacks.put((fn, args, kwargs))))

        def thread_it(fn):
            thread = threading.Thread(target=fn, daemon=True)
            self.threads.append(thread)
            thread.start()

        self.context.enter_context(patch.object(panel, "thread_it", thread_it))

    def targets(self, count):
        return [(ResourceInfo(f"教材{index}", f"https://example.com/{index}.pdf", "pdf", []), str(Path(self.directory) / f"教材{index}.pdf")) for index in range(count)]

    def finish(self):
        for thread in self.threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "批次线程没有退出")
        while not self.callbacks.empty():
            fn, args, kwargs = self.callbacks.get_nowait()
            fn(*args, **kwargs)

    def test_all_tasks_registered_before_fast_failure_and_queued_work(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        observed = []

        def download(url, path, chapters, state):
            observed.append(len(panel.download_states))
            if url.endswith("/0.pdf"):
                state["failed_reason"] = "HTTP 404"
            else:
                entered.set()
                release.wait(timeout=5)
            state["finished"] = True

        with patch.object(panel, "download_file", download):
            panel.start_download_batch(self.targets(5), self.directory)
            # 批次一开始（start_download_batch 本身，同步）按钮就已经切到“取消”
            self.assertEqual(panel.download_btn.config.call_args, call(text="取消", state="normal", command=panel.cancel_current_batch))
            self.assertTrue(entered.wait(timeout=3))
            self.assertTrue(panel.downloads_active())
            self.assertTrue(self.callbacks.empty())
            self.assertEqual(panel.download_btn.config.call_count, 1) # 批次仍在进行，还没收到终态回调
            release.set()
            self.finish()

        self.assertEqual(observed, [5] * 5)
        self.warning.assert_called_once()
        self.notice.assert_not_called()
        # 批次结束后恢复空闲：文案、命令都要换回去，不只是 state
        self.assertEqual(panel.download_btn.config.call_args, call(text="下载", state="normal", command=panel.download))

    def test_concurrent_downloads_emit_one_batch_notice(self):
        barrier = threading.Barrier(2)

        class Response:
            ok = False
            status_code = 404
            content = b""

            def close(self):
                barrier.wait(timeout=3)

        with patch.object(panel, "request_download", side_effect=lambda url: (Response(), [url])):
            panel.start_download_batch(self.targets(2), self.directory)
            self.finish()

        self.warning.assert_called_once()
        self.assertFalse(panel.downloads_active())
        self.assertTrue(all(state["failed_reason"] for state in panel.download_states))

    def test_successful_batch_creates_subdirectories_and_reports_root(self):
        class Response:
            ok = True
            status_code = 200
            headers = {"Content-Length": "2"}

            def iter_content(self, **kwargs):
                yield b"ok"

            def close(self):
                pass

        targets = [(resource, str(Path(self.directory) / resource.title / "book.pdf")) for resource, _ in self.targets(2)]
        with patch.object(panel, "request_download", side_effect=lambda url: (Response(), [url])):
            panel.start_download_batch(targets, self.directory)
            self.finish()

        self.notice.assert_called_once_with("下载完成", f"文件已下载到：{self.directory}")
        self.warning.assert_not_called()
        for _, path in targets:
            self.assertEqual(Path(path).read_bytes(), b"ok")
            self.assertFalse(Path(f"{path}.tmp").exists())
