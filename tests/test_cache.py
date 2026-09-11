# -*- coding: utf-8 -*-
"""版本键缓存与启动加载流程（A3、任务 9/10）。"""

import json
import os

import pytest
import requests

from conftest import FakeResponse, FakeSession
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import cache, catalog
from tchmaterial_parser.core.catalog import CatalogNode
from tchmaterial_parser.core.http import HttpClient

LIST_URL = "https://example.invalid/list-a.json"

TAGS = {"hierarchies": [{"children": [
    {"tag_id": "tag-edu", "tag_name": "电子教材", "hierarchies": [{"children": [
        {"tag_id": "tag-primary", "tag_name": "小学", "hierarchies": []},
    ]}]}
]}]}

BOOKS = [{
    "id": "book-1", "title": "语文一年级上册",
    "tag_paths": ["教材/tag-edu/tag-primary"],
    "resource_type_code": "assets_document",
    "global_description": "x" * 2000,
}]


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """缓存写到临时目录，绝不碰用户真实的缓存。"""
    monkeypatch.setattr(cache.config, "cache_dir", lambda: str(tmp_path / "cache"))
    yield


def make_client(version_payload, *, tags=TAGS, books=BOOKS, version_error=None):
    routes = {
        catalog.TCH_MATERIAL_VERSION: version_error or FakeResponse(200, json_data=version_payload),
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=tags),
        LIST_URL: FakeResponse(200, json_data=books),
    }
    return HttpClient(config=AppConfig(), session=FakeSession(routes))


def sample_tree():
    return {"tag-edu": CatalogNode("tag-edu", "电子教材", children={
        "book-1": CatalogNode("book-1", "语文一年级上册", "assets_document")})}


# ---- fetch_version：必须廉价 ----

def test_fetch_version_issues_exactly_one_request():
    """版本探测只碰 data_version.json，不触碰那几个大列表文件。"""
    client = make_client({"version": "v-1", "urls": LIST_URL})
    version = catalog.ResourceHelper(client).fetch_version()

    assert version.version == "v-1"
    assert version.urls == (LIST_URL,)
    assert len(client.session.calls) == 1, client.session.calls
    assert client.session.calls[0][0] == catalog.TCH_MATERIAL_VERSION
    assert LIST_URL not in [url for url, _ in client.session.calls]
    assert catalog.TCH_MATERIAL_TAGS not in [url for url, _ in client.session.calls]


def test_fetch_version_falls_back_to_body_digest():
    """上游没给版本字段时，用响应体摘要当键——内容变则键变。"""
    client = make_client({"urls": LIST_URL})
    first = catalog.ResourceHelper(client).fetch_version()
    assert len(first.version) == 16

    same = catalog.ResourceHelper(make_client({"urls": LIST_URL})).fetch_version()
    assert same.version == first.version

    other = catalog.ResourceHelper(make_client({"urls": LIST_URL + "?x=2"})).fetch_version()
    assert other.version != first.version


def test_fetch_version_splits_multiple_urls():
    client = make_client({"version": "v-1", "urls": "a.json,b.json,c.json,d.json"})
    assert catalog.ResourceHelper(client).fetch_version().urls == ("a.json", "b.json", "c.json", "d.json")


# ---- 存取往返 ----

def test_store_then_load_round_trip():
    tree = sample_tree()
    assert cache.store("v-1", tree) is True
    assert cache.load("v-1") == tree


def test_load_misses_on_version_change():
    cache.store("v-1", sample_tree())
    assert cache.load("v-2") is None


def test_load_any_ignores_version():
    cache.store("v-1", sample_tree())
    version, tree = cache.load_any()
    assert version == "v-1"
    assert tree == sample_tree()


def test_corrupted_cache_is_treated_as_a_miss():
    cache.store("v-1", sample_tree())
    with open(cache.cache_file(), "w", encoding="utf-8") as f:
        f.write('{"version": "v-1", "tre')  # 被截断的 JSON
    assert cache.load("v-1") is None
    assert cache.load_any() is None


def test_cache_without_tree_key_is_a_miss():
    os.makedirs(os.path.dirname(cache.cache_file()), exist_ok=True)
    with open(cache.cache_file(), "w", encoding="utf-8") as f:
        json.dump({"version": "v-1"}, f)
    assert cache.load("v-1") is None


def test_store_overwrites_and_leaves_no_temp_file():
    cache.store("v-1", sample_tree())
    cache.store("v-2", sample_tree())
    assert cache.load("v-2") is not None
    directory = os.path.dirname(cache.cache_file())
    assert [n for n in os.listdir(directory) if n.endswith(".tmp")] == []


# ---- 启动加载流程：缓存命中就完全不拉那四十余 MB ----

def load_catalog_with(client, **kwargs):
    from tchmaterial_parser.core.startup import load_catalog
    return load_catalog(client, **kwargs)


def test_cold_start_fetches_and_stores():
    client = make_client({"version": "v-1", "urls": LIST_URL})
    tree, stale, failure = load_catalog_with(client)

    assert failure is None and stale is False
    urls = [url for url, _ in client.session.calls]
    assert LIST_URL in urls, "冷启动应当真的去拉列表文件"
    assert cache.load("v-1") is not None, "冷启动之后缓存要落盘"


def test_warm_start_never_calls_fetch_tree(monkeypatch):
    """缓存命中时 fetch_tree 一次都不许被调用——这是「秒开」成立的唯一证据。"""
    cache.store("v-1", sample_tree())

    called = []
    monkeypatch.setattr(catalog.ResourceHelper, "fetch_tree",
                        lambda self, *a, **kw: called.append(1))

    client = make_client({"version": "v-1", "urls": LIST_URL})
    tree, stale, failure = load_catalog_with(client)

    assert called == [], "缓存命中却仍然调用了 fetch_tree"
    assert failure is None and stale is False
    assert tree == sample_tree()

    urls = [url for url, _ in client.session.calls]
    assert urls == [catalog.TCH_MATERIAL_VERSION], urls
    assert LIST_URL not in urls
    assert catalog.TCH_MATERIAL_TAGS not in urls


def test_version_change_invalidates_cache_and_refetches():
    cache.store("v-old", sample_tree())
    client = make_client({"version": "v-new", "urls": LIST_URL})
    tree, stale, failure = load_catalog_with(client)

    assert failure is None and stale is False
    urls = [url for url, _ in client.session.calls]
    assert LIST_URL in urls, "版本变了就必须重新拉取"
    assert cache.load("v-new") is not None
    assert "book-1" in tree["tag-edu"].children["tag-primary"].children


def test_offline_falls_back_to_stale_cache():
    cache.store("v-1", sample_tree())
    client = make_client({}, version_error=requests.ConnectionError("offline"))
    tree, stale, failure = load_catalog_with(client)

    assert failure is None
    assert stale is True, "离线回退必须标记为过时"
    assert tree == sample_tree()


def test_offline_without_cache_reports_the_reason():
    client = make_client({}, version_error=requests.ConnectionError("offline"))
    tree, stale, failure = load_catalog_with(client)

    assert tree == {}
    assert stale is False
    assert failure and "无法解析" not in failure


def test_list_fetch_failure_also_falls_back():
    """版本探测成功、拉列表时断网，同样回退到旧缓存。"""
    cache.store("v-old", sample_tree())
    routes = {
        catalog.TCH_MATERIAL_VERSION: FakeResponse(200, json_data={"version": "v-new", "urls": LIST_URL}),
        catalog.TCH_MATERIAL_TAGS: FakeResponse(200, json_data=TAGS),
        LIST_URL: requests.ConnectionError("dropped"),
    }
    client = HttpClient(config=AppConfig(), session=FakeSession(routes))
    tree, stale, failure = load_catalog_with(client)

    assert failure is None
    assert stale is True
    assert tree == sample_tree()
