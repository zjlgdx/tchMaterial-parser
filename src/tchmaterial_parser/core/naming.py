# -*- coding: utf-8 -*-
"""文件名清洗与重名去重。纯函数，除了探测磁盘不做任何 I/O。"""

import os
import threading

INVALID_FILENAME_CHARS = "/\\:*?\"<>|"
WINDOWS_RESERVED_NAMES = { "CON", "PRN", "AUX", "NUL" } | { f"COM{i}" for i in range(1, 10) } | { f"LPT{i}" for i in range(1, 10) }
MAX_FILENAME_BYTES = 200 # 文件系统的上限是单个文件名 255 字节，留出去重后缀与扩展名的余量


def sanitize_filename(title: str) -> str:
    """把接口返回的标题变成安全的文件名。"""
    name = "".join("_" if (ch in INVALID_FILENAME_CHARS or ord(ch) < 32) else ch for ch in title or "")
    name = name.strip().strip(".").strip() # 首尾的空白与点在 Windows 上会被静默丢弃，导致文件名与预期不符
    if name.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        name = "_" + name

    # 限额是字节数而非字符数：一个汉字 UTF-8 占 3 字节，按字符算会让长标题照样写入失败
    encoded = name.encode("utf-8")[:MAX_FILENAME_BYTES]
    while encoded:
        try:
            name = encoded.decode("utf-8")
            break
        except UnicodeDecodeError: # 截断落在了多字节字符中间，回退一个字节再试
            encoded = encoded[:-1]
    else:
        name = ""

    return name or "download"


_reserved_paths = set() # 已被在飞任务占用的目标路径
_reserved_lock = threading.Lock()


def unique_path(dir_path: str, base_name: str, ext: str) -> str:
    """为重名教材分配互不冲突的路径。"""
    with _reserved_lock:
        candidate = os.path.join(dir_path, base_name + ext)
        index = 2
        # 实测同名教材多达 19 本；此刻文件都还没建出来，只查磁盘会让它们全部选中同一个路径
        while candidate in _reserved_paths or os.path.exists(candidate):
            candidate = os.path.join(dir_path, f"{base_name} ({index}){ext}")
            index += 1

        _reserved_paths.add(candidate)
        return candidate


def reserved_paths() -> set:
    """当前被占用的路径快照，供测试与诊断使用。"""
    with _reserved_lock:
        return set(_reserved_paths)


def clear_reservations() -> None:
    with _reserved_lock:
        _reserved_paths.clear()


def assert_within(dir_path: str, file_path: str) -> None:
    """确认构造出的路径没有逃出所选文件夹。"""
    dir_real = os.path.realpath(dir_path)
    file_real = os.path.realpath(file_path)
    if os.path.commonpath([dir_real, file_real]) != dir_real:
        raise ValueError(f"目标路径超出了所选文件夹：{file_path}")
