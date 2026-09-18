# 工作线程 → 主线程的界面更新投递。
# Tk 9 下工作线程里的 root.after_idle 拿得到编号却永远不执行，所以这里的重点是
# 「线程投递的回调最终被主线程执行」，以及主线程投递保持原有的即时时序。
import queue
import sys
import threading
import time
import tkinter as tk
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.tchmaterial_parser.ui import runtime


class RecordingRoot: # 记录调度调用的假主窗口：不需要 Tk，也就不受 Tk 版本与有无桌面影响
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, str]] = []

    def _record(self, name: str) -> str:
        self.scheduled.append((name, threading.current_thread().name))
        return "after#0"

    def after(self, *_args: object) -> str:
        return self._record("after")

    def after_idle(self, *_args: object) -> str:
        return self._record("after_idle")

    def after_cancel(self, *_args: object) -> None:
        self._record("after_cancel")


class WorkerThreadSchedulingTest(unittest.TestCase):
    """跨线程投递的机制护栏。

    这一条要能在任何实现上跑起来才有意义，所以既不开窗口，也不碰投递机制的内部符号：
    只用假主窗口看「工作线程有没有去动 Tk 的定时器」。
    """

    def setUp(self) -> None:
        self.addCleanup(self.drop_queued_tasks)

    def drop_queued_tasks(self) -> None: # 本用例投进去的任务不留给别的用例
        pending = getattr(runtime, "_ui_queue", None)
        while pending is not None and not pending.empty():
            pending.get_nowait()

    def test_worker_thread_delivery_never_touches_the_tk_scheduler(self) -> None:
        # Tk 9 下工作线程排进去的回调拿得到编号却永远不执行，没人唤醒主循环。
        # 跨线程投递因此只能入队列，交给主线程轮询取走。
        fake_root = RecordingRoot()
        worker_name = "投递线程"

        with patch.object(runtime, "root", fake_root, create=True), patch.object(runtime, "app_closing", False):
            worker = threading.Thread(target=lambda: runtime.ui_call(len, "x"), name=worker_name)
            worker.start()
            worker.join(5)
        self.assertFalse(worker.is_alive())

        from_worker = [call for call in fake_root.scheduled if call[1] == worker_name]
        self.assertEqual(
            from_worker, [],
            "工作线程不得调用 root.after / root.after_idle：Tk 9 下这样排进去的回调永远不会被执行，"
            f"跨线程投递必须只入队列。实际发生了 {from_worker}",
        )
        self.assertEqual(
            getattr(runtime, "_ui_queue").qsize(), 1,
            "工作线程投递的任务必须留在队列里等主线程轮询取走，队列却是空的",
        )


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
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        enter = self.context.enter_context
        enter(patch.object(runtime, "root", self.root, create=True))
        enter(patch.object(runtime, "app_closing", False))
        enter(patch.object(runtime, "_ui_queue", queue.Queue())) # 每条用例从空队列开始，免得互相看到对方的残留
        enter(patch.object(runtime, "_pump_after_id", None))
        self.addCleanup(self.cancel_pending_pump) # 先于上面的还原执行，此时读到的还是本用例排下的定时器

        self.errors: list[BaseException] = []
        enter(patch.object(self.root, "report_callback_exception", lambda _type, error, _traceback: self.errors.append(error)))

    def cancel_pending_pump(self) -> None: # 不把轮询定时器留给下一条用例
        if runtime._pump_after_id is not None:
            self.root.after_cancel(runtime._pump_after_id)

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
        time.sleep(runtime.UI_QUEUE_POLL_MS / 1000 * 3) # 足够走完几轮轮询
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
