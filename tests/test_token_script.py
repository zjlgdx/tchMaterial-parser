# 控制台取凭据脚本必须指向 ND_UC_AUTH-...&token，避免误读账号缓存（#89）。
import unittest
from pathlib import Path

from src.tchmaterial_parser.ui.token_window import ACCESS_TOKEN_SCRIPT

README_PATH = Path(__file__).resolve().parents[1] / "README.md"


class AccessTokenScriptTest(unittest.TestCase):
    def test_selects_token_suffix_not_first_auth_key(self) -> None:
        self.assertIn('/^ND_UC_AUTH-[^&]+&[^&]+&token$/.test(key)', ACCESS_TOKEN_SCRIPT)
        self.assertNotIn(
            'key.startsWith("ND_UC_AUTH")\n',
            ACCESS_TOKEN_SCRIPT,
        )

    def test_readme_script_uses_the_same_token_key_rule(self) -> None:
        readme = README_PATH.read_text(encoding="utf-8")
        self.assertIn('/^ND_UC_AUTH-[^&]+&[^&]+&token$/.test(key)', readme)
        self.assertIn("&sdk_cache", readme)
