# -*- coding: utf-8 -*-
"""运行期配置与用户目录解析。纯数据，不做任何 I/O。"""

import os
import platform
from dataclasses import dataclass

APP_NAME = "tchMaterial-parser"

os_name = platform.system() # 获取操作系统类型


@dataclass(frozen=True)
class AppConfig:
    # 读取超时是“两次收到数据之间”的间隔而非总时长，因此对大文件下载同样适用
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_download_workers: int = 4
    max_catalog_workers: int = 4
    max_retries: int = 3
    chunk_size: int = 131072 # 每次读取 128 KB
    progress_poll_ms: int = 200

    @property
    def timeout(self) -> tuple:
        return (self.connect_timeout, self.read_timeout)


def _home_subdir(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def cache_dir() -> str: # 资源目录缓存的落盘位置
    if os_name == "Windows":
        base = os.environ.get("LOCALAPPDATA") or _home_subdir("AppData", "Local")
        return os.path.join(base, APP_NAME, "Cache")
    if os_name == "Darwin":
        return _home_subdir("Library", "Caches", APP_NAME)
    base = os.environ.get("XDG_CACHE_HOME") or _home_subdir(".cache")
    return os.path.join(base, APP_NAME)


def config_dir() -> str: # Access Token 等用户数据的落盘位置
    if os_name == "Windows":
        base = os.environ.get("APPDATA") or _home_subdir("AppData", "Roaming")
        return os.path.join(base, APP_NAME)
    if os_name == "Darwin":
        return _home_subdir("Library", "Application Support", APP_NAME)
    base = os.environ.get("XDG_CONFIG_HOME") or _home_subdir(".config")
    return os.path.join(base, APP_NAME)


def log_dir() -> str:
    if os_name == "Windows":
        base = os.environ.get("LOCALAPPDATA") or _home_subdir("AppData", "Local")
        return os.path.join(base, APP_NAME, "Logs")
    if os_name == "Darwin":
        return _home_subdir("Library", "Logs", APP_NAME)
    base = os.environ.get("XDG_STATE_HOME") or _home_subdir(".local", "state")
    return os.path.join(base, APP_NAME)


def legacy_linux_config_file() -> str: # v3.1 及以前固定写在这里，需要继续读得出来
    return _home_subdir(".config", APP_NAME, "data.json")
