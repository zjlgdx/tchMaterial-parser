# 工作线程 → 主线程的界面更新投递。
# Tk 9 下工作线程里的 root.after_idle 拿得到编号却永远不执行，所以这里的重点是
# 「线程投递的回调最终被主线程执行」，以及主线程投递保持原有的即时时序。
import queue
import sys
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tchmaterial_parser.ui import runtime


UI_POLL_SECONDS = runtime.UI_QUEUE_POLL_MS / 1000 * 3 # 足够走完几轮轮询


class UiCallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
        except tk.TclError as error:
            if "no display name" in str(error) or "couldn't connect to display" in str(error):
                raise unittest.SkipTest(f"当前环境没有图形显示服务：{error}") from error
            raise
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def setUp(self) -> None:
        self.previous_root = getattr(runtime, "root", None)
        runtime.root = self.root
        runtime.app_closing = False
        runtime._ui_queue = queue.Queue() # 每条用例从空队列开始，免得互相看到对方的残留
        runtime._pump_after_id = None
        self.addCleanup(self.restore_runtime)

        self.errors: list[BaseException] = []
        self.previous_reporter = self.root.report_callback_exception
        self.root.report_callback_exception = lambda _type, error, _traceback: self.errors.append(error)

    def restore_runtime(self) -> None:
        runtime.app_closing = False
        self.root.report_callback_exception = self.previous_reporter
        if runtime._pump_after_id is not None:
            self.root.after_cancel(runtime._pump_after_id)
            runtime._pump_after_id = None
        if self.previous_root is not None:
            runtime.root = self.previous_root

    def drain(self, done: threading.Event, timeout: float = 5.0) -> bool: # 转主循环直到事件置位或超时
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if done.is_set():
                return True
            time.sleep(0.005)
        return done.is_set()

    def test_worker_thread_delivery_reaches_the_main_thread(self) -> None:
        done = threading.Event()
        seen: list[tuple] = []

        def record(*args, **kwargs) -> None:
            seen.append((threading.current_thread() is threading.main_thread(), args, kwargs))
            done.set()

        runtime.start_ui_pump()
        threading.Thread(target=lambda: runtime.ui_call(record, 1, "二", key="值"), daemon=True).start()

        self.assertTrue(self.drain(done), "工作线程投递的回调没有在主线程被执行")
        self.assertEqual(seen, [(True, (1, "二"), {"key": "值"})])

    def test_main_thread_delivery_runs_within_one_update(self) -> None:
        # 既有测试依赖这条时序：主线程投递后调一次 update() 就应看到效果，不能退化成要等下一轮轮询
        seen: list[int] = []

        runtime.ui_call(seen.append, 7)
        self.root.update()

        self.assertEqual(seen, [7])

    def test_callback_error_reaches_the_reporter_without_stopping_the_pump(self) -> None:
        def boom() -> None:
            raise ValueError("回调故意失败")

        seen: list[str] = []
        runtime.ui_call(boom)
        runtime.ui_call(seen.append, "之后的回调")
        self.root.update()

        self.assertEqual([type(error) for error in self.errors], [ValueError])
        self.assertEqual(seen, ["之后的回调"]) # 坏回调之后的回调仍要送达

    def test_delivery_order_is_preserved(self) -> None:
        seen: list[int] = []
        for index in range(5):
            runtime.ui_call(seen.append, index)
        self.root.update()

        self.assertEqual(seen, [0, 1, 2, 3, 4])

    def test_closing_drops_new_deliveries_and_stops_the_pump(self) -> None:
        seen: list[str] = []
        runtime.start_ui_pump()
        runtime.app_closing = True

        runtime.ui_call(seen.append, "关闭后投递")
        self.root.update()
        time.sleep(UI_POLL_SECONDS)
        self.root.update()

        self.assertEqual(seen, [])
        self.assertIsNone(runtime._pump_after_id) # 轮询不再重新排期

    def test_budget_yields_and_the_rest_is_delivered_on_the_next_poll(self) -> None:
        # 单轮只在预算内执行，剩下的留给下一轮，避免一次投递洪峰把重绘饿死
        seen: list[int] = []

        def slow(index: int) -> None:
            seen.append(index)
            time.sleep(runtime.UI_QUEUE_BUDGET_MS / 1000)

        for index in range(3):
            runtime._ui_queue.put((slow, (index,), {}))

        runtime._pump()
        self.assertEqual(len(seen), 1)

        runtime._pump()
        runtime._pump()
        self.assertEqual(seen, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
