# -*- coding: utf-8 -*-
"""抓取并构建资源目录树。"""

import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..config import AppConfig
from .errors import NetworkError, UpstreamFormatError

logger = logging.getLogger(__name__)

# data_version.json 里承载版本号的候选键；上游没承诺过字段名，都取不到时
# 退化成整份响应体的摘要——内容变则键变，在任何命名下都是对的
VERSION_KEYS = ("version", "module_version", "data_version", "update_time")

DEFAULT_RESOURCE_TYPE = "assets_document"

TCH_MATERIAL_TAGS = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/tch_material_tag.json"
TCH_MATERIAL_VERSION = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/version/data_version.json"


@dataclass(frozen=True)
class CatalogVersion:
    """版本探测的结果：缓存键，外加四个列表文件的地址。"""

    version: str
    urls: tuple


@dataclass
class CatalogNode:
    """目录树的节点。

    只保留这四个字段。实测单个列表文件解析后常驻 19.7 MB，裁剪后 0.43 MB——
    原始条目里的 global_description、缩略图列表等大字段一律用完即弃，
    绝不整份留存。
    """

    node_id: str
    display_name: str
    resource_type_code: str = None
    children: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "resource_type_code": self.resource_type_code,
            "children": {k: v.to_dict() for k, v in self.children.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CatalogNode":
        return cls(
            node_id=data["node_id"],
            display_name=data["display_name"],
            resource_type_code=data.get("resource_type_code"),
            children={k: cls.from_dict(v) for k, v in (data.get("children") or {}).items()},
        )


@dataclass(frozen=True)
class PageOutcome:
    """一个列表文件的解析结果。

    「整页不可用」与「这一页里一条都没挂上」必须分开记：四个列表文件出自同一个
    接口，真实的格式变更是四页同时变形，而不是只坏一页。
    """

    usable: bool
    placed: int = 0
    skipped: int = 0
    reason: str = "" # 整页不可用时留下原因，全都不可用时要报给用户


def iter_nodes(tree):
    """深度优先遍历整棵树。"""
    for node in tree.values():
        yield node
        yield from iter_nodes(node.children)


class CatalogCancelled(Exception):
    """关窗时置位取消标志，正在加载目录的线程据此提前退出。"""


class ResourceHelper: # 获取网站上资源的数据
    def __init__(self, client, config=None):
        self.client = client
        self.config = config or AppConfig()
        self.skipped_entries = 0
        self.skipped_pages = 0
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    def _check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise CatalogCancelled("资源目录加载已取消")

    def parse_hierarchy(self, hierarchy) -> dict: # 解析层级数据
        if not hierarchy: # 如果没有层级数据，返回空
            return {}

        parsed = {}
        for h in hierarchy:
            for ch in h["children"]:
                parsed[ch["tag_id"]] = CatalogNode(
                    node_id=ch["tag_id"],
                    display_name=ch["tag_name"],
                    children=self.parse_hierarchy(ch["hierarchies"]))
        return parsed

    def place_book(self, parsed_hier, book) -> bool:
        """把一本课本挂到层级树上；这条数据挂不上去时返回 False。"""
        if len(book["tag_paths"]) == 0: # 某些非课本资料的 tag_paths 属性为空数组
            return False

        # 解析课本层级数据；电子课本 tag_paths 的前两项为“教材”、“电子教材”
        tag_paths = book["tag_paths"][0].split("/")[2:]

        # 如果课本层级数据不在层级数据中，跳过
        temp_hier = parsed_hier[book["tag_paths"][0].split("/")[1]]
        if not tag_paths or tag_paths[0] not in temp_hier.children:
            return False

        # 分别解析课本层级
        for p in tag_paths:
            if temp_hier.children.get(p):
                temp_hier = temp_hier.children[p]

        display_name = book["title"] if "title" in book else book["name"] if "name" in book else f"(未知电子课本 {book['id']})"

        # 就地裁剪：原始 dict 用完即弃，不把整份列表留在内存里等建完树再筛
        temp_hier.children[book["id"]] = CatalogNode(
            node_id=book["id"],
            display_name=display_name,
            resource_type_code=book.get("resource_type_code") or DEFAULT_RESOURCE_TYPE)
        return True

    def fetch_version(self) -> CatalogVersion:
        """只取 data_version.json 这一个小文件。

        版本探测必须廉价到可以无条件执行，缓存才有机会在付出那约 32 MB 的
        解析与建树之前拦下这次加载。
        """
        payload = self.client.get_json(TCH_MATERIAL_VERSION)
        urls = tuple(u for u in str(payload["urls"]).split(",") if u)

        version = None
        for key in VERSION_KEYS:
            if payload.get(key):
                version = str(payload[key])
                break
        if version is None:
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
            version = digest.hexdigest()[:16]

        return CatalogVersion(version=version, urls=urls)

    def parse_and_merge(self, url: str, response, parsed_hier: dict) -> PageOutcome:
        """解析一个列表文件并挂到树上。

        调用方必须持有解析锁：解析出来的对象比原始字节大一个数量级，
        同时存在几份会把内存峰值顶上去；建树也在改同一棵树。
        """
        book_data = self.client.parse_json(url, response)
        if not isinstance(book_data, list):
            # 只丢这一页，另外几页照常建树——「一条坏数据不该让整棵树报废」
            # 同样适用于「一页坏数据」，何况用户失去的是整个选择功能
            logger.warning("课本列表不是数组，整页跳过：%s", url)
            return PageOutcome(usable=False, reason="课本列表不是数组")

        placed = 0
        skipped = 0
        for book in book_data:
            # 逐条容错：一条坏数据只该丢掉它自己，不该让整棵树报废、
            # 让用户失去全部选择功能
            try:
                if self.place_book(parsed_hier, book):
                    placed += 1
                else:
                    skipped += 1
            except (KeyError, IndexError, TypeError, AttributeError) as e:
                skipped += 1
                # 坏条目未必是字典：在这里调 book.get("id") 会再抛一次，
                # 把「逐条容错」变成「一条坏数据毁掉整棵树」
                logger.debug("跳过一条无法解析的课本数据：%.80s（%s）", repr(book), e)
        return PageOutcome(usable=True, placed=placed, skipped=skipped)

    def _load_one_list(self, url: str, parsed_hier: dict, parse_lock) -> PageOutcome:
        self._check_cancelled()
        try:
            response = self.client.get(url) # 传输：并行，瓶颈在网络
            self._check_cancelled()
            with parse_lock: # 解析 + 裁剪 + 挂树：串行，同一时刻只存在一份中间对象
                return self.parse_and_merge(url, response, parsed_hier)
        except (NetworkError, UpstreamFormatError) as e:
            # 整页不可用有好几种形态——连不上、5xx、正文不是 JSON、不是数组——
            # 对用户来说结论是同一个：这一页没有课本可用。只丢这一页，其余几页
            # 照常建树；全都不可用时由 placed == 0 那道下界响亮地失败。
            # AuthError 不在这里拦：Token 失效四页都会失败，而那句「请重新设置
            # Token」是用户唯一能据以行动的信息，不该被降级成一句格式错误
            logger.warning("课本列表整页跳过：%s（%s）", url, e)
            return PageOutcome(usable=False, reason=str(e))

    def fetch_tree(self, version: CatalogVersion = None, progress_cb=None) -> dict:
        """拉取四个列表文件并建树；只有缓存未命中时才会走到这里。"""
        if version is None:
            version = self.fetch_version()

        # 获取电子课本层级数据
        tags_data = self.client.get_json(TCH_MATERIAL_TAGS)
        parsed_hier = self.parse_hierarchy(tags_data["hierarchies"])

        total = len(version.urls)
        placed = 0
        skipped = 0
        skipped_pages = 0
        page_reasons = []
        done = 0
        parse_lock = threading.Lock()
        workers = max(1, min(self.config.max_catalog_workers, total or 1))

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="catalog") as pool:
            futures = {pool.submit(self._load_one_list, url, parsed_hier, parse_lock): url
                       for url in version.urls}
            try:
                for future in as_completed(futures):
                    outcome = future.result()
                    placed += outcome.placed
                    skipped += outcome.skipped
                    if not outcome.usable:
                        skipped_pages += 1
                        page_reasons.append(outcome.reason)
                    done += 1
                    if progress_cb is not None:
                        progress_cb(done, total)
            except BaseException:
                self.cancelled.set() # 让还没开始传输的任务立刻退出，不必等它们跑完
                for future in futures:
                    future.cancel()
                raise

        if skipped_pages:
            logger.warning("资源目录构建完成，%d/%d 个课本列表文件不可用，整页跳过",
                           skipped_pages, total)
        if skipped:
            logger.warning("资源目录构建完成，跳过 %d 条无法解析的条目", skipped)

        self.skipped_entries = skipped
        self.skipped_pages = skipped_pages

        # 一本课本都没挂上，说明拿到的不是这个接口该有的东西。此时返回一棵
        # 只有分类、没有课本的树，load_catalog 会把它当成好数据写进缓存，
        # 覆盖掉上一份能用的离线缓存——用户逐层展开全是空的，重启也不自愈。
        # 响亮地失败，缓存回退才接得住
        if placed == 0:
            detail = page_reasons[0] if page_reasons else f"另有 {skipped} 条条目被跳过"
            raise UpstreamFormatError(
                f"课本列表里没有任何一本课本可以挂上层级树"
                f"（{skipped_pages}/{total} 个列表文件整页跳过：{detail}）")

        return parsed_hier

