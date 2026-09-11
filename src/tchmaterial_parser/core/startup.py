# -*- coding: utf-8 -*-
"""启动时的资源目录加载流程。

cache 依赖 catalog 的 CatalogNode，所以这段同时用到两者的编排放在它们之上，
而不是塞进其中任何一个。这里没有任何界面依赖，无图形环境下照样可以测。
"""

import logging

from . import cache
from .catalog import CatalogCancelled, ResourceHelper

logger = logging.getLogger(__name__)


def load_catalog(client, helper: ResourceHelper = None, progress_cb=None):
    """加载资源目录，返回 (树, 是否为离线缓存, 失败原因)。

    先花一个小请求探版本；命中缓存就直接用，热启动完全不碰上游那四十余 MB。
    """
    helper = helper or ResourceHelper(client)

    try:
        version = helper.fetch_version()
    except CatalogCancelled:
        raise
    except Exception as e: # 多半是离线；旧缓存也好过一个空白面板
        logger.info("版本探测失败，尝试回退到本地缓存：%s", e)
        found = cache.load_any()
        if found:
            return found[1], True, None
        return {}, False, str(e)

    cached = cache.load(version.version)
    if cached is not None:
        logger.info("资源目录缓存命中（版本 %s）", version.version)
        return cached, False, None

    try:
        tree = helper.fetch_tree(version, progress_cb=progress_cb)
    except CatalogCancelled:
        raise
    except Exception as e:
        # 上游版本已经变了，但这次没拉下来。旧树仍然比空面板有用，
        # 但它确实已经过时——必须打上标记，否则用户看到的是旧目录却毫不知情
        logger.warning("资源目录拉取失败，回退到本地缓存：%s", e)
        found = cache.load_any()
        if found:
            return found[1], True, None
        return {}, False, str(e)

    cache.store(version.version, tree)
    return tree, False, None
