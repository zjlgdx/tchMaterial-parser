#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从包里的 __version__ 生成 PyInstaller 用的 version.txt。

版本号只有一个来源。这个脚本既能生成，也能用 --check 校验仓库里的
version.txt 有没有落后——单一来源靠校验而不是靠自觉。
"""

import argparse
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")
VERSION_FILE = os.path.join(REPO_ROOT, "version.txt")

TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x4,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
    ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404B0',
        [
          StringStruct('CompanyName', '肥宅水水呀'),
          StringStruct('FileDescription', '国家中小学智慧教育平台 资源下载工具'),
          StringStruct('FileVersion', '{major}.{minor}.{patch}.0'),
          StringStruct('InternalName', 'tchMaterial-parser'),
          StringStruct('LegalCopyright', 'Copyright © 2025 肥宅水水呀'),
          StringStruct('OriginalFilename', 'tchMaterial-parser.exe'),
          StringStruct('ProductName', '国家中小学智慧教育平台 资源下载工具'),
          StringStruct('ProductVersion', '{major}.{minor}.{patch}.0'),
          StringStruct('Comments', '国中小学智慧教育平台 资源下载工具')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def read_version() -> str:
    """直接从源文件里抠 __version__，避免为了读一个字符串去导入整个包。"""
    init_py = os.path.join(SRC, "tchmaterial_parser", "__init__.py")
    with open(init_py, encoding="utf-8") as f:
        match = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.MULTILINE)
    if not match:
        raise SystemExit("在 %s 里找不到 __version__" % init_py)
    return match.group(1)


def render(version: str) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit("__version__ 必须是 x.y.z 形式，当前为 %r" % version)
    major, minor, patch = parts
    return TEMPLATE.format(major=major, minor=minor, patch=patch)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="只校验 version.txt 是否与 __version__ 一致，不写入")
    args = parser.parse_args(argv)

    version = read_version()
    expected = render(version)

    if args.check:
        try:
            with open(VERSION_FILE, encoding="utf-8") as f:
                actual = f.read()
        except FileNotFoundError:
            print("version.txt 不存在，请运行 python scripts/gen_version_file.py")
            return 1
        if actual != expected:
            print("version.txt 与 __version__ (%s) 不一致，请运行 "
                  "python scripts/gen_version_file.py" % version)
            return 1
        print("version.txt 与 __version__ (%s) 一致" % version)
        return 0

    with open(VERSION_FILE, "w", encoding="utf-8") as f:
        f.write(expected)
    print("已根据 __version__ (%s) 生成 %s" % (version, VERSION_FILE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
