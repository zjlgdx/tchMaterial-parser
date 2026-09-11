# -*- coding: utf-8 -*-
"""版本号单一来源（C7）。"""

import os
import re
import subprocess
import sys

import tchmaterial_parser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "gen_version_file.py")
VERSION_FILE = os.path.join(REPO_ROOT, "version.txt")


def test_version_is_a_three_part_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", tchmaterial_parser.__version__)


def test_version_txt_is_in_sync():
    """CI 也跑这条：version.txt 落后于 __version__ 就该失败。"""
    proc = subprocess.run([sys.executable, SCRIPT, "--check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_literal_in_version_txt_matches():
    version = tchmaterial_parser.__version__
    major, minor, patch = version.split(".")
    text = open(VERSION_FILE, encoding="utf-8").read()

    assert "filevers=(%s, %s, %s, 0)" % (major, minor, patch) in text
    assert "prodvers=(%s, %s, %s, 0)" % (major, minor, patch) in text
    assert "StringStruct('FileVersion', '%s.0')" % version in text
    assert "StringStruct('ProductVersion', '%s.0')" % version in text


def test_no_hardcoded_version_left_in_sources():
    """源码、spec、README 里都不该再有写死的旧版本号。"""
    targets = [
        os.path.join(REPO_ROOT, "src", "tchMaterial-parser.pyw"),
        os.path.join(REPO_ROOT, "tchMaterial-parser.spec"),
    ]
    for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, "src", "tchmaterial_parser")):
        targets += [os.path.join(dirpath, n) for n in files if n.endswith(".py")]

    offenders = []
    for path in targets:
        text = open(path, encoding="utf-8").read()
        if os.path.basename(path) == "__init__.py" and "__version__" in text:
            continue # 唯一来源本身
        for match in re.finditer(r"v?\d+\.\d+(\.\d+)?", text):
            token = match.group(0)
            if token.startswith("v") and token[1:].count(".") >= 1:
                offenders.append("%s: %s" % (os.path.relpath(path, REPO_ROOT), token))
    assert not offenders, offenders


def test_window_title_derives_from_the_package():
    src = open(os.path.join(REPO_ROOT, "src", "tchmaterial_parser", "ui", "app.py"),
               encoding="utf-8").read()
    assert "__version__" in src
    assert "v3.1" not in src and "v3.2" not in src, "窗口标题里写死了版本号"


def test_generator_is_idempotent(tmp_path, monkeypatch):
    before = open(VERSION_FILE, encoding="utf-8").read()
    proc = subprocess.run([sys.executable, SCRIPT], cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert open(VERSION_FILE, encoding="utf-8").read() == before
