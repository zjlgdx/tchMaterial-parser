# -*- coding: utf-8 -*-
"""资源目录构建：逐条容错（C2）。"""

import logging

from conftest import FakeResponse, FakeSession
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import catalog
from tchmaterial_parser.core.http import HttpClient

LIST_A = "https://example.invalid/list-a.json"
LIST_B = "https://example.invalid/list-b.json"

TAGS = {
    "hierarchies": [
        {"children": [
            {"tag_id": "tag-edu", "tag_name": "电子教材", "hierarchies": [
                {"children": [
                    {"tag_id": "tag-primary", "tag_name": "小学", "hierarchies": [
                        {"children": [
                            {"tag_id": "tag-chinese", "tag_name": "语文", "hierarchies": []},
                            {"tag_id": "tag-math", "tag_name": "数学", "hierarchies": []},
                        ]}
                    ]},
                    {"tag_id": "tag-junior", "tag_name": "初中", "hierarchies": []},
                ]}
            ]}
        ]}
    ]
}

GOOD_PATH = "教材/tag-edu/tag-primary/tag-chinese"


def book(book_id, title, tag_path=GOOD_PATH, **extra):
    entry = {
        "id": book_id,
        "title": title,
        "tag_paths": [tag_path] if tag_path is not None else [],
        # 这些是实测里占了绝大部分体积的大字段
        "global_description": "x" * 500,
        "custom_properties": {"thumbnails": ["https://x/1.jpg"] * 5},
        "update_time": "2025-05-18",
    }
    entry.update(extra)
    return entry


def build(books, tags=TAGS):
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=tags),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(200, json_data={"urls": LIST_A}),
        LIST_A: FakeResponse(200, json_data=books),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))
    helper = catalog.ResourceHelper(client)
    return helper, helper.fetch_resource_list()


def leaves(tree):
    """收集所有课本叶子。"""
    out = []

    def walk(node):
        children = node.get("children") if isinstance(node, dict) else None
        if not children:
            return
        for key, child in children.items():
            if isinstance(child, dict) and "tag_paths" in child:
                out.append((key, child))
            else:
                walk(child)

    for node in tree.values():
        walk(node)
    return out


def test_tree_structure_matches_tags():
    helper, tree = build([book("b1", "语文一年级上册")])
    assert list(tree) == ["tag-edu"]
    assert tree["tag-edu"]["display_name"] == "电子教材"
    primary = tree["tag-edu"]["children"]["tag-primary"]
    assert primary["display_name"] == "小学"
    assert set(primary["children"]) >= {"tag-chinese", "tag-junior"} - {"tag-junior"}


def test_books_land_under_their_tag():
    helper, tree = build([book("b1", "语文一年级上册")])
    chinese = tree["tag-edu"]["children"]["tag-primary"]["children"]["tag-chinese"]
    assert "b1" in chinese["children"]
    assert chinese["children"]["b1"]["display_name"] == "语文一年级上册"


def test_bad_entries_are_skipped_without_losing_the_tree(caplog):
    """一条坏数据只该丢掉它自己。"""
    books = [
        book("good-1", "语文一年级上册"),
        book("empty-tag", "没有层级的资料", tag_path=None),          # tag_paths 为空数组
        book("unknown-branch", "指向不存在的分支", tag_path="教材/tag-nope/tag-x"),
        {"id": "no-tag-paths", "title": "字段缺失"},                  # 连 tag_paths 键都没有
        book("good-2", "数学一年级上册", tag_path="教材/tag-edu/tag-primary/tag-math"),
    ]
    with caplog.at_level(logging.DEBUG, logger="tchmaterial_parser.core.catalog"):
        helper, tree = build(books)

    ids = {book_id for book_id, _ in leaves(tree)}
    assert ids == {"good-1", "good-2"}, ids
    assert helper.skipped_entries == 3
    messages = [r.getMessage() for r in caplog.records]
    assert any("跳过 3 条无法解析的条目" in m for m in messages), messages
    assert any(r.levelno == logging.WARNING for r in caplog.records), "汇总没有记成 WARNING"


def test_duplicate_titles_both_survive():
    """实测同名极普遍；两本同名教材必须以不同 id 同时存在。"""
    books = [
        book("dup-1", "义务教育教科书·英语三年级下册"),
        book("dup-2", "义务教育教科书·英语三年级下册"),
    ]
    helper, tree = build(books)
    found = leaves(tree)
    assert {book_id for book_id, _ in found} == {"dup-1", "dup-2"}
    assert len({node["display_name"] for _, node in found}) == 1


def test_display_name_falls_back_to_name_then_id():
    books = [
        book("only-name", None, name="只有 name"),
        book("neither", None),
    ]
    books[0].pop("title")
    books[1].pop("title")
    helper, tree = build(books)
    names = {book_id: node["display_name"] for book_id, node in leaves(tree)}
    assert names["only-name"] == "只有 name"
    assert names["neither"] == "(未知电子课本 neither)"


def test_lesson_list_is_not_fetched():
    """fetch_resource_list 不该触碰课件接口。"""
    helper, tree = build([book("b1", "语文一年级上册")])
    urls = [url for url, _ in helper.client.session.calls]
    assert catalog.NATIONAL_LESSON_TAGS not in urls
    assert catalog.NATIONAL_LESSON_VERSION not in urls
