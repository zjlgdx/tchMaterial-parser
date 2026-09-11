# -*- coding: utf-8 -*-
"""测试替身。全部离线：任何用例都不应产生真实网络请求。"""

import pytest
import requests


class FakeResponse:
    def __init__(self, status_code=200, chunks=(), json_data=None, headers=None,
                 boom_after=None, on_chunk=None):
        self.status_code = status_code
        self._chunks = list(chunks)
        self._json = json_data
        # requests.Response.headers 是 CaseInsensitiveDict；替身用普通 dict
        # 的话，真实流量里 content-range: 这种写法在测试里从没被走到过
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.headers.setdefault("Content-Length", str(sum(len(c) for c in self._chunks)))
        self._boom_after = boom_after
        self._on_chunk = on_chunk
        self.closed = False

    def close(self):
        # requests.Response 一定有 close()，替身也得有，否则会掩盖真实调用路径
        self.closed = True

    def json(self):
        if self._json is None:
            raise ValueError("该响应没有 JSON 体")
        return self._json

    def iter_content(self, chunk_size=None):
        for i, chunk in enumerate(self._chunks):
            if self._boom_after is not None and i == self._boom_after:
                # 真实的流中断是 requests 的异常，不是裸 OSError——
                # 用错类型会让「网络失败该重试、磁盘失败不该重试」的分类测不出来
                raise requests.exceptions.ChunkedEncodingError("模拟的连接中断")
            if self._on_chunk is not None:
                self._on_chunk(i)
            yield chunk


class FakeSession:
    """按 URL 返回预置响应；未预置的 URL 直接报错，防止用例悄悄依赖网络。"""

    def __init__(self, routes=None, default=None):
        self.routes = dict(routes or {})
        self.default = default
        self.headers = {}
        self.proxies = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url in self.routes:
            response = self.routes[url]
        elif self.default is not None:
            response = self.default
        else:
            raise AssertionError("用例未给这个 URL 预置响应（测试不应打网络）: %s" % url)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(url, **kwargs)
        return response


@pytest.fixture
def fake_session():
    return FakeSession
