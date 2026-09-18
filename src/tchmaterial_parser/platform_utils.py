# -*- coding: utf-8 -*-
# 平台相关的基础设施：错误记录、只读资源定位、目录打开、操作系统判定与 Windows 专有库
#
# 这里只用标准库 logging，不导入本包的 logging_utils：后者要经由 config 反过来用到本模块。

import logging, os, subprocess, sys, platform, webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)

def print_error(e: Exception) -> None: # 记录错误信息与调用栈
    logger.error("%s", e, exc_info=e)

def resource_path(*parts: str) -> Path: # 获取源码或 PyInstaller 打包后的只读资源路径
    bundle_root = getattr(sys, "_MEIPASS", None)

    if bundle_root: # PyInstaller 中数据被放在 tchmaterial_parser/assets/
        package_root = Path(bundle_root) / "tchmaterial_parser"
    else: # 源码运行或 wheel 安装
        package_root = Path(__file__).resolve().parent

    return package_root.joinpath(*parts)

def open_path(target: Path) -> bool: # 用系统的文件管理器打开目录或文件，成功返回 True
    try:
        if os_name == "Windows":
            os.startfile(target)
        elif os_name == "Darwin":
            subprocess.run(["open", str(target)], check=True)
        else:
            subprocess.run(["xdg-open", str(target)], check=True)
        return True
    except Exception as e: # 精简安装的系统可能没有 xdg-open，退回浏览器仍能列出目录内容
        print_error(e)
        try:
            return webbrowser.open(target.as_uri())
        except Exception as fallback_error:
            print_error(fallback_error)
            return False

os_name = platform.system() # 获取操作系统类型
if os_name == "Windows": # 在 Windows 操作系统下，导入 Windows 相关库
    try:
        import win32print, win32gui, win32con, win32api, ctypes, winreg
    except Exception as e:
        print_error(e)
        win32print = win32gui = win32con = win32api = ctypes = winreg = None
else:
    win32print = win32gui = win32con = win32api = ctypes = winreg = None
