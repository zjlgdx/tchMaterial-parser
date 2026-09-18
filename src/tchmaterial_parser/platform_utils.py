# -*- coding: utf-8 -*-
# 平台相关的基础设施：错误记录、只读资源定位、目录打开、操作系统判定与 Windows 专有库
#
# 这里只用标准库 logging，不导入本包的 logging_utils：后者要经由 config 反过来用到本模块。

import logging, os, re, subprocess, sys, platform, webbrowser
from pathlib import Path

logger = logging.getLogger(__name__)

# CPython gh-110218：更早的 Tk 在 macOS Sonoma 及以后的系统上收不到鼠标点击，界面看上去像是卡死
MIN_MACOS_TK = (8, 6, 13)

def print_error(e: Exception) -> None: # 记录错误信息与调用栈
    logger.error("%s", e, exc_info=e)

def tk_patchlevel(window: object) -> tuple[int, ...]: # 解析 Tk 的完整版本号，读不到或格式异常时返回空元组
    try:
        raw = str(window.getvar("tk_patchLevel"))
    except Exception as e: # 读不到就不判断，这条路径不该刷出调用栈
        logger.debug("读取 Tk 版本号失败：%s", e)
        return ()

    numbers: list[int] = []
    for segment in raw.split("."):
        leading_digits = re.match(r"\d+", segment) # 预发布版形如 9.1b1，取前导数字仍可比较
        if not leading_digits:
            break
        numbers.append(int(leading_digits.group()))

    if len(numbers) < 2: # 正常的 patchlevel 至少是「主版本.次版本」，短于此说明根本没解析出来
        logger.debug("无法解析 Tk 版本号：%s", raw)
        return ()
    return tuple(numbers)

def outdated_macos_tk(window: object) -> str: # macOS 上 Tk 过旧时返回它的版本号，否则返回空字符串
    if os_name != "Darwin":
        return ""

    patchlevel = tk_patchlevel(window)
    if not patchlevel or patchlevel >= MIN_MACOS_TK: # 空元组表示版本号没解析出来，此时不猜也不提示
        return ""
    return ".".join(str(part) for part in patchlevel)

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
