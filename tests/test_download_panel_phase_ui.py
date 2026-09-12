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
        url_text = tk.Text(self.root)
        bookmark_var = tk.BooleanVar(self.root)
        self.download_btn = ttk.Button(self.root)
        self.copy_btn = ttk.Button(self.root)
        download_progress_bar = ttk.Progressbar(self.root)
        progress_label = ttk.Label(self.root)
        for widget in (url_text, self.download_btn, self.copy_btn, download_progress_bar, progress_label):
            self.addCleanup(widget.destroy)
        panel.bind_widgets(url_text, bookmark_var, self.download_btn, self.copy_btn, download_progress_bar, progress_label)

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
