# -*- coding: utf-8 -*-
"""Access Token 的设置窗口与获取方法说明。"""

import tkinter as tk
from tkinter import ttk, messagebox

from ..core import tokens
from ..core.tokens import SAVE_FAILED_PREFIX
from .platform_ui import ui_font

HELP_TEXT = """\
国家中小学智慧教育平台需要登录后才可获取教材，因此要使用本程序下载教材，您需要在平台内登录账号（如没有需注册），然后获得登录凭据（Access Token）。本程序仅保存该凭据至本地。

获取方法如下：
1. 打开浏览器，访问国家中小学智慧教育平台（https://auth.smartedu.cn/uias/login）并登录账号。
2. 按下 F12 或 Ctrl+Shift+I，或右键——检查（审查元素）打开开发者工具，选择控制台（Console）。
3. 在控制台粘贴以下代码后回车（Enter）：
---------------------------------------------------------
(function() {
    const authKey = Object.keys(localStorage).find(key => key.startsWith("ND_UC_AUTH"));
    if (!authKey) {
        console.error("未找到 Access Token，请确保已登录！");
        return;
    }
    const tokenData = JSON.parse(localStorage.getItem(authKey));
    const accessToken = JSON.parse(tokenData.value).access_token;
    console.log("%cAccess Token:", "color: green; font-weight: bold", accessToken);
})();
---------------------------------------------------------
然后在控制台输出中即可看到 Access Token。将其复制后粘贴到本程序中。"""


def attach_context_menu(widget, root, items):
    """给文本控件挂右键菜单。"""
    menu = tk.Menu(widget, tearoff=0)
    for label, event in items:
        menu.add_command(label=label, command=lambda e=event: widget.event_generate(e))

    def show(event):
        menu.post(event.x_root, event.y_root)
        menu.bind("<FocusOut>", lambda e: menu.unpost())
        root.bind("<Button-1>", lambda e: menu.unpost(), add="+") # 点到别处也要收起菜单

    widget.bind("<Button-3>", show)
    return menu


def center_on_screen(window) -> None:
    window.update_idletasks()
    w = window.winfo_width()
    h = window.winfo_height()
    ws = window.winfo_screenwidth()
    hs = window.winfo_screenheight()
    window.geometry(f"{w}x{h}+{(ws // 2) - (w // 2)}+{(hs // 2) - (h // 2)}")
    window.lift() # 置顶可见


def show_token_help(parent, root) -> None: # 打开获取 Access Token 方法的窗口
    help_win = tk.Toplevel(parent)
    help_win.title("获取 Access Token 方法")

    help_win.focus_force() # 自动获得焦点
    help_win.grab_set() # 阻止主窗口操作
    help_win.bind("<Escape>", lambda event: help_win.destroy()) # 绑定 Esc 键关闭窗口

    help_frame = ttk.Frame(help_win, padding=20)
    help_frame.pack(fill="both", expand=True)

    # 只读文本区，支持选择复制
    txt = tk.Text(help_frame, wrap="word", font=ui_font(9, root=root))
    txt.insert("1.0", HELP_TEXT)
    txt.config(state="disabled")
    txt.pack(fill="both", expand=True)

    attach_context_menu(txt, root, [("复制 (Ctrl＋C)", "<<Copy>>")])


def show_access_token_window(root, client, on_saved=None) -> None: # 打开输入 Access Token 的窗口
    token_window = tk.Toplevel(root)
    token_window.title("设置 Access Token")

    token_window.focus_force() # 自动获得焦点
    token_window.grab_set() # 阻止主窗口操作
    token_window.bind("<Escape>", lambda event: token_window.destroy()) # 绑定 Esc 键关闭窗口

    # 设置一个 Frame 用于留白、布局更美观
    frame = ttk.Frame(token_window, padding=20)
    frame.pack(fill="both", expand=True)

    label = ttk.Label(frame, text="请粘贴从浏览器获取的 Access Token：", font=ui_font(10, root=root))
    label.pack(pady=5)

    token_text = tk.Text(frame, width=50, height=4, wrap="word", font=ui_font(9, root=root))
    token_text.pack(pady=5)

    if client.access_token: # 若已存在 token，则填入
        token_text.insert("1.0", client.access_token)

    attach_context_menu(token_text, root, [
        ("剪切 (Ctrl＋X)", "<<Cut>>"),
        ("复制 (Ctrl＋C)", "<<Copy>>"),
        ("粘贴 (Ctrl＋V)", "<<Paste>>"),
    ])

    def save_token():
        user_token = token_text.get("1.0", tk.END).strip()
        client.set_access_token(user_token) # 本次运行内立即生效
        tip_info = tokens.save_token(user_token)

        # 落盘失败时文案已经说了实话，呈现方式也得跟上：用警告图标，
        # 并且不执行「保存成功之后」才该做的事
        if tip_info.startswith(SAVE_FAILED_PREFIX):
            messagebox.showwarning("警告", tip_info)
        else:
            if on_saved is not None:
                on_saved()
            messagebox.showinfo("提示", tip_info)

        token_window.destroy()

    def return_save_token(event): # 按下 Enter 键即可保存，并屏蔽换行
        save_token()
        return "break"

    token_text.bind("<Return>", return_save_token)
    token_text.bind("<Shift-Return>", lambda e: "break") # 按下 Shift＋Enter 也不换行，直接屏蔽

    save_btn = ttk.Button(frame, text="保存", command=save_token)
    save_btn.pack(pady=5)

    help_btn = ttk.Button(frame, text="如何获取？", command=lambda: show_token_help(token_window, root))
    help_btn.pack(pady=5)

    center_on_screen(token_window)
