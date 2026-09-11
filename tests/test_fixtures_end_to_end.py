# -*- coding: utf-8 -*-
"""用录制的 JSON fixture 走完整链路（任务 20）。

fixture 是按真实响应结构手工裁剪的小样本；这些用例不打网络，
FakeSession 对未预置的 URL 直接报错。
"""

import json
import os

import pytest

from conftest import FakeResponse, FakeSession
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import cache, catalog, naming, parser
from tchmaterial_parser.core.errors import ResourceNotFoundError
from tchmaterial_parser.core.http import HttpClient
from tchmaterial_parser.core.startup import load_catalog

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CID = "4f64356a-8df7-4579-9400-e32c9a7f6718"


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cache.config, "cache_dir", lambda: str(tmp_path / "cache"))
    naming.clear_reservations()
    yield
    naming.clear_reservations()


def test_all_seven_fixtures_exist_and_are_small():
    names = sorted(os.listdir(FIXTURES))
    assert names == sorted([
        "book_list_page.json", "data_version.json", "details_no_pdf.json",
        "details_tch_material.json", "details_thematic_course.json",
        "tch_material_tag.json", "thematic_course_resources.json",
    ]), names
    for name in names:
        size = os.path.getsize(os.path.join(FIXTURES, name))
        assert size < 32 * 1024, "%s 有 %d 字节，fixture 应当是小样本" % (name, size)


# ---- 解析链路 ----

def test_parse_normal_textbook_from_fixture():
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document&contentId=%s" % CID
    routes = {parser.TCH_MATERIAL_DETAIL.format(content_id=CID):
              FakeResponse(200, json_data=fixture("details_tch_material.json"))}
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))

    resource_url, content_id, title = parser.parse(client, url)
    assert content_id == CID
    assert title == "义务教育教科书·数学一年级上册"
    # 未设 Token 时改写成可直接下载的地址
    assert resource_url == "https://r1-ndr.ykt.cbern.com.cn/edu_product/esp/assets/%s.pkg/pdf.pdf" % CID


def test_parse_keeps_private_url_when_token_is_set():
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document&contentId=%s" % CID
    routes = {parser.TCH_MATERIAL_DETAIL.format(content_id=CID):
              FakeResponse(200, json_data=fixture("details_tch_material.json"))}
    client = HttpClient(config=AppConfig(), session=FakeSession(routes), access_token="tok")

    resource_url = parser.parse(client, url)[0]
    assert "-private." in resource_url


def test_parse_thematic_course_falls_back_to_resource_list():
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentType=thematic_course&contentId=%s" % CID
    routes = {
        parser.SPECIAL_EDU_DETAIL.format(content_id=CID):
            FakeResponse(200, json_data=fixture("details_thematic_course.json")),
        parser.THEMATIC_COURSE_LIST.format(content_id=CID):
            FakeResponse(200, json_data=fixture("thematic_course_resources.json")),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))

    resource_url, _, title = parser.parse(client, url)
    assert title == "专题课程·科学探究"
    assert resource_url.endswith(".pdf")


def test_parse_reports_missing_pdf():
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document&contentId=%s" % CID
    routes = {parser.TCH_MATERIAL_DETAIL.format(content_id=CID):
              FakeResponse(200, json_data=fixture("details_no_pdf.json"))}
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))

    with pytest.raises(ResourceNotFoundError):
        parser.parse(client, url)


# ---- 目录链路 ----

def catalog_client():
    version = fixture("data_version.json")
    routes = {
        catalog.TCH_MATERIAL_VERSION: FakeResponse(200, json_data=version),
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=fixture("tch_material_tag.json")),
    }
    page = fixture("book_list_page.json")
    for url in version["urls"].split(","):
        routes[url] = FakeResponse(200, json_data=page)
    return HttpClient(config=AppConfig(), session=FakeSession(routes)), version


def test_fetch_version_from_fixture():
    client, version = catalog_client()
    parsed = catalog.ResourceHelper(client).fetch_version()
    assert parsed.version == "20250518-01"
    assert len(parsed.urls) == 4
    assert len(client.session.calls) == 1


def test_tree_built_from_fixture_skips_bad_entries():
    client, _ = catalog_client()
    helper = catalog.ResourceHelper(client)
    tree = helper.fetch_tree()

    chinese = tree["tag-edu"].children["tag-primary"].children["tag-chinese"]
    math = tree["tag-edu"].children["tag-primary"].children["tag-math"]

    # 每个列表文件都含同样 6 条：2 条同名 + 1 条空 tag_paths + 1 条坏分支
    # + 1 条无 title + 1 条含非法字符
    assert set(chinese.children) == {"book-dup-1", "book-dup-2"}
    assert set(math.children) == {"book-no-title", "book-odd-name"}
    assert helper.skipped_entries == 4 * 2, helper.skipped_entries # 空 tag_paths 与坏分支各 4 次


def test_duplicate_titles_survive_with_distinct_ids():
    client, _ = catalog_client()
    tree = catalog.ResourceHelper(client).fetch_tree()
    chinese = tree["tag-edu"].children["tag-primary"].children["tag-chinese"]

    names = {node.display_name for node in chinese.children.values()}
    assert len(names) == 1, "两条记录本该同名"
    assert len(chinese.children) == 2, "同名被合并了"


def test_display_name_falls_back_to_name():
    client, _ = catalog_client()
    tree = catalog.ResourceHelper(client).fetch_tree()
    math = tree["tag-edu"].children["tag-primary"].children["tag-math"]
    assert math.children["book-no-title"].display_name == "只有 name 字段的课本"


def test_big_fields_are_trimmed_everywhere():
    client, _ = catalog_client()
    tree = catalog.ResourceHelper(client).fetch_tree()
    visited = 0
    for node in catalog.iter_nodes(tree):
        visited += 1
        assert set(vars(node)) == {"node_id", "display_name", "resource_type_code", "children"}
        assert not hasattr(node, "global_description")
        assert not hasattr(node, "custom_properties")
    assert visited == 9, visited # 5 个标签节点 + 4 本课本


def test_odd_title_from_fixture_is_sanitised(tmp_path):
    client, _ = catalog_client()
    tree = catalog.ResourceHelper(client).fetch_tree()
    node = tree["tag-edu"].children["tag-primary"].children["tag-math"].children["book-odd-name"]

    assert node.display_name == "  义务教育教科书/数学:一年级上册  ", node.display_name

    safe = naming.sanitize_filename(node.display_name)
    assert safe == "义务教育教科书_数学_一年级上册", safe # 斜杠与冒号被替换，首尾空白被去掉


def test_resource_type_from_fixture_is_preserved():
    client, _ = catalog_client()
    tree = catalog.ResourceHelper(client).fetch_tree()
    math = tree["tag-edu"].children["tag-primary"].children["tag-math"]
    assert math.children["book-odd-name"].resource_type_code == "thematic_course"
    assert math.children["book-no-title"].resource_type_code == "assets_document"


# ---- 启动链路：冷启动 -> 缓存 -> 热启动 ----

def test_cold_then_warm_start_with_fixtures():
    client, version = catalog_client()
    tree, stale, failure = load_catalog(client)
    assert failure is None and stale is False
    assert cache.load(version["version"]) is not None

    warm_client, _ = catalog_client()
    warm_tree, warm_stale, warm_failure = load_catalog(warm_client)

    assert warm_failure is None and warm_stale is False
    assert warm_tree == tree
    assert [url for url, _ in warm_client.session.calls] == [catalog.TCH_MATERIAL_VERSION]
