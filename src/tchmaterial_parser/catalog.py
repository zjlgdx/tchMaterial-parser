# -*- coding: utf-8 -*-
# 获取平台上的资源目录树，并提供按分类路径筛选与计数的辅助函数

import gzip, json, logging, os
from collections.abc import Callable

from .config import catalog_cache_path
from .logging_utils import log_duration
from .network import session
from .platform_utils import print_error

logger = logging.getLogger(__name__)

BOOK_VERSION_URL = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/version/data_version.json"
CACHE_FORMAT = 1 # 缓存文件的结构版本，结构变动后旧缓存自然失效
CATALOG_SLOW_MS = 15000 # 目录加载超过这个耗时就值得在日志里标出来

def fetch_book_version() -> tuple[str, list[str]]: # 获取电子课本目录的版本标识与各分片地址（该文件很小，可先取它判断缓存是否仍然可用）
    version_data: dict = session.get(BOOK_VERSION_URL).json()
    urls: str = version_data["urls"]
    return f"{version_data['module_version']}:{urls}", urls.split(",")

def is_resource_tree(items: object) -> bool: # 缓存文件可能被外部改动，结构不符时不能当成命中，否则界面会卡在加载提示上
    if not isinstance(items, dict):
        return False
    for item in items.values():
        if not isinstance(item, dict) or not isinstance(item.get("display_name"), str):
            return False
        if "children" in item and not is_resource_tree(item["children"]):
            return False
    return True

def load_cached_resource_list(version: str) -> dict | None: # 读取本地缓存的资源目录；缓存缺失、损坏或版本不符时返回 None，由调用方重新抓取
    cache_file = catalog_cache_path()
    if not cache_file:
        return None

    try:
        if not cache_file.exists(): # 探测本身也可能因目录权限不足而抛错，因此一并放进 try
            return None
        with gzip.open(cache_file, "rt", encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict) or cached.get("cache_format") != CACHE_FORMAT or cached.get("version") != version:
            return None
        resource_list = cached.get("resource_list")
        return resource_list if is_resource_tree(resource_list) else None
    except Exception as e: # 缓存文件损坏或无法读取，重新抓取即可
        print_error(e)
        return None

def save_cached_resource_list(version: str, resource_list: dict) -> None: # 把资源目录写入本地缓存（数据较大，使用 gzip 压缩存放）
    cache_file = catalog_cache_path()
    if not cache_file:
        return

    # 先写入临时文件再替换，避免写入中断时留下半截缓存；文件名带进程 ID，以免多个实例同时写入时互相覆盖
    temp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(temp_file, "wt", encoding="utf-8") as f:
            json.dump({ "cache_format": CACHE_FORMAT, "version": version, "resource_list": resource_list }, f, ensure_ascii=False)
        os.replace(temp_file, cache_file)
    except Exception as e:
        print_error(e)
        try:
            temp_file.unlink(missing_ok=True)
        except Exception:
            pass

class ResourceHelper: # 获取网站上资源的数据
    def parse_hierarchy(self, hierarchy: list) -> dict: # 解析层级数据
        if not hierarchy: # 如果没有层级数据，返回空字典
            return {}

        parsed = {}
        for h in hierarchy:
            for ch in h["children"]:
                parsed[ch["tag_id"]] = { "display_name": ch["tag_name"], "children": self.parse_hierarchy(ch["hierarchies"]) }
        return parsed

    def fetch_book_list(self, list_data: list[str], progress: Callable[[str], None] | None = None) -> dict: # 获取课本列表（list_data 为 fetch_book_version() 取得的分片地址）
        # 获取电子课本层级数据
        tags_resp = session.get("https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/tch_material_tag.json")
        tags_data: dict = tags_resp.json()
        parsed_hier = self.parse_hierarchy(tags_data["hierarchies"])

        # 获取电子课本列表
        for index, url in enumerate(list_data, start=1):
            if progress:
                progress(f"正在下载资源列表（第 {index}/{len(list_data)} 部分）")
            book_resp = session.get(url)
            book_data: list[dict] = book_resp.json()
            if not isinstance(book_data, list) or not book_data: # 分片内容异常时整体失败，避免把残缺的目录写进缓存后长期复用
                raise ValueError(f"电子课本分片返回了异常内容：{url}")
            for book in book_data:
                if book.get("tag_paths"): # 某些非课本资料的 tag_paths 属性为空数组
                    # 解析课本层级数据
                    tag_paths: list[str] = book["tag_paths"][0].split("/")

                    # 分别解析课本层级
                    temp_hier = parsed_hier[tag_paths[1]]

                    for p in tag_paths[2:]: # 电子课本 tag_paths 的前两项为 “教材”、“电子教材”
                        if temp_hier.get("children") and temp_hier["children"].get(p):
                            temp_hier = temp_hier["children"][p]
                    if not temp_hier.get("children"):
                        temp_hier["children"] = {}

                    book["display_name"] = book.get("title") or book.get("name") or f"(未知电子课本 {book['id']})"

                    temp_hier["children"][book["id"]] = book

        return parsed_hier

    def fetch_national_lesson_list(self) -> dict: # 获取自学课件列表
        # 获取课件层级数据
        tags_resp = session.get("https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/national_lesson_tag.json")
        tags_data: dict = tags_resp.json()
        parsed_hier = self.parse_hierarchy([{ "children": [{ "tag_id": "__internal_national_lesson", "hierarchies": tags_data["hierarchies"], "tag_name": "学生自主学习课件" }] }])

        # 获取课件 URL 列表
        list_resp = session.get("https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/national_lesson/teachingmaterials/version/data_version.json")
        list_data: list[str] = list_resp.json()["urls"]

        # 获取课件列表
        for url in list_data:
            lesson_resp = session.get(url)
            lesson_data: list[dict] = lesson_resp.json()
            for lesson in lesson_data:
                if lesson.get("tag_list"):
                    # 解析课件层级数据
                    tag_paths: list[str] = [tag["tag_id"] for tag in sorted(lesson["tag_list"], key=lambda tag: tag["order_num"])]

                    # 分别解析课件层级（tag_paths 为乱序）
                    def parse_tag_path(hier: dict) -> dict:
                        for p in tag_paths:
                            if hier.get("children") and hier["children"].get(p):
                                return parse_tag_path(hier["children"][p])
                        return hier

                    hier = parse_tag_path(parsed_hier["__internal_national_lesson"])
                    if not hier.get("children"):
                        hier["children"] = {}

                    lesson["display_name"] = lesson.get("title") or lesson.get("name") or f"(未知课件 {lesson['id']})"

                    hier["children"][lesson["id"]] = lesson

        return parsed_hier

    def fetch_prepare_lesson_list(self) -> dict: # 获取备课课件列表
        # 获取课件层级数据
        tags_resp = session.get("https://s-file-2.ykt.cbern.com.cn/zxx/ndrs/tags/k12.json")
        tags_data: dict = tags_resp.json()
        parsed_hier = self.parse_hierarchy([{ "children": [{ "tag_id": "__internal_prepare_lesson", "hierarchies": tags_data["hierarchies"], "tag_name": "教师备课授课课件" }] }])

        # 获取课件 URL 列表
        list_resp = session.get("https://s-file-2.ykt.cbern.com.cn/zxx/ndrs/prepare_lesson/teachingmaterials/parts.json")
        list_data: list[str] = list_resp.json()

        # 获取课件列表
        for url in list_data:
            lesson_resp = session.get(url)
            lesson_data: list[dict] = lesson_resp.json()
            for lesson in lesson_data:
                if lesson.get("tag_list"):
                    # 解析课件层级数据
                    tag_paths: list[str] = [tag["tag_id"] for tag in sorted(lesson["tag_list"], key=lambda tag: tag["order_num"])]

                    # 分别解析课件层级（tag_paths 为乱序）
                    def parse_tag_path(hier: dict) -> dict:
                        for p in tag_paths:
                            if hier.get("children") and hier["children"].get(p):
                                return parse_tag_path(hier["children"][p])
                        return hier

                    hier = parse_tag_path(parsed_hier["__internal_prepare_lesson"])
                    if not hier.get("children"):
                        hier["children"] = {}

                    lesson["display_name"] = lesson.get("title") or lesson.get("name") or f"(未知课件 {lesson['id']})"

                    hier["children"][lesson["id"]] = lesson

        return parsed_hier

    def fetch_resource_list(self, progress: Callable[[str], None] | None = None) -> dict: # 获取资源列表：目录版本未变时直接使用本地缓存，避免每次启动都重新下载全部分片
        with log_duration(logger, "获取资源目录", CATALOG_SLOW_MS):
            if progress:
                progress("正在检查资源列表版本")
            version, list_data = fetch_book_version()

            if progress:
                progress("正在读取本地缓存")
            cached_list = load_cached_resource_list(version)
            if cached_list is not None:
                logger.info("资源目录来自本地缓存，共 %d 个顶层分类", len(cached_list))
                return cached_list

            book_hier = self.fetch_book_list(list_data, progress)
            # 下面两类资源若要启用，其版本标识也应计入 version，否则它们更新后不会刷新缓存
            # national_lesson_hier = self.fetch_national_lesson_list()
            # prepare_lesson_hier = self.fetch_prepare_lesson_list()
            resource_list = { **book_hier }
            save_cached_resource_list(version, resource_list)
            logger.info("资源目录来自网络，共 %d 个分片、%d 个顶层分类", len(list_data), len(resource_list))
            return resource_list

def filter_resource_items(items: dict[str, dict], query: str) -> dict[str, dict]: # 按完整分类路径筛选资源树
    keywords = query.casefold().split()
    if not keywords:
        return items

    def filter_branch(branch: dict[str, dict], parent_names: tuple[str, ...]) -> dict[str, dict]:
        matches: dict[str, dict] = {}
        for option_id, option_data in branch.items():
            path_names = (*parent_names, option_data["display_name"])
            children = option_data.get("children", {})
            if children:
                filtered_children = filter_branch(children, path_names)
                if filtered_children:
                    matches[option_id] = { **option_data, "children": filtered_children }
            elif all(keyword in " ".join(path_names).casefold() for keyword in keywords):
                matches[option_id] = option_data
        return matches

    return filter_branch(items, ())

def count_resource_items(items: dict[str, dict]) -> int: # 统计资源树中的末级资源数量
    return sum(
        count_resource_items(children) if (children := option_data.get("children", {})) else 1
        for option_data in items.values()
    )
