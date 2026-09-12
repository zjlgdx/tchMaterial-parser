# -*- coding: utf-8 -*-
# 真实 Tk 控件下验证 set_ui_phase：只用 Mock 控件只能证明“调用了 config”，
# 证不了按钮的文案、启用状态、点击后触发的命令是不是真的对——这里用真实 ttk.Button。

import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from src.tchmaterial_parser.ui import download_panel as panel


class SetUiPhaseRealWidgetsTest(unittest.TestCase):
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
        # 六个受 bind_widgets 管理的控件；只关心底部两个按钮，其余给最简单的真实控件即可。
        self.url_text = tk.Text(self.root)
        bookmark_var = tk.BooleanVar(self.root)
        self.download_btn = ttk.Button(self.root)
        self.copy_btn = ttk.Button(self.root)
        download_progress_bar = ttk.Progressbar(self.root)
        progress_label = ttk.Label(self.root)
        for widget in (self.url_text, self.download_btn, self.copy_btn, download_progress_bar, progress_label):
            self.addCleanup(widget.destroy)
        panel.bind_widgets(self.url_text, bookmark_var, self.download_btn, self.copy_btn, download_progress_bar, progress_label)
        # “解析并复制在飞”是模块级状态，逐条用例都从干净的初值起算，不受别处顺序影响
        flag_patch = patch.object(panel, "_copy_parse_active", False)
        flag_patch.start()
        self.addCleanup(flag_patch.stop)

    def start_a_copy_parse(self) -> list:
        """真的起一次“解析并复制”，但把解析线程截下来，让它停在“还没跑完”的状态。

        返回截下来的线程函数列表，调用其中的函数就等于让那次解析跑完并回到主线程。
        """
        workers: list = []
        self.url_text.insert("1.0", "https://example.com/book")
        with (
            patch.object(panel, "thread_it", lambda fn, *args, **kwargs: workers.append(fn)),
            patch.object(panel, "download_states", []),
        ):
            panel.parse_and_copy()
        self.root.update()
        self.assertEqual(str(self.copy_btn.cget("state")), "disabled", "parse_and_copy 自己就该先禁用按钮")
        return workers

    def finish_the_copy_parse(self, workers: list) -> None:
        """让那次解析跑完：解析结果一律算作失败链接，绕开剪贴板（无头环境下不可靠）。"""
        with (
            patch.object(panel, "ui_call", lambda fn, *args, **kwargs: fn(*args, **kwargs)),
            patch.object(panel, "parse", lambda url, bookmarks: None),
            patch.object(panel, "download_states", []),
            patch.object(panel.messagebox, "showwarning", lambda *args, **kwargs: None),
        ):
            workers[0]()
        self.root.update()

    def test_idle_phase_shows_download_and_parse_and_copy_both_enabled(self) -> None:
        panel.set_ui_phase("idle")
        self.root.update()

        self.assertEqual(self.download_btn.cget("text"), "下载")
        self.assertEqual(str(self.download_btn.cget("state")), "normal")
        self.assertEqual(self.copy_btn.cget("text"), "解析并复制")
        self.assertEqual(str(self.copy_btn.cget("state")), "normal")

    def test_parsing_phase_disables_copy_and_shows_cancel(self) -> None:
        panel.set_ui_phase("parsing")
        self.root.update()

        self.assertEqual(self.download_btn.cget("text"), "取消")
        self.assertEqual(str(self.download_btn.cget("state")), "normal")
        self.assertEqual(self.copy_btn.cget("text"), "解析并复制")
        self.assertEqual(str(self.copy_btn.cget("state")), "disabled") # 解析阶段暂停无从谈起

    def test_downloading_phase_shows_pause_and_cancel_both_enabled(self) -> None:
        panel.set_ui_phase("downloading")
        self.root.update()

        self.assertEqual(self.download_btn.cget("text"), "取消")
        self.assertEqual(str(self.download_btn.cget("state")), "normal")
        self.assertEqual(self.copy_btn.cget("text"), "暂停")
        self.assertEqual(str(self.copy_btn.cget("state")), "normal")

    def test_paused_phase_shows_resume_and_cancel_both_enabled(self) -> None:
        panel.set_ui_phase("paused")
        self.root.update()

        self.assertEqual(self.download_btn.cget("text"), "取消")
        self.assertEqual(str(self.download_btn.cget("state")), "normal")
        self.assertEqual(self.copy_btn.cget("text"), "继续")
        self.assertEqual(str(self.copy_btn.cget("state")), "normal")

    def test_clicking_the_buttons_in_downloading_phase_invokes_the_real_commands(self) -> None:
        # 不只检查文案：真的 invoke() 按钮，证明 command 绑定的是可以正常调用的函数，
        # 不是一个凑巧同名、签名不对、一点就报错的东西。
        calls: list[str] = []
        with (
            patch.object(panel, "cancel_current_batch", lambda: calls.append("cancel")),
            patch.object(panel, "pause_current_batch", lambda: calls.append("pause")),
        ):
            panel.set_ui_phase("downloading")
            self.root.update()
            self.download_btn.invoke()
            self.copy_btn.invoke()

        self.assertEqual(calls, ["cancel", "pause"])

    def test_clicking_the_buttons_in_paused_phase_invokes_the_real_commands(self) -> None:
        calls: list[str] = []
        with (
            patch.object(panel, "cancel_current_batch", lambda: calls.append("cancel")),
            patch.object(panel, "resume_current_batch", lambda: calls.append("resume")),
        ):
            panel.set_ui_phase("paused")
            self.root.update()
            self.download_btn.invoke()
            self.copy_btn.invoke()

        self.assertEqual(calls, ["cancel", "resume"])

    def test_clicking_download_in_idle_phase_invokes_the_real_download_command(self) -> None:
        calls: list[str] = []
        with (
            patch.object(panel, "download", lambda: calls.append("download")),
            patch.object(panel, "parse_and_copy", lambda: calls.append("parse_and_copy")),
        ):
            panel.set_ui_phase("idle")
            self.root.update()
            self.download_btn.invoke()
            self.copy_btn.invoke()

        self.assertEqual(calls, ["download", "parse_and_copy"])

    def test_idle_keeps_copy_btn_disabled_while_a_copy_parse_is_still_running(self) -> None:
        # 用户点了“解析并复制”，没等它结束又点了“下载”；批次结束后回到空闲，如果这里无条件
        # 把按钮启用，用户就能对同一批链接再点一次“解析并复制”，两份解析并发跑：重复的网络
        # 请求、两次弹窗、两次写剪贴板。文案与命令要复位，启用则得等那次解析自己回来。
        workers = self.start_a_copy_parse()

        with patch.object(panel, "_batch_control", None):
            panel.set_ui_phase("downloading") # 用户不等解析结束就点了“下载”，按钮归批次所有
            self.root.update()
            self.assertEqual(self.copy_btn.cget("text"), "暂停")

            panel.set_ui_phase("idle") # 批次结束或被取消
            self.root.update()
            self.assertEqual(self.copy_btn.cget("text"), "解析并复制") # 文案与命令照常复位
            self.assertEqual(str(self.copy_btn.cget("state")), "disabled") # 那次解析还在飞

            self.finish_the_copy_parse(workers)

        self.assertEqual(str(self.copy_btn.cget("state")), "normal") # 这时才由它自己恢复

    def test_idle_enables_copy_btn_when_no_copy_parse_is_running(self) -> None:
        # 对照：没有复制解析在飞时，回到空闲照常启用，不因为多了这个判据就一直禁用。
        panel.set_ui_phase("downloading")
        self.root.update()

        panel.set_ui_phase("idle")
        self.root.update()

        self.assertEqual(self.copy_btn.cget("text"), "解析并复制")
        self.assertEqual(str(self.copy_btn.cget("state")), "normal")

    def test_empty_input_never_latches_the_copy_parse_flag(self) -> None:
        # 文本框为空时点“解析并复制”：函数早退，界面上什么都没发生，标志也不该被置位。
        # 一旦在早退之前就置位，此后任何一次回到空闲都会把“解析并复制”按在禁用态上，
        # 而且没有任何回调会来清它——直到重启程序为止。
        started: list = []
        with patch.object(panel, "thread_it", lambda fn, *args, **kwargs: started.append(fn)):
            panel.parse_and_copy() # 文本框是空的
        self.root.update()
        self.assertEqual(started, [], "走的不是早退路径，测试前置条件不成立")

        panel.set_ui_phase("idle")
        self.root.update()

        self.assertEqual(str(self.copy_btn.cget("state")), "normal")

    def test_copy_parse_flag_is_cleared_even_while_a_download_batch_is_active(self) -> None:
        # 复制解析在下载批次仍在跑的时候结束：此刻按钮归批次所有（显示“暂停”），这条回调
        # 不能去改它的状态；但“解析在飞”这个标志必须照样清掉，否则批次结束后回到空闲时，
        # 一次早已结束的解析会把“解析并复制”永远按在禁用态上。
        workers = self.start_a_copy_parse()

        with patch.object(panel, "_batch_control", panel.BatchControl()):
            panel.set_ui_phase("downloading")
            self.root.update()
            self.finish_the_copy_parse(workers) # 解析结束，但批次还在跑

            self.assertEqual(self.copy_btn.cget("text"), "暂停") # 按钮仍归批次，没被这条回调抢走
            self.assertEqual(str(self.copy_btn.cget("state")), "normal")

        with patch.object(panel, "_batch_control", None):
            panel.set_ui_phase("idle") # 批次结束
            self.root.update()

        self.assertEqual(self.copy_btn.cget("text"), "解析并复制")
        self.assertEqual(str(self.copy_btn.cget("state")), "normal") # 标志已清干净，照常启用

    def test_copy_btn_disabled_in_parsing_phase_does_not_invoke_its_command(self) -> None:
        # ttk 对禁用按钮的 invoke() 是真的不执行 command，这条断言依赖的是 Tk 的真实行为，
        # 用 Mock 控件测不出来（Mock 不会管 state 是不是 disabled）。
        calls: list[str] = []
        with patch.object(panel, "parse_and_copy", lambda: calls.append("parse_and_copy")):
            panel.set_ui_phase("parsing")
            self.root.update()
            self.copy_btn.invoke()

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
