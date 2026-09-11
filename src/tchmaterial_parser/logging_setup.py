# -*- coding: utf-8 -*-
"""标准库 logging 的一次性配置。只由入口调用，库模块一律只取 getLogger。"""

import logging
import os
from logging.handlers import RotatingFileHandler

from . import config

LOG_FILENAME = "tchMaterial-parser.log"


def setup_logging(level: int = logging.INFO) -> str:
    """配置 root logger，返回日志文件路径（无法写盘时返回空字符串）。"""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers): # 重复调用时不叠加 handler
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    log_path = ""
    try:
        directory = config.log_dir()
        os.makedirs(directory, exist_ok=True)
        log_path = os.path.join(directory, LOG_FILENAME)
        file_handler = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError: # 日志目录不可写不该让程序起不来
        log_path = ""
        root.warning("日志文件不可写，本次运行只输出到标准错误")

    return log_path
