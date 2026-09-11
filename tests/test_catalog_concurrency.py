# -*- coding: utf-8 -*-
"""目录加载：并行传输、串行解析、可取消（A3、设计硬约束 (a)）。"""

import threading
import time

import pytest

from conftest import FakeResponse
from tchmaterial_parser.config import AppConfig
from tchmaterial_parser.core import catalog
from tchmaterial_parser.core.catalog import CatalogCancelled, ResourceHelper
from tchmaterial_parser.core.http import HttpClient

LIST_URLS = ["https://example.invalid/list-%d.json" % i for i in range(4)]

TAGS = {"hierarchies": [{"children": [
    {"tag_id": "tag-edu", "tag_name": "电子教材", "hierarchies": [{"children": [
        {"tag_id": "tag-primary", "tag_name": "小学", "hierarchies": []},
    ]}]}
]}]}


def books(prefix, count=50):
    return [{"id": "%s-%d" % (prefix, i), "title": "课本 %s-%d" % (prefix, i),
             "tag_paths": ["教材/tag-edu/tag-primary"],
             "resource_type_code": "assets_document"} for i in range(count)]


class TimedSession:
    """记录每个请求的传输区间，并可让传输慢下来以便观察重叠。"""

    def __init__(self, transfer_delay=0.05):
        self.transfer_delay = transfer_delay
        self.headers = {}
        self.proxies = {}
        self.windows = []  # (url, 开始, 结束)
        self.lock = threading.Lock()

    def get(self, url, **kwargs):
        start = time.monotonic()
        if url in LIST_URLS:
            time.sleep(self.transfer_delay) # 模拟 10 MB 的传输耗时
        end = time.monotonic()
        with self.lock:
            self.windows.append((url, start, end))

        if url == catalog.TCH_MATERIAL_VERSION:
            return FakeResponse(200, json_data={"version": "v-1", "urls": ",".join(LIST_URLS)})
        if url == catalog.TCH_MATERIAL_TAGS:
            return FakeResponse(200, json_data=TAGS)
        return FakeResponse(200, json_data=books(url[-6]))


def overlapping(windows):
    """返回有重叠的区间对数。"""
    pairs = 0
    for i in range(len(windows)):
        for j in range(i + 1, len(windows)):
            _, s1, e1 = windows[i]
            _, s2, e2 = windows[j]
            if s1 < e2 and s2 < e1:
                pairs += 1
    return pairs


def make_helper(session, workers=4):
    config = AppConfig(max_catalog_workers=workers)
    return ResourceHelper(HttpClient(config=config, session=session), config=config)


def test_list_transfers_run_in_parallel():
    """四个列表文件的传输区间必须有重叠——否则就是串行。"""
    session = TimedSession(transfer_delay=0.05)
    helper = make_helper(session)
    helper.fetch_tree()

    list_windows = [w for w in session.windows if w[0] in LIST_URLS]
    assert len(list_windows) == 4
    assert overlapping(list_windows) > 0, "四个传输两两不重叠，说明还是串行的"

    span = max(e for _, _, e in list_windows) - min(s for _, s, _ in list_windows)
    serial = sum(e - s for _, s, e in list_windows)
    assert span < serial * 0.8, "总墙钟时间接近串行累加（span=%.3f serial=%.3f）" % (span, serial)


def test_parsing_is_serialised():
    """解析临界区两两不得重叠——同一时刻只能存在一份中间对象。"""
    session = TimedSession(transfer_delay=0.02)
    helper = make_helper(session)

    windows = []
    lock = threading.Lock()
    original = ResourceHelper.parse_and_merge

    def timed(self, url, response, parsed_hier):
        start = time.monotonic()
        try:
            return original(self, url, response, parsed_hier)
        finally:
            time.sleep(0.01) # 拉长临界区，重叠才观察得到
            with lock:
                windows.append((url, start, time.monotonic()))

    ResourceHelper.parse_and_merge = timed
    try:
        tree = helper.fetch_tree()
    finally:
        ResourceHelper.parse_and_merge = original

    assert len(windows) == 4
    assert overlapping(windows) == 0, "解析临界区出现重叠：%s" % windows
    assert len(tree["tag-edu"].children["tag-primary"].children) == 200


def test_all_lists_land_in_the_tree():
    session = TimedSession(transfer_delay=0)
    helper = make_helper(session)
    tree = helper.fetch_tree()
    leaves = tree["tag-edu"].children["tag-primary"].children
    assert len(leaves) == 200, len(leaves)


def test_progress_callback_reports_each_file():
    session = TimedSession(transfer_delay=0)
    helper = make_helper(session)
    seen = []
    helper.fetch_tree(progress_cb=lambda done, total: seen.append((done, total)))
    assert [d for d, _ in seen] == [1, 2, 3, 4]
    assert all(t == 4 for _, t in seen)


# ---- 取消 ----

def test_cancel_before_start_stops_immediately():
    session = TimedSession(transfer_delay=0)
    helper = make_helper(session)
    helper.cancel()
    with pytest.raises(CatalogCancelled):
        helper.fetch_tree()


def test_cancel_mid_flight_exits_within_one_list_file():
    """取消标志置位后，目录线程在一个列表文件的粒度内退出。"""
    session = TimedSession(transfer_delay=0.05)
    helper = make_helper(session, workers=1) # 串行化，便于观察「还剩几个没拉」

    parsed = []
    original = ResourceHelper.parse_and_merge

    def cancel_after_first(self, url, response, parsed_hier):
        parsed.append(url)
        result = original(self, url, response, parsed_hier)
        helper.cancel() # 第一个文件刚处理完就关窗
        return result

    ResourceHelper.parse_and_merge = cancel_after_first
    try:
        with pytest.raises(CatalogCancelled):
            helper.fetch_tree()
    finally:
        ResourceHelper.parse_and_merge = original

    assert len(parsed) == 1, "取消后又多处理了文件：%s" % parsed
    transferred = [w for w in session.windows if w[0] in LIST_URLS]
    assert len(transferred) <= 2, "取消后还在继续传输：%s" % transferred


def test_cancellation_is_not_swallowed_by_the_page_level_tolerance():
    """关窗信号不许被整页容错吞掉（R6 P2-6）。

    传输结束后的那次 _check_cancelled() 就在整页容错的 try 里。
    CatalogCancelled 现在不是 NetworkError / UpstreamFormatError 的子类所以
    穿得过去，但一旦有人把 except 放宽成 Exception，取消就会被记成「这一页
    不可用」然后接着拉下一页——关窗之后目录线程还在闷头拉那四十余 MB。

    直接调 _load_one_list，而不是走 fetch_tree：循环外还有一次
    _check_cancelled()，它会在下一页补上，把「吞掉」这件事整个掩盖过去。
    """
    session = TimedSession(transfer_delay=0)
    helper = make_helper(session, workers=1)

    class CancelRightAfterTransfer:
        """传输刚结束就关窗，正好落在 try 内那次检查之前。"""

        def __init__(self, inner):
            self.inner = inner
            self.headers = inner.headers
            self.proxies = inner.proxies

        def get(self, url, **kwargs):
            response = self.inner.get(url, **kwargs)
            helper.cancel()
            return response

    helper.client.session = CancelRightAfterTransfer(session)

    with pytest.raises(CatalogCancelled):
        helper._load_one_list(LIST_URLS[0], {}, threading.Lock())
