# -*- coding: utf-8 -*-
"""核心模块不得 import tkinter。

用子进程跑，避免被同一进程内其他测试导入的 UI 模块污染；PYTHONPATH 显式传入，
不依赖继承——pytest 的 pythonpath 配置只作用于 pytest 自己的进程。
"""

import ast
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")

CORE_MODULES = [
    "tchmaterial_parser",
    "tchmaterial_parser.config",
    "tchmaterial_parser.logging_setup",
    "tchmaterial_parser.core",
    "tchmaterial_parser.core.http",
    "tchmaterial_parser.core.parser",
    "tchmaterial_parser.core.catalog",
    "tchmaterial_parser.core.naming",
    "tchmaterial_parser.core.downloader",
    "tchmaterial_parser.core.tokens",
]

SNIPPET = """
import importlib, sys
for name in {modules!r}:
    importlib.import_module(name)
leaked = sorted(m for m in sys.modules if m == "tkinter" or m.startswith("tkinter."))
assert not leaked, "核心模块把 tkinter 拉了进来: %s" % leaked
print("imported", len({modules!r}), "core modules; tkinter not in sys.modules")
"""


def test_core_modules_do_not_import_tkinter():
    proc = subprocess.run(
        [sys.executable, "-c", SNIPPET.format(modules=CORE_MODULES)],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": SRC, "PATH": os.environ.get("PATH", ""),
             "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "核心模块的无 tkinter 断言失败\nstdout: %s\nstderr: %s" % (proc.stdout, proc.stderr))
    assert "tkinter not in sys.modules" in proc.stdout


def test_core_source_has_no_tkinter_import():
    """静态兜底：即使某个分支从未被执行，import 语句本身也不许出现。"""
    core_dir = os.path.join(SRC, "tchmaterial_parser", "core")
    offenders = []
    for name in sorted(os.listdir(core_dir)):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(core_dir, name), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n == "tkinter" or n.startswith("tkinter.") for n in names):
                offenders.append("%s:%d" % (name, node.lineno))
    assert not offenders, "core 下的模块 import 了 tkinter: %s" % offenders
