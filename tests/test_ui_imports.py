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
    assert catalog_tree.DEPTH == 8


def test_detail_url_is_built_in_one_place():
    from tchmaterial_parser.ui.catalog_tree import build_detail_url

    url = build_detail_url("abc-123", "assets_document")
    assert "contentId=abc-123" in url
    assert "contentType=assets_document" in url

    # resource_type_code 缺失时不该炸，也不该拼出空的 contentType
    assert "contentType=assets_document" in build_detail_url("abc-123", None)
