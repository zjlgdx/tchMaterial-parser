# -*- coding: utf-8 -*-
"""抓取并构建资源目录树。"""

import logging

logger = logging.getLogger(__name__)

TCH_MATERIAL_TAGS = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/tch_material_tag.json"
TCH_MATERIAL_VERSION = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/resources/tch_material/version/data_version.json"
NATIONAL_LESSON_TAGS = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/tags/national_lesson_tag.json"
NATIONAL_LESSON_VERSION = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/national_lesson/teachingmaterials/version/data_version.json"


class ResourceHelper: # 获取网站上资源的数据
    def __init__(self, client):
        self.client = client
        self.skipped_entries = 0

    def parse_hierarchy(self, hierarchy): # 解析层级数据
        if not hierarchy: # 如果没有层级数据，返回空
            return None

        parsed = {}
        for h in hierarchy:
            for ch in h["children"]:
                parsed[ch["tag_id"]] = { "display_name": ch["tag_name"], "children": self.parse_hierarchy(ch["hierarchies"]) }
        return parsed

    def place_book(self, parsed_hier, book) -> bool:
        """把一本课本挂到层级树上；这条数据挂不上去时返回 False。"""
        if len(book["tag_paths"]) == 0: # 某些非课本资料的 tag_paths 属性为空数组
            return False

        # 解析课本层级数据；电子课本 tag_paths 的前两项为“教材”、“电子教材”
        tag_paths = book["tag_paths"][0].split("/")[2:]

        # 如果课本层级数据不在层级数据中，跳过
        temp_hier = parsed_hier[book["tag_paths"][0].split("/")[1]]
        if not tag_paths or tag_paths[0] not in temp_hier["children"]:
            return False

        # 分别解析课本层级
        for p in tag_paths:
            if temp_hier["children"] and temp_hier["children"].get(p):
                temp_hier = temp_hier["children"].get(p)
        if not temp_hier["children"]:
            temp_hier["children"] = {}

        book["display_name"] = book["title"] if "title" in book else book["name"] if "name" in book else f"(未知电子课本 {book['id']})"

        temp_hier["children"][book["id"]] = book
        return True

    def fetch_book_list(self): # 获取课本列表
        # 获取电子课本层级数据
        tags_data = self.client.get_json(TCH_MATERIAL_TAGS)
        parsed_hier = self.parse_hierarchy(tags_data["hierarchies"])

        # 获取电子课本 URL 列表
        list_data = self.client.get_json(TCH_MATERIAL_VERSION)["urls"].split(",")

        skipped = 0
        for url in list_data:
            book_data = self.client.get_json(url)
            for book in book_data:
                # 逐条容错：一条坏数据只该丢掉它自己，不该让整棵树报废、
                # 让用户失去全部选择功能
                try:
                    if not self.place_book(parsed_hier, book):
                        skipped += 1
                except (KeyError, IndexError, TypeError, AttributeError) as e:
                    skipped += 1
                    logger.debug("跳过一条无法解析的课本数据：%s（%s）", book.get("id"), e)

        if skipped:
            logger.warning("资源目录构建完成，跳过 %d 条无法解析的条目", skipped)

        self.skipped_entries = skipped
        return parsed_hier

    def fetch_lesson_list(self): # 获取课件列表
        # 获取课件层级数据
        tags_data = self.client.get_json(NATIONAL_LESSON_TAGS)
        parsed_hier = self.parse_hierarchy([{ "children": [{ "tag_id": "__internal_national_lesson", "hierarchies": tags_data["hierarchies"], "tag_name": "课件资源" }] }])

        # 获取课件 URL 列表
        list_data = self.client.get_json(NATIONAL_LESSON_VERSION)["urls"]

        # 获取课件列表
        for url in list_data:
            lesson_data = self.client.get_json(url)
            for lesson in lesson_data:
                if len(lesson["tag_list"]) > 0:
                    # 解析课件层级数据
                    tag_paths = [tag["tag_id"] for tag in sorted(lesson["tag_list"], key=lambda tag: tag["order_num"])]

                    # 分别解析课件层级
                    temp_hier = parsed_hier["__internal_national_lesson"]
                    for p in tag_paths:
                        if temp_hier["children"] and temp_hier["children"].get(p):
                            temp_hier = temp_hier["children"].get(p)
                    if not temp_hier["children"]:
                        temp_hier["children"] = {}

                    lesson["display_name"] = lesson["title"] if "title" in lesson else lesson["name"] if "name" in lesson else f"(未知课件 {lesson['id']})"

                    temp_hier["children"][lesson["id"]] = lesson

        return parsed_hier

    def fetch_resource_list(self): # 获取资源列表
        book_hier = self.fetch_book_list()
        # lesson_hier = self.fetch_lesson_list() # 目前此函数代码存在问题
        return { **book_hier }
