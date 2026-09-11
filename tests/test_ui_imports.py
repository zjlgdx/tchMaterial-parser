# -*- coding: utf-8 -*-
"""界面模块的导入冒烟。无头环境不保证有 Tk，缺 Tk 时跳过而非失败。"""

import pytest

pytest.importorskip("tkinter")


def test_ui_modules_import():
    import tchmaterial_parser.ui.app as app
    import tchmaterial_parser.ui.catalog_tree as catalog_tree
    import tchmaterial_parser.ui.platform_ui as platform_ui
    import tchmaterial_parser.ui.token_dialog as token_dialog

    assert callable(app.main)
    assert callable(platform_ui.apply_dpi_scaling)
    assert callable(token_dialog.show_access_token_window)
    assert callable(catalog_tree.build_detail_url)


def test_detail_url_is_built_in_one_place():
    from tchmaterial_parser.ui.catalog_tree import build_detail_url

    url = build_detail_url("abc-123", "assets_document")
    assert "contentId=abc-123" in url
    assert "contentType=assets_document" in url

    # resource_type_code 缺失时不该炸，也不该拼出空的 contentType
    assert "contentType=assets_document" in build_detail_url("abc-123", None)


def test_closing_cancels_the_download_pool(monkeypatch):
    """线程池的工作线程不是守护线程，关窗必须显式取消，否则进程残留。"""
    import ast
    import os

    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "tchmaterial_parser", "ui", "app.py")
    tree = ast.parse(open(src_path, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "on_closing")
    calls = [ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert "self.downloads.cancel_all" in calls, calls
    assert "self.root.destroy" in calls, calls


def source_of(module_name, func_name):
    import ast
    import os as _os

    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "src", "tchmaterial_parser", "ui", module_name)
    tree = ast.parse(open(path, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == func_name)
    return ast, fn


def test_token_dialog_does_not_reenable_the_download_button():
    """按钮的恢复只能由「没有任务在飞」派生（R1 P1-1）。"""
    ast, fn = source_of("app.py", "open_token_window")
    body = ast.unparse(fn)
    assert "download_btn" not in body, body


def test_token_dialog_warns_on_failure():
    """保存失败要用警告图标，且不执行 on_saved（R1 P1-9）。"""
    ast, fn = source_of("token_dialog.py", "save_token")
    branches = [n for n in ast.walk(fn) if isinstance(n, ast.If)]
    assert branches, "save_token 里没有区分成功与失败"

    failure_branch = "\n".join(ast.unparse(stmt) for stmt in branches[0].body)
    success_branch = "\n".join(ast.unparse(stmt) for stmt in branches[0].orelse)
    assert "showwarning" in failure_branch, failure_branch
    assert "on_saved" not in failure_branch, failure_branch
    assert "on_saved" in success_branch, success_branch
