# -*- coding: utf-8 -*-
"""跨平台的界面杂活：高 DPI、字体族、窗口图标。"""

import logging
import tkinter as tk
from importlib.resources import as_file, files
from tkinter import font as tkfont

from ..config import os_name

logger = logging.getLogger(__name__)

if os_name == "Windows":
    import win32print, win32gui, win32con, win32api, ctypes

# 各平台自带的中文字体优先级；硬编码「微软雅黑」会在 macOS / Linux 上回退成难看的默认字体
FONT_PREFERENCES = {
    "Windows": ["Microsoft YaHei UI", "微软雅黑", "Microsoft YaHei"],
    "Darwin": ["PingFang SC", "Heiti SC", "STHeiti"],
    "Linux": ["Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "DejaVu Sans"],
}

_ui_family = None


def apply_dpi_scaling(root) -> float:
    """返回缩放因子，并在 Windows 上声明由应用程序自行缩放。"""
    if os_name == "Windows":
        scale = round(win32print.GetDeviceCaps(win32gui.GetDC(0), win32con.DESKTOPHORZRES)
                      / win32api.GetSystemMetrics(0), 2) # 获取当前的缩放因子
        try: # Windows 8.1 或更新
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception: # Windows 8 或更老
            ctypes.windll.user32.SetProcessDPIAware()
    else: # 在非 Windows 操作系统上，通过 Tkinter 估算缩放因子
        try:
            scale = round(root.winfo_fpixels("1i") / 96.0, 2)
        except Exception:
            scale = 1.0

    root.tk.call("tk", "scaling", scale / 0.75) # 设置缩放因子
    return scale


def ui_family(root=None) -> str:
    """挑一个本机确实装了的中文字体族。"""
    global _ui_family
    if _ui_family is not None:
        return _ui_family

    try:
        available = set(tkfont.families(root))
    except Exception:
        available = set()

    for candidate in FONT_PREFERENCES.get(os_name, []):
        if candidate in available:
            _ui_family = candidate
            break
    else:
        _ui_family = tkfont.nametofont("TkDefaultFont").actual("family")

    return _ui_family


def ui_font(size: int, bold: bool = False, root=None) -> tuple:
    family = ui_family(root)
    return (family, size, "bold") if bold else (family, size)


ICON_PACKAGE = "tchmaterial_parser.assets"
ICON_FILENAME = "favicon_223x223.png"


def set_window_icon(root) -> None: # 设置窗口图标
    # 图标随包发布，源码运行与 PyInstaller 冻结后走同一段代码；不落临时文件，
    # 共享临时目录里的固定文件名在多用户机器上是可被预先占位的符号链接攻击面
    try:
        with as_file(files(ICON_PACKAGE).joinpath(ICON_FILENAME)) as icon_path:
            icon = tk.PhotoImage(file=str(icon_path))
    except Exception:
        logger.warning("窗口图标加载失败", exc_info=True)
        return

    root.iconphoto(True, icon)
    root._icon_ref = icon # 为防止图片被垃圾回收，保存引用
