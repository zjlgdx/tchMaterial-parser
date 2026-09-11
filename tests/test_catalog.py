# -*- coding: utf-8 -*-
"""资源目录构建：逐条容错（C2）。"""

import logging

import pytest
import requests

from conftest import FakeResponse, FakeSession
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import catalog
from tchmaterial_parser.core.errors import AuthError, UpstreamFormatError
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
    return helper, helper.fetch_tree()


def leaves(tree):
    """收集所有课本节点。

    只看 is_leaf 不够：没有课本挂上去的标签节点同样没有子节点。课本一定带
    resource_type_code，标签节点一定不带，用这个区分。
    """
    return [(node.node_id, node) for node in catalog.iter_nodes(tree)
            if node.resource_type_code is not None]


def test_tree_structure_matches_tags():
    helper, tree = build([book("b1", "语文一年级上册")])
    assert list(tree) == ["tag-edu"]
    assert tree["tag-edu"].display_name == "电子教材"
    primary = tree["tag-edu"].children["tag-primary"]
    assert primary.display_name == "小学"
    assert set(primary.children) == {"tag-chinese", "tag-math"}


def test_books_land_under_their_tag():
    helper, tree = build([book("b1", "语文一年级上册")])
    chinese = tree["tag-edu"].children["tag-primary"].children["tag-chinese"]
    assert "b1" in chinese.children
    assert chinese.children["b1"].display_name == "语文一年级上册"


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
    assert len({node.display_name for _, node in found}) == 1


def test_display_name_falls_back_to_name_then_id():
    books = [
        book("only-name", None, name="只有 name"),
        book("neither", None),
    ]
    books[0].pop("title")
    books[1].pop("title")
    helper, tree = build(books)
    names = {book_id: node.display_name for book_id, node in leaves(tree)}
    assert names["only-name"] == "只有 name"
    assert names["neither"] == "(未知电子课本 neither)"


def test_only_the_textbook_endpoints_are_touched():
    """目录构建只碰电子课本那三个接口，不该有别的。"""
    helper, tree = build([book("b1", "语文一年级上册")])
    urls = [url for url, _ in helper.client.session.calls]
    assert set(urls) == {catalog.TCH_MATERIAL_VERSION, catalog.TCH_MATERIAL_TAGS, LIST_A}, urls
    assert not any("national_lesson" in u for u in urls)


# ---- 任务 11：字段裁剪 ----

KEPT_FIELDS = {"node_id", "display_name", "resource_type_code", "children"}
DROPPED_FIELDS = ["global_description", "custom_properties", "update_time",
                  "tag_paths", "title", "name", "id"]


def test_every_node_keeps_only_the_whitelisted_fields():
    """递归到叶子：整棵树上没有任何节点带着被裁掉的大字段。"""
    books = [
        book("b1", "语文一年级上册"),
        book("b2", "数学一年级上册", tag_path="教材/tag-edu/tag-primary/tag-math"),
        book("b3", "语文一年级下册"),
    ]
    helper, tree = build(books)

    visited = 0
    for node in catalog.iter_nodes(tree):
        visited += 1
        assert set(vars(node)) == KEPT_FIELDS, (node.node_id, sorted(vars(node)))
        for dropped in DROPPED_FIELDS:
            assert not hasattr(node, dropped), (node.node_id, dropped)
    # 电子教材 + 小学 + 初中 + 语文 + 数学 + 3 本课本
    assert visited == 8, visited
    print("遍历节点总数:", visited)


def test_trimmed_tree_is_far_smaller_than_the_raw_entries():
    """裁剪要真的省下体积，而不只是换了个容器。"""
    import json
    import sys

    books = [book("b%d" % i, "课本 %d" % i) for i in range(50)]
    raw_size = len(json.dumps(books, ensure_ascii=False).encode("utf-8"))

    helper, tree = build(books)
    trimmed_size = len(json.dumps({k: v.to_dict() for k, v in tree.items()},
                                  ensure_ascii=False).encode("utf-8"))
    print("原始 %d 字节 -> 裁剪后 %d 字节" % (raw_size, trimmed_size))
    assert trimmed_size * 4 < raw_size, (raw_size, trimmed_size)
    assert sys.getsizeof(tree) > 0  # 树本身仍然可用


def test_round_trip_through_dict_preserves_the_tree():
    """to_dict / from_dict 用于缓存落盘，必须是等价变换。"""
    helper, tree = build([book("b1", "语文一年级上册")])
    restored = {k: catalog.CatalogNode.from_dict(v.to_dict()) for k, v in tree.items()}
    assert restored == tree


def test_leaf_resource_type_defaults_when_missing():
    """resource_type_code 缺失时取默认值，不再 KeyError。"""
    entry = book("b1", "语文一年级上册")
    entry.pop("resource_type_code", None)
    helper, tree = build([entry])
    node = dict(leaves(tree))["b1"]
    assert node.resource_type_code == catalog.DEFAULT_RESOURCE_TYPE


def test_leaf_resource_type_is_kept_when_present():
    helper, tree = build([book("b1", "专题", resource_type_code="thematic_course")])
    node = dict(leaves(tree))["b1"]
    assert node.resource_type_code == "thematic_course"


# ---- R1 P1-8：容错分支自己不许再抛 ----

@pytest.mark.parametrize("bad", [None, "不是对象", 42, [], True])
def test_non_object_entries_do_not_break_the_whole_tree(bad, caplog):
    """坏条目未必是字典：日志参数里再调一次 .get() 会把整棵树带走。"""
    books = [
        book("good-1", "语文一年级上册"),
        bad,
        book("good-2", "数学一年级上册", tag_path="教材/tag-edu/tag-primary/tag-math"),
    ]
    with caplog.at_level(logging.DEBUG, logger="tchmaterial_parser.core.catalog"):
        helper, tree = build(books)

    ids = {book_id for book_id, _ in leaves(tree)}
    assert ids == {"good-1", "good-2"}, "一条 %r 把整棵树毁了：%s" % (bad, ids)
    assert helper.skipped_entries == 1


def test_a_page_that_is_not_an_array_is_skipped_not_fatal(caplog):
    """一页坏数据只丢这一页，另外几页照常建树。

    整棵树报废意味着用户失去全部选择功能——这正是 C2 要根治的，
    「一条坏数据」如此，「一页坏数据」更如此。
    """
    good = "https://example.invalid/good.json"
    bad = "https://example.invalid/bad.json"
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(200, json_data={"urls": "%s,%s" % (bad, good)}),
        bad: FakeResponse(200, json_data={"unexpected": "object"}),
        good: FakeResponse(200, json_data=[book("b1", "语文一年级上册")]),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))
    with caplog.at_level(logging.WARNING, logger="tchmaterial_parser.core.catalog"):
        tree = catalog.ResourceHelper(client).fetch_tree()

    ids = {book_id for book_id, _ in leaves(tree)}
    assert ids == {"b1"}, "坏的那一页把好的那一页也带走了：%s" % ids
    assert any("整页跳过" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_all_pages_not_arrays_is_a_format_error(caplog):
    """四个列表文件出自同一个接口，真实的格式变更是四页同时变形。

    此时返回一棵只有分类、没有课本的树，等于把「接口变了」伪装成
    「上游今年没出教材」：调用方看不出失败，会把空树写进缓存。
    """
    page_a = "https://example.invalid/a.json"
    page_b = "https://example.invalid/b.json"
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(
            200, json_data={"urls": "%s,%s" % (page_a, page_b)}),
        page_a: FakeResponse(200, json_data={"data": [], "code": 0}),
        page_b: FakeResponse(200, json_data={"data": [], "code": 0}),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))
    helper = catalog.ResourceHelper(client)

    with caplog.at_level(logging.WARNING, logger="tchmaterial_parser.core.catalog"):
        with pytest.raises(UpstreamFormatError):
            helper.fetch_tree()

    assert helper.skipped_pages == 2, helper.skipped_pages
    assert any("整页跳过" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


def test_pages_that_parse_but_place_nothing_are_a_format_error():
    """页是数组、条目却一本都挂不上，同样是「拿到的不是这个接口该有的东西」。

    单页跳过与「整棵树是空的」是两回事：前者可以容忍，后者必须响亮地失败。
    """
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(200, json_data={"urls": LIST_A}),
        # 结构合法但 tag_paths 指向标签树里不存在的分支
        LIST_A: FakeResponse(200, json_data=[
            book("b1", "语文", tag_path="教材/tag-unknown/tag-x"),
            book("b2", "数学", tag_path=None),
        ]),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))
    helper = catalog.ResourceHelper(client)

    with pytest.raises(UpstreamFormatError):
        helper.fetch_tree()

    assert helper.skipped_pages == 0, "这一页本身是数组，不该算整页跳过"
    assert helper.skipped_entries == 2


def test_a_single_bad_page_still_yields_a_tree_and_counts_the_page(caplog):
    """单页跳过的容忍度不变，但这一页必须被计数并汇总上报。"""
    good = "https://example.invalid/good.json"
    bad = "https://example.invalid/bad.json"
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(
            200, json_data={"urls": "%s,%s" % (bad, good)}),
        bad: FakeResponse(200, json_data={"unexpected": "object"}),
        good: FakeResponse(200, json_data=[book("b1", "语文一年级上册")]),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))
    helper = catalog.ResourceHelper(client)

    with caplog.at_level(logging.WARNING, logger="tchmaterial_parser.core.catalog"):
        tree = helper.fetch_tree()

    assert {book_id for book_id, _ in leaves(tree)} == {"b1"}
    assert helper.skipped_pages == 1, helper.skipped_pages
    summary = [r.getMessage() for r in caplog.records if "1/2" in r.getMessage()]
    assert summary, [r.getMessage() for r in caplog.records]


# ---- R5-P2-5：整页不可用不止「不是数组」一种 ----

@pytest.mark.parametrize("bad_response, fragment", [
    (FakeResponse(500), "状态码 500"),
    (FakeResponse(200, json_data=None), "不是合法的 JSON"), # json() 抛 ValueError
    (requests.ConnectionError("dropped"), "网络请求失败"),
    (FakeResponse(200, json_data={"data": []}), "不是数组"),
])
def test_any_kind_of_unusable_page_is_skipped_not_fatal(bad_response, fragment, caplog):
    """一页 5xx、半截 JSON、连不上，都不该带走另外三页已经拿到手的课本。"""
    good = "https://example.invalid/good.json"
    bad = "https://example.invalid/bad.json"
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(
            200, json_data={"urls": "%s,%s" % (bad, good)}),
        bad: bad_response,
        good: FakeResponse(200, json_data=[book("b1", "语文一年级上册")]),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))
    helper = catalog.ResourceHelper(client)

    with caplog.at_level(logging.WARNING, logger="tchmaterial_parser.core.catalog"):
        tree = helper.fetch_tree()

    assert {book_id for book_id, _ in leaves(tree)} == {"b1"}, "坏的那一页把好的带走了"
    assert helper.skipped_pages == 1
    # 原因要落到日志里：四种不可用共用一条跳过路径，分不清是哪一种就没法排查
    messages = [r.getMessage() for r in caplog.records]
    assert any("整页跳过" in m for m in messages), messages
    assert any(fragment in m for m in messages), (fragment, messages)


@pytest.mark.parametrize("bad_response, fragment", [
    (FakeResponse(500), "状态码 500"),
    (requests.ConnectionError("dropped"), "网络请求失败"),
])
def test_all_pages_unusable_reports_the_real_reason(bad_response, fragment):
    """全都不可用时要响亮失败，且带上第一页的真实原因而不是一句「不是数组」。"""
    page_a = "https://example.invalid/a.json"
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(200, json_data={"urls": page_a}),
        page_a: bad_response,
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))

    with pytest.raises(UpstreamFormatError) as excinfo:
        catalog.ResourceHelper(client).fetch_tree()
    assert fragment in str(excinfo.value), str(excinfo.value)


def test_an_expired_token_is_reported_as_such_not_as_a_format_error():
    """401 四页都会中，把它降级成格式错误会抹掉用户唯一能据以行动的信息。"""
    page_a = "https://example.invalid/a.json"
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(200, json_data={"urls": page_a}),
        page_a: FakeResponse(401),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))

    with pytest.raises(AuthError) as excinfo:
        catalog.ResourceHelper(client).fetch_tree()
    assert "Token" in str(excinfo.value)


def test_the_summary_line_does_not_claim_every_skipped_page_was_not_an_array(caplog):
    """整页不可用有四种形态，汇总行不该只说其中一种（R6 P2-3）。"""
    good = "https://example.invalid/good.json"
    bad = "https://example.invalid/bad.json"
    routes = {
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        catalog.TCH_MATERIAL_VERSION: FakeResponse(
            200, json_data={"urls": "%s,%s" % (bad, good)}),
        bad: requests.ConnectionError("dropped"), # 连不上，与「不是数组」无关
        good: FakeResponse(200, json_data=[book("b1", "语文一年级上册")]),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))

    with caplog.at_level(logging.WARNING, logger="tchmaterial_parser.core.catalog"):
        catalog.ResourceHelper(client).fetch_tree()

    summary = [m for m in (r.getMessage() for r in caplog.records) if "资源目录构建完成" in m]
    assert summary, [r.getMessage() for r in caplog.records]
    assert not any("不是数组" in m for m in summary), summary
