# -*- coding: utf-8 -*-
"""唯一的网络出口。超时与鉴权头都在这里注入，调用方无从遗漏。"""

import logging

import requests

from ..config import AppConfig
from .errors import AuthError, NetworkError, UpstreamFormatError

logger = logging.getLogger(__name__)

# “MAC id”等同于“access_token”，“nonce”和“mac”不可缺省但无需有效
ANONYMOUS_AUTH = 'MAC id="0",nonce="0",mac="0"'


class HttpClient:
    def __init__(self, config: AppConfig = None, session=None, access_token: str = None):
        self.config = config or AppConfig()
        self.session = session if session is not None else requests.Session()
        self.session.proxies = { "http": None, "https": None } # 全局忽略代理
        self.access_token = None
        self.set_access_token(access_token)

    def set_access_token(self, token: str) -> None:
        # 鉴权头挂在 session 上，详情接口与下载接口才会共用同一份凭据
        self.access_token = token or None
        if self.access_token:
            self.session.headers["X-ND-AUTH"] = f'MAC id="{self.access_token}",nonce="0",mac="0"'
        else:
            self.session.headers["X-ND-AUTH"] = ANONYMOUS_AUTH

    def get(self, url: str, **kwargs):
        """发起请求；只把传输层故障翻译成 NetworkError，状态码交给调用方判断。"""
        kwargs.setdefault("timeout", self.config.timeout)
        try:
            return self.session.get(url, **kwargs)
        except requests.Timeout as e:
            logger.warning("请求超时：%s", url)
            raise NetworkError(f"请求超时，服务器在 {self.config.read_timeout:.0f} 秒内没有响应", e) from e
        except requests.RequestException as e:
            logger.warning("网络请求失败：%s（%s）", url, e)
            raise NetworkError(f"网络请求失败：{e}", e) from e

    def get_json(self, url: str):
        response = self.get(url)

        if response.status_code in (401, 403):
            raise AuthError("授权失败，Access Token 可能已过期或无效，请重新设置")
        if response.status_code >= 400:
            raise NetworkError(f"服务器返回状态码 {response.status_code}")

        try:
            return response.json()
        except ValueError as e:
            logger.warning("响应不是合法 JSON：%s", url)
            raise UpstreamFormatError("服务器返回的内容不是合法的 JSON，接口可能已变更", e) from e

    def stream(self, url: str, headers: dict = None):
        """流式下载；状态码由下载器自行处理，以便把失败原因记进任务状态。"""
        return self.get(url, stream=True, headers=headers)
