# -*- coding: utf-8 -*-
"""领域异常。

每一种失败都带一句可直接展示给用户的中文说明——「网络超时」「Token 过期」
「这个页面里没有 PDF」原本全都汇成同一个「无法解析」，用户无从判断该改什么。
"""


class ParserError(Exception):
    def __init__(self, message: str, cause: BaseException = None):
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __str__(self) -> str:
        return self.message


class InvalidUrlError(ParserError):
    """URL 里找不到 contentId，多半是粘错了地址。"""


class ResourceNotFoundError(ParserError):
    """接口响应正常，但里面没有可下载的 PDF。"""


class AuthError(ParserError):
    """401 / 403：Access Token 缺失、过期或无效。"""


class NetworkError(ParserError):
    """连不上、超时，或服务端返回了 4xx / 5xx。"""


class UpstreamFormatError(ParserError):
    """响应能拿到，但结构与预期不符（字段缺失、不是 JSON）。"""
