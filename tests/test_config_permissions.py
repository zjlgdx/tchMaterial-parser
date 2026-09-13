# 配置文件里存着 Access Token，必须只有文件所有者可读写（0600）。
# 整个测试类在 Windows 上跳过：Windows 的配置存放于注册表，不经过该文件，且 os.chmod 只能修改只读位。
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tchmaterial_parser import config


TOKEN = "test-access-token-value"


@unittest.skipIf(os.name == "nt", "Windows 走注册表分支，且 os.chmod 语义不同（只认只读位）")
class ConfigFilePermissionTest(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.config_path = Path(temp_dir.name) / "tchMaterial-parser" / "data.json"
        patcher = patch.object(config, "config_file_path", lambda: self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def file_mode(self) -> int: # 取出文件的权限位
        return stat.S_IMODE(self.config_path.stat().st_mode)

    def write_existing_file(self, mode: int, **values: str) -> None: # 造一个指定权限的现存配置文件
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(values), encoding="utf-8")
        os.chmod(self.config_path, mode)

    def test_new_file_is_created_with_owner_only_permission(self) -> None:
        previous_umask = os.umask(0o022) # 显式固定 umask，否则在 umask 077 的机器上这条用例会假绿
        try:
            config.save_config(access_token=TOKEN)
        finally:
            os.umask(previous_umask)

        self.assertEqual(self.file_mode(), 0o600)
        self.assertEqual(config.load_config().get("access_token"), TOKEN)

    def test_new_file_is_not_world_readable_while_the_token_is_written(self) -> None:
        observed: list[int] = []
        real_dump = config.json.dump

        def dump_and_record(*args, **kwargs): # 在写入 Token 的瞬间抓一次权限，确认没有「先 0644 建好再收紧」的窗口期
            observed.append(self.file_mode())
            return real_dump(*args, **kwargs)

        previous_umask = os.umask(0o022)
        try:
            with patch.object(config.json, "dump", dump_and_record):
                config.save_config(access_token=TOKEN)
        finally:
            os.umask(previous_umask)

        self.assertEqual(observed, [0o600])

    def test_save_tightens_existing_world_readable_file(self) -> None:
        self.write_existing_file(0o644, access_token="old-token", theme="dark")

        config.save_config(access_token=TOKEN)

        self.assertEqual(self.file_mode(), 0o600)
        stored = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["access_token"], TOKEN)
        self.assertEqual(stored["theme"], "dark") # 其他配置项仍应被合并保留

    def test_save_tightens_existing_file_without_help_from_the_read_path(self) -> None:
        # save_config 会先调用 load_config，为验证写入路径自身也会收紧权限，这里屏蔽读取路径的收紧
        self.write_existing_file(0o644, access_token="old-token")

        with patch.object(config, "restrict_config_file", lambda target_file: None):
            config.save_config(access_token=TOKEN)

        self.assertEqual(self.file_mode(), 0o600)
        self.assertEqual(config.load_config().get("access_token"), TOKEN)

    def test_shorter_save_overwrites_the_whole_file(self) -> None:
        # 保存时必须截断：否则用户清空 Token 后，新内容盖不住旧内容，尾部残留会让配置解析失败，
        # 且刚清除的旧 Access Token 仍以明文留在文件里
        old_token = "A" * 120
        config.save_config(access_token=old_token, theme="dark")

        config.save_config(access_token="") # 用户清空 Token，新内容比旧内容短

        raw = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(set(json.loads(raw)), {"access_token", "theme"}) # 文件仍是合法 JSON
        loaded = config.load_config()
        self.assertEqual(loaded.get("theme"), "dark") # 其他配置项没有一起丢失
        self.assertEqual(loaded.get("access_token"), "")
        self.assertNotIn(old_token, raw) # 旧凭据不得以明文残留

    def test_load_tightens_existing_world_readable_file(self) -> None:
        self.write_existing_file(0o644, access_token=TOKEN)

        self.assertEqual(config.load_config().get("access_token"), TOKEN)
        self.assertEqual(self.file_mode(), 0o600)

    def test_load_keeps_permission_stricter_than_owner_read_write(self) -> None:
        self.write_existing_file(0o400, access_token=TOKEN)

        self.assertEqual(config.load_config().get("access_token"), TOKEN)
        self.assertEqual(self.file_mode(), 0o400) # 比 0600 更严格的权限不应被放宽

    def test_load_still_returns_config_when_tightening_fails(self) -> None:
        self.write_existing_file(0o644, access_token=TOKEN)

        with patch("os.chmod", side_effect=PermissionError("chmod failed")):
            loaded = config.load_config()

        self.assertEqual(loaded.get("access_token"), TOKEN)
        self.assertEqual(self.file_mode(), 0o644)


if __name__ == "__main__":
    unittest.main()
