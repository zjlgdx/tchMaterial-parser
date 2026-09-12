from contextlib import ExitStack
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.tchmaterial_parser import catalog, config


TAGS = {"hierarchies": [{"children": [
    {"tag_id": "books", "tag_name": "电子教材", "hierarchies": [{"children": [
        {"tag_id": "primary", "tag_name": "小学", "hierarchies": []},
    ]}]},
]}]}
PART_URLS = [f"https://example.com/tch_material/part_{index}.json" for index in range(4)]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeSession: # 记录每次请求的替身，用于统计缓存命中时是否还在拉取分片
    def __init__(self):
        self.calls = []
        self.module_version = "1"
        self.parts = {
            url: [{"id": f"book-{index}", "title": f"语文 {index}", "tag_paths": ["教材/books/primary"]}]
            for index, url in enumerate(PART_URLS)
        }

    def get(self, url):
        self.calls.append(url)
        if url.endswith("data_version.json"):
            return FakeResponse({"module_version": self.module_version, "urls": ",".join(PART_URLS)})
        if url.endswith("tch_material_tag.json"):
            return FakeResponse(TAGS)
        return FakeResponse(copy.deepcopy(self.parts[url])) # 与真实响应一致，每次解析出新的对象

    def part_calls(self):
        return [url for url in self.calls if url in PART_URLS]


class CatalogCacheTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache_dir = Path(directory.name)
        self.cache_file = self.cache_dir / "catalog-cache.json.gz"
        self.session = FakeSession()
        self.errors = []
        context = ExitStack()
        self.addCleanup(context.close)
        context.enter_context(patch.object(catalog, "session", self.session))
        context.enter_context(patch.object(catalog, "catalog_cache_path", lambda: self.cache_file))
        context.enter_context(patch.object(catalog, "print_error", self.errors.append))

    def fetch(self):
        self.session.calls.clear()
        return catalog.ResourceHelper().fetch_resource_list()

    def cached_payload(self):
        with gzip.open(self.cache_file, "rt", encoding="utf-8") as f:
            return json.load(f)

    def write_cache(self, data):
        self.cache_file.write_bytes(data)

    def temp_files(self):
        return [path.name for path in self.cache_dir.iterdir() if path.name != self.cache_file.name]

    def test_cold_start_fetches_parts_and_writes_cache(self):
        resource_list = self.fetch()
        self.assertEqual(self.session.part_calls(), PART_URLS)
        self.assertEqual(sorted(resource_list["books"]["children"]["primary"]["children"]), ["book-0", "book-1", "book-2", "book-3"])
        self.assertTrue(self.cache_file.exists())
        self.assertEqual(self.cached_payload()["resource_list"], resource_list)

    def test_cache_hit_skips_part_requests(self):
        expected = self.fetch()
        self.assertEqual(self.fetch(), expected)
        self.assertEqual(self.session.part_calls(), []) # 命中缓存时不再拉取分片
        self.assertEqual(len(self.session.calls), 1) # 只请求 data_version.json

    def test_version_change_refetches_and_rewrites_cache(self):
        first = self.fetch()
        self.session.module_version = "2"
        self.session.parts[PART_URLS[0]][0]["title"] = "语文 0（新版）"
        second = self.fetch()
        self.assertEqual(self.session.part_calls(), PART_URLS)
        self.assertNotEqual(second, first)
        self.assertEqual(self.cached_payload()["resource_list"], second)
        self.assertEqual(self.fetch(), second)
        self.assertEqual(self.session.part_calls(), [])

    def test_unusable_cache_is_treated_as_miss(self):
        expected = self.fetch()
        version = self.cached_payload()["version"]
        broken_caches = {
            "不是 gzip 数据": "这不是 gzip 文件".encode("utf-8"),
            "gzip 内不是合法 JSON": gzip.compress(b"{\"version\":"),
            "根节点不是对象": gzip.compress(json.dumps([]).encode("utf-8")),
            "缺少版本字段": gzip.compress(json.dumps({"cache_format": catalog.CACHE_FORMAT, "resource_list": {}}).encode("utf-8")),
            "版本不符": gzip.compress(json.dumps({"cache_format": catalog.CACHE_FORMAT, "version": "别的版本", "resource_list": {}}).encode("utf-8")),
            "资源列表结构异常": gzip.compress(json.dumps({"cache_format": catalog.CACHE_FORMAT, "version": version, "resource_list": "文本"}).encode("utf-8")),
            "缓存结构版本不符": gzip.compress(json.dumps({"cache_format": catalog.CACHE_FORMAT + 1, "version": version, "resource_list": {}}).encode("utf-8")),
            "空文件": b"",
        }
        for name, data in broken_caches.items():
            with self.subTest(name):
                self.write_cache(data)
                self.assertEqual(self.fetch(), expected) # 缓存不可用时重新抓取，而不是抛出异常
                self.assertEqual(self.session.part_calls(), PART_URLS)

    def test_broken_part_response_is_not_written_into_the_cache(self):
        for name, payload in {"空分片": [], "分片不是数组": {"message": "服务异常"}}.items():
            with self.subTest(name):
                self.cache_file.unlink(missing_ok=True)
                original = self.session.parts[PART_URLS[2]]
                self.session.parts[PART_URLS[2]] = payload
                with self.assertRaises(Exception):
                    self.fetch()
                self.assertFalse(self.cache_file.exists()) # 残缺的目录一旦落盘，会一直供到平台改版本号为止
                self.session.parts[PART_URLS[2]] = original
                self.assertEqual(sorted(self.fetch()["books"]["children"]["primary"]["children"]),
                                 ["book-0", "book-1", "book-2", "book-3"]) # 平台恢复后自行痊愈

    def test_unreadable_cache_path_is_treated_as_miss(self):
        expected = self.fetch()
        with patch.object(Path, "exists", side_effect=PermissionError("模拟缓存目录不可访问")): # 目录权限或 ACL 被收紧
            self.assertEqual(self.fetch(), expected) # 读不到缓存只是退化为重新抓取，不应连资源列表一起加载失败
        self.assertEqual(self.session.part_calls(), PART_URLS)
        self.assertTrue(self.errors)

    def test_missing_cache_file_is_treated_as_miss(self):
        expected = self.fetch()
        self.cache_file.unlink()
        self.assertEqual(self.fetch(), expected)
        self.assertEqual(self.session.part_calls(), PART_URLS)

    def test_failed_write_keeps_previous_cache_and_leaves_no_temp_file(self):
        self.fetch()
        previous = self.cache_file.read_bytes()
        self.session.module_version = "2"
        with patch.object(catalog.json, "dump", side_effect=OSError("模拟写入失败")):
            self.fetch()
        self.assertEqual(self.cache_file.read_bytes(), previous) # 写入失败不会破坏已有缓存
        self.assertEqual(self.temp_files(), [])
        self.assertTrue(self.errors)

    def test_successful_write_leaves_no_temp_file(self):
        self.fetch()
        self.assertEqual(self.temp_files(), [])
        self.assertEqual(self.errors, [])

    def test_cache_file_sits_next_to_the_config_file(self):
        cache_file = config.catalog_cache_path()
        self.assertEqual(cache_file.parent, config.config_file_path().parent)
        self.assertEqual(cache_file.name, "catalog-cache.json.gz")


if __name__ == "__main__":
    unittest.main()
