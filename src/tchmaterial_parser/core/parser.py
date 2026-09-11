# -*- coding: utf-8 -*-
"""把资源页面 URL 解析成可直接下载的 PDF 地址。"""

import logging
import re

from .errors import InvalidUrlError, ResourceNotFoundError, UpstreamFormatError

logger = logging.getLogger(__name__)

BASIC_WORK_PATTERN = re.compile(r"^https?://([^/]+)/syncClassroom/basicWork/detail")
PRIVATE_URL_PATTERN = re.compile(
    r"^https?://(.+)-private.ykt.cbern.com.cn/(.+)/"
    r"([\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}).pkg/(?:.+)\.pdf$")
PUBLIC_URL_TEMPLATE = r"https://\1.ykt.cbern.com.cn/\2/\3.pkg/pdf.pdf"

SPECIAL_EDU_DETAIL = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/special_edu/resources/details/{content_id}.json"
TCH_MATERIAL_DETAIL = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{content_id}.json"
THEMATIC_COURSE_LIST = "https://s-file-1.ykt.cbern.com.cn/zxx/ndrs/special_edu/thematic_course/{content_id}/resources/list.json"


def query_value(url: str, key: str) -> str:
    """从 URL 的查询串里取出一个参数。

    不用 urllib.parse 是因为这些地址常被用户手工粘贴，尾部可能带着破损的片段，
    宽松地按 & 与 = 切分反而比严格解析更不容易整条失败。
    """
    for q in url[url.find("?") + 1:].split("&"):
        name, sep, value = q.partition("=")
        # 用 partition 而不是 split("=")[1]：形如 ?contentId（没有等号）的
        # 畸形链接会让下标越界，异常一路穿透到 Tk 回调，用户看不到任何提示
        if name == key and sep:
            return value
    return None


def public_url(resource_url: str, access_token: str) -> str:
    """未登录时，通过一个不可靠的方法构造可直接下载的 URL。"""
    if access_token:
        return resource_url
    return PRIVATE_URL_PATTERN.sub(PUBLIC_URL_TEMPLATE, resource_url)


def pick_pdf_url(ti_items, access_token: str) -> str:
    if ti_items is None:
        return None
    if not isinstance(ti_items, list):
        raise UpstreamFormatError("详情接口的 ti_items 不是列表")

    for item in ti_items:
        if not isinstance(item, dict): # 上游偶尔会混进 null
            continue
        if item.get("lc_ti_format") == "pdf": # 找到存有 PDF 链接列表的项
            storages = item.get("ti_storages")
            if not isinstance(storages, list) or not storages:
                raise UpstreamFormatError("详情接口里的 PDF 条目没有文件地址")
            return public_url(storages[0], access_token)
    return None


def detail_url(url: str, content_id: str, content_type: str) -> str:
    if BASIC_WORK_PATTERN.search(url): # 对于 “基础性作业” 的解析
        return SPECIAL_EDU_DETAIL.format(content_id=content_id)
    if content_type == "thematic_course": # 对专题课程（含电子课本、视频等）的解析
        return SPECIAL_EDU_DETAIL.format(content_id=content_id)
    return TCH_MATERIAL_DETAIL.format(content_id=content_id) # 对普通电子课本的解析


def parse(client, url: str):
    """返回 (PDF 地址, contentId, 标题)。

    失败时抛出 errors 里的具体异常，调用方据此告诉用户到底哪一步出了问题。
    """
    if not isinstance(url, str) or "?" not in url:
        raise InvalidUrlError("这一行不是带查询参数的资源页面网址")

    content_id = query_value(url, "contentId")
    if not content_id:
        raise InvalidUrlError("这一行里找不到 contentId，请确认粘贴的是资源页面的完整网址")

    content_type = query_value(url, "contentType") or "assets_document"

    # 详情接口返回的 $.ti_items 每一项对应一个资源，其中 ti_storages 是文件地址列表
    data = client.get_json(detail_url(url, content_id, content_type))
    if not isinstance(data, dict):
        raise UpstreamFormatError("详情接口返回的结构与预期不符")

    try:
        resource_url = pick_pdf_url(data.get("ti_items"), client.access_token)
    except (KeyError, IndexError, TypeError) as e:
        raise UpstreamFormatError("详情接口里的资源条目缺少文件地址", e) from e

    if not resource_url and content_type == "thematic_course": # 专题课程的 PDF 挂在子资源上
        resources_data = client.get_json(THEMATIC_COURSE_LIST.format(content_id=content_id))
        try:
            for resource in list(resources_data):
                if resource.get("resource_type_code") == "assets_document":
                    resource_url = pick_pdf_url(resource.get("ti_items"), client.access_token)
                    if resource_url:
                        break
        except (KeyError, IndexError, TypeError) as e:
            raise UpstreamFormatError("专题课程的资源列表结构与预期不符", e) from e

    if not resource_url:
        raise ResourceNotFoundError("这个页面里没有可下载的 PDF 资源")

    return resource_url, content_id, data.get("title")
