# -*- coding: utf-8 -*-
"""URL 解析与失败分类（C1、C4）。"""

import json

import pytest
import requests

from conftest import FakeResponse, FakeSession
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import parser
from tchmaterial_parser.core.errors import (AuthError, InvalidUrlError, NetworkError,
                                            ParserError, ResourceNotFoundError,
                                            UpstreamFormatError)
from tchmaterial_parser.core.http import HttpClient

CONTENT_ID = "4f64356a-8df7-4579-9400-e32c9a7f6718"
DETAIL = parser.TCH_MATERIAL_DETAIL.format(content_id=CONTENT_ID)
SPECIAL = parser.SPECIAL_EDU_DETAIL.format(content_id=CONTENT_ID)
COURSE_LIST = parser.THEMATIC_COURSE_LIST.format(content_id=CONTENT_ID)

PAGE = ("https://basic.smartedu.cn/tchMaterial/detail"
        "?contentType=assets_document&contentId=%s&catalogType=tchMaterial" % CONTENT_ID)


def client_for(routes, access_token=None):
    return HttpClient(config=AppConfig(), session=FakeSession(routes), access_token=access_token)


def detail_body(title="义务教育教科书·数学一年级上册"):
    return {
        "id": CONTENT_ID,
        "title": title,
        "ti_items": [
            {"lc_ti_format": "image/jpg", "ti_storages": ["https://x/cover.jpg"]},
            {"lc_ti_format": "pdf", "ti_storages": [
                "https://r1-ndr-private.ykt.cbern.com.cn/edu_product/esp/assets/%s.pkg/pdf.pdf" % CONTENT_ID]},
        ],
    }


# ---- 查询串提取 ----

@pytest.mark.parametrize("url, key, want", [
    (PAGE, "contentId", CONTENT_ID),
    (PAGE, "contentType", "assets_document"),
    ("https://x/d?a=1&contentId=%s" % CONTENT_ID, "contentId", CONTENT_ID),  # 末位
    ("https://x/d?contentId=%s&a=1" % CONTENT_ID, "contentId", CONTENT_ID),  # 首位
    ("https://x/d?a=1", "contentId", None),
])
def test_query_value(url, key, want):
    assert parser.query_value(url, key) == want


def test_missing_content_type_defaults(tmp_path):
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentId=%s" % CONTENT_ID
    client = client_for({DETAIL: FakeResponse(200, json_data=detail_body())})
    resource_url, content_id, title = parser.parse(client, url)
    assert content_id == CONTENT_ID
    assert resource_url.endswith(".pdf")


# ---- 未登录时的 URL 改写 ----

def test_public_url_rewrite_without_token():
    private = "https://r1-ndr-private.ykt.cbern.com.cn/edu_product/esp/assets/%s.pkg/pdf.pdf" % CONTENT_ID
    got = parser.public_url(private, None)
    assert got == "https://r1-ndr.ykt.cbern.com.cn/edu_product/esp/assets/%s.pkg/pdf.pdf" % CONTENT_ID


def test_public_url_kept_when_token_present():
    private = "https://r1-ndr-private.ykt.cbern.com.cn/edu_product/esp/assets/%s.pkg/pdf.pdf" % CONTENT_ID
    assert parser.public_url(private, "a-token") == private


def test_public_url_leaves_unmatched_url_alone():
    other = "https://example.invalid/whatever.pdf"
    assert parser.public_url(other, None) == other


# ---- 三条分支 ----

def test_basic_work_branch_uses_special_edu_endpoint():
    url = "https://basic.smartedu.cn/syncClassroom/basicWork/detail?contentId=%s" % CONTENT_ID
    client = client_for({SPECIAL: FakeResponse(200, json_data=detail_body("基础性作业"))})
    resource_url, _, title = parser.parse(client, url)
    assert title == "基础性作业"
    assert client.session.calls[0][0] == SPECIAL


def test_thematic_course_falls_back_to_resource_list():
    url = "https://basic.smartedu.cn/tchMaterial/detail?contentType=thematic_course&contentId=%s" % CONTENT_ID
    empty = {"id": CONTENT_ID, "title": "专题课程", "ti_items": [
        {"lc_ti_format": "mp4", "ti_storages": ["https://x/v.mp4"]}]}
    course_resources = [
        {"resource_type_code": "assets_video", "ti_items": []},
        {"resource_type_code": "assets_document", "ti_items": [
            {"lc_ti_format": "pdf", "ti_storages": [
                "https://r2-ndr-private.ykt.cbern.com.cn/edu_product/esp/assets/%s.pkg/pdf.pdf" % CONTENT_ID]}]},
    ]
    client = client_for({SPECIAL: FakeResponse(200, json_data=empty),
                         COURSE_LIST: FakeResponse(200, json_data=course_resources)})
    resource_url, _, title = parser.parse(client, url)
    assert title == "专题课程"
    assert resource_url.endswith(".pdf")
    assert [c[0] for c in client.session.calls] == [SPECIAL, COURSE_LIST]


# ---- 失败分类：三种失败必须彼此可分辨 ----

def test_invalid_url_raises_invalid_url_error():
    client = client_for({})
    with pytest.raises(InvalidUrlError) as excinfo:
        parser.parse(client, "https://basic.smartedu.cn/tchMaterial/detail?foo=bar")
    assert "contentId" in excinfo.value.message


def test_timeout_raises_network_error():
    client = client_for({DETAIL: requests.Timeout("timed out")})
    with pytest.raises(NetworkError) as excinfo:
        parser.parse(client, PAGE)
    assert "超时" in excinfo.value.message


def test_connection_failure_raises_network_error():
    client = client_for({DETAIL: requests.ConnectionError("no route to host")})
    with pytest.raises(NetworkError):
        parser.parse(client, PAGE)


def test_server_error_raises_network_error():
    client = client_for({DETAIL: FakeResponse(500)})
    with pytest.raises(NetworkError) as excinfo:
        parser.parse(client, PAGE)
    assert "500" in excinfo.value.message


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_raises_auth_error(code):
    client = client_for({DETAIL: FakeResponse(code)})
    with pytest.raises(AuthError) as excinfo:
        parser.parse(client, PAGE)
    assert "Access Token" in excinfo.value.message


def test_non_json_body_raises_upstream_format_error():
    client = client_for({DETAIL: FakeResponse(200)})  # json() 会抛 ValueError
    with pytest.raises(UpstreamFormatError) as excinfo:
        parser.parse(client, PAGE)
    assert "JSON" in excinfo.value.message


def test_missing_ti_storages_raises_upstream_format_error():
    broken = {"id": CONTENT_ID, "title": "坏数据", "ti_items": [{"lc_ti_format": "pdf"}]}
    client = client_for({DETAIL: FakeResponse(200, json_data=broken)})
    with pytest.raises(UpstreamFormatError):
        parser.parse(client, PAGE)


def test_no_pdf_raises_resource_not_found():
    body = {"id": CONTENT_ID, "title": "只有视频", "ti_items": [
        {"lc_ti_format": "mp4", "ti_storages": ["https://x/v.mp4"]}]}
    client = client_for({DETAIL: FakeResponse(200, json_data=body)})
    with pytest.raises(ResourceNotFoundError) as excinfo:
        parser.parse(client, PAGE)
    assert "PDF" in excinfo.value.message


def test_every_failure_kind_is_distinguishable():
    """C1 的核心：五种失败不再汇成同一个「无法解析」。"""
    cases = {
        InvalidUrlError: ("https://basic.smartedu.cn/tchMaterial/detail?foo=bar", {}),
        NetworkError: (PAGE, {DETAIL: requests.Timeout("timed out")}),
        AuthError: (PAGE, {DETAIL: FakeResponse(401)}),
        UpstreamFormatError: (PAGE, {DETAIL: FakeResponse(200)}),
        ResourceNotFoundError: (PAGE, {DETAIL: FakeResponse(200, json_data={
            "id": CONTENT_ID, "title": "x", "ti_items": []})}),
    }
    seen = {}
    for expected, (url, routes) in cases.items():
        with pytest.raises(ParserError) as excinfo:
            parser.parse(client_for(routes), url)
        assert type(excinfo.value) is expected, (
            "%s 的场景抛出了 %s" % (expected.__name__, type(excinfo.value).__name__))
        seen[expected.__name__] = excinfo.value.message

    assert len(set(seen.values())) == len(seen), "不同失败给出了相同的文案：%s" % seen
    for name, message in seen.items():
        assert message and not message.startswith("无法解析"), (name, message)


def test_auth_header_applies_to_detail_endpoint():
    """C4：设了 Token，详情接口也要带上。"""
    client = client_for({DETAIL: FakeResponse(200, json_data=detail_body())}, access_token="tok-1")
    parser.parse(client, PAGE)
    assert client.session.headers["X-ND-AUTH"] == 'MAC id="tok-1",nonce="0",mac="0"'


def test_timeout_is_always_injected():
    """B6：每次请求都带 timeout，调用方无从遗漏。"""
    client = client_for({DETAIL: FakeResponse(200, json_data=detail_body())})
    parser.parse(client, PAGE)
    for url, kwargs in client.session.calls:
        assert kwargs.get("timeout") == AppConfig().timeout, url


# ---- R1 P1-6：畸形输入不得逃出领域异常 ----

@pytest.mark.parametrize("url, label", [
    ("https://basic.smartedu.cn/tchMaterial/detail?contentId", "参数没有等号"),
    ("https://basic.smartedu.cn/tchMaterial/detail?", "查询串为空"),
    ("https://basic.smartedu.cn/tchMaterial/detail", "根本没有查询串"),
    ("?contentId", "只有一个残缺参数"),
    ("", "空行"),
    ("不是网址", "不是网址"),
])
def test_malformed_urls_raise_invalid_url_error(url, label):
    client = client_for({})
    with pytest.raises(InvalidUrlError):
        parser.parse(client, url)


@pytest.mark.parametrize("url", [
    "https://basic.smartedu.cn/tchMaterial/detail?contentId",
    "https://x/d?a",
    "https://x/d?=value",
])
def test_query_value_never_raises(url):
    assert parser.query_value(url, "contentId") in (None, "")


@pytest.mark.parametrize("ti_items, label", [
    ([None], "条目是 null"),
    ([None, {"lc_ti_format": "pdf", "ti_storages": ["https://x/a.pdf"]}], "null 混在中间"),
    (["字符串", 42], "条目是标量"),
])
def test_malformed_items_do_not_leak_attribute_errors(ti_items, label):
    """上游偶尔混进 null，不该变成没人接得住的 AttributeError。"""
    body = {"id": CONTENT_ID, "title": "坏数据", "ti_items": ti_items}
    client = client_for({parser.TCH_MATERIAL_DETAIL.format(content_id=CONTENT_ID):
                         FakeResponse(200, json_data=body)})
    try:
        parser.parse(client, PAGE)
    except ParserError:
        pass # 可辨的领域异常，符合预期
    except Exception as exc:
        raise AssertionError("%s 逃出了领域异常：%r" % (label, exc))


def test_ti_items_not_a_list_is_a_format_error():
    body = {"id": CONTENT_ID, "title": "坏数据", "ti_items": {"lc_ti_format": "pdf"}}
    client = client_for({parser.TCH_MATERIAL_DETAIL.format(content_id=CONTENT_ID):
                         FakeResponse(200, json_data=body)})
    with pytest.raises(UpstreamFormatError):
        parser.parse(client, PAGE)


def test_pdf_item_without_storages_is_a_format_error():
    for storages in (None, [], "not-a-list"):
        body = {"id": CONTENT_ID, "title": "坏数据",
                "ti_items": [{"lc_ti_format": "pdf", "ti_storages": storages}]}
        client = client_for({parser.TCH_MATERIAL_DETAIL.format(content_id=CONTENT_ID):
                             FakeResponse(200, json_data=body)})
        with pytest.raises(UpstreamFormatError):
            parser.parse(client, PAGE)
