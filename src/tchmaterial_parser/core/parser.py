# -*- coding: utf-8 -*-
"""把资源页面 URL 解析成可直接下载的 PDF 地址。"""

import logging
import re

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
        if q.split("=")[0] == key:
            return q.split("=")[1]
    return None


def public_url(resource_url: str, access_token: str) -> str:
    """未登录时，通过一个不可靠的方法构造可直接下载的 URL。"""
    if access_token:
        return resource_url
    return PRIVATE_URL_PATTERN.sub(PUBLIC_URL_TEMPLATE, resource_url)


def pick_pdf_url(ti_items, access_token: str) -> str:
    for item in list(ti_items or []):
        if item.get("lc_ti_format") == "pdf": # 找到存有 PDF 链接列表的项
            return public_url(item["ti_storages"][0], access_token)
    return None


def detail_url(url: str, content_id: str, content_type: str) -> str:
    if BASIC_WORK_PATTERN.search(url): # 对于 “基础性作业” 的解析
        return SPECIAL_EDU_DETAIL.format(content_id=content_id)
    if content_type == "thematic_course": # 对专题课程（含电子课本、视频等）的解析
        return SPECIAL_EDU_DETAIL.format(content_id=content_id)
    return TCH_MATERIAL_DETAIL.format(content_id=content_id) # 对普通电子课本的解析


def parse(client, url: str):
    """返回 (PDF 地址, contentId, 标题)；解析不出来时三个都是 None。"""
    try:
        content_id = query_value(url, "contentId")
        if not content_id:
            return None, None, None

        content_type = query_value(url, "contentType") or "assets_document"

        # 详情接口返回的 $.ti_items 每一项对应一个资源，其中 ti_storages 是文件地址列表
        data = client.get_json(detail_url(url, content_id, content_type))
        resource_url = pick_pdf_url(data.get("ti_items"), client.access_token)

        if not resource_url and content_type == "thematic_course": # 专题课程的 PDF 挂在子资源上
            resources_data = client.get_json(THEMATIC_COURSE_LIST.format(content_id=content_id))
            for resource in list(resources_data):
                if resource.get("resource_type_code") == "assets_document":
                    resource_url = pick_pdf_url(resource.get("ti_items"), client.access_token)
                    if resource_url:
                        break

        if not resource_url:
            return None, None, None

        return resource_url, content_id, data.get("title")
    except Exception:
        return None, None, None # 如果解析失败，返回 None
