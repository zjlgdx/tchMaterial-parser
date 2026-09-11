# -*- coding: utf-8 -*-
"""资源目录的磁盘缓存。

存的是裁剪后的树（约 2 MB），热启动只读它，完全不触碰上游那四个 10 MB 的
列表文件。缓存永远不该让程序起不来：读到任何异常都当作未命中。
"""

import json
import logging
import os

from .. import config
from .catalog import CatalogNode

logger = logging.getLogger(__name__)

CACHE_FILENAME = "catalog.json"


def cache_file() -> str:
    return os.path.join(config.cache_dir(), CACHE_FILENAME)


def _read() -> dict:
    path = cache_file()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_any():
    """不校验版本地读出缓存，返回 (版本, 树)。

    只在版本探测失败（离线）时使用：一棵可能过时几天的树，对断网的用户远胜
    于一个空白面板。
    """
    try:
        payload = _read()
        if not payload:
            return None
        tree = {k: CatalogNode.from_dict(v) for k, v in payload["tree"].items()}
        return payload.get("version"), tree
    except Exception:
        logger.warning("资源目录缓存读取失败，将忽略它", exc_info=True)
        return None


def load(cache_key: str):
    """版本一致时返回缓存的树，否则返回 None。"""
    if not cache_key:
        return None

    found = load_any()
    if not found:
        return None

    version, tree = found
    if version != cache_key:
        logger.info("资源目录缓存版本不符（缓存 %s，上游 %s），将重新拉取", version, cache_key)
        return None

    return tree


def store(cache_key: str, tree: dict) -> bool:
    """写入缓存；失败只记日志，不影响本次运行。"""
    path = cache_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = { "version": cache_key, "tree": {k: v.to_dict() for k, v in tree.items()} }

        tmp = path + ".tmp" # 先写临时文件再改名，避免进程中断留下半截缓存
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        logger.warning("资源目录缓存写入失败", exc_info=True)
        return False


def clear() -> None:
    try:
        os.remove(cache_file())
    except FileNotFoundError:
        pass
