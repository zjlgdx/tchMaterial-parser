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


def test_no_embedded_icon_and_no_shared_temp_file():
    """base64 内嵌与共享临时目录里的固定文件名都不该再出现。"""
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(PACKAGE_DIR):
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            text = open(path, encoding="utf-8").read()
            for needle in ("base64", "gettempdir", "mkstemp"):
                if needle in text:
                    offenders.append("%s: %s" % (os.path.relpath(path, SRC), needle))
    assert not offenders, offenders
