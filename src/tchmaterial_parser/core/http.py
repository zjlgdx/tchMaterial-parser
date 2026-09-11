# -*- coding: utf-8 -*-
"""唯一的网络出口。超时与鉴权头都在这里注入，调用方无从遗漏。"""

import logging

import requests

from ..config import AppConfig

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
        kwargs.setdefault("timeout", self.config.timeout)
        return self.session.get(url, **kwargs)

    def get_json(self, url: str):
        return self.get(url).json()

    def stream(self, url: str, headers: dict = None):
        return self.get(url, stream=True, headers=headers)
