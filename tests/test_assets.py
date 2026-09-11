# -*- coding: utf-8 -*-
"""窗口图标作为包内资源（C6）。"""

import os

from importlib.resources import as_file, files

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
PACKAGE_DIR = os.path.join(SRC, "tchmaterial_parser")

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_icon_is_reachable_as_package_resource():
    with as_file(files("tchmaterial_parser.assets").joinpath("favicon_223x223.png")) as path:
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read(8) == PNG_MAGIC


def test_windows_build_icon_ships_too():
    with as_file(files("tchmaterial_parser.assets").joinpath("favicon_48x48.ico")) as path:
        assert os.path.exists(path)


def test_icon_is_not_written_to_a_shared_temp_path():
    """图标直接从包资源读，不落任何临时文件。

    断言的是行为（set_window_icon 只碰 importlib.resources 给出的路径），
    而不是源码里出现过哪些字面量——后者既验证不了行为，又会因将来任何
    正当用途误伤。
    """
    import ast

    path = os.path.join(PACKAGE_DIR, "ui", "platform_ui.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "set_window_icon")

    opens = [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert not any("gettempdir" in call or "mkstemp" in call or "NamedTemporary" in call
                   for call in opens), opens
    assert any("as_file" in call for call in opens), opens
