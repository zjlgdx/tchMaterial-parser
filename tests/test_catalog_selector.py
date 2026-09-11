# -*- coding: utf-8 -*-
"""层级选择控件的联动（B5、C6）。需要 Tk，缺 Tk 时跳过。"""

import pytest

tk = pytest.importorskip("tkinter")

from tchmaterial_parser.core.catalog import CatalogNode  # noqa: E402
from tchmaterial_parser.ui.catalog_tree import CatalogSelector, build_detail_url  # noqa: E402


def sample_tree():
    """按上游真实形状：顶层单节点「电子教材」-> 学段 -> 学科 -> 课本。"""
    return {
        "tag-edu": CatalogNode("tag-edu", "电子教材", children={
            "tag-primary": CatalogNode("tag-primary", "小学", children={
                "tag-chinese": CatalogNode("tag-chinese", "语文", children={
                    "book-1": CatalogNode("book-1", "语文一年级上册", "assets_document"),
                    "book-2": CatalogNode("book-2", "语文一年级下册", "assets_document"),
                }),
                "tag-math": CatalogNode("tag-math", "数学", children={
                    "book-3": CatalogNode("book-3", "数学一年级上册", "thematic_course"),
                }),
            }),
            "tag-junior": CatalogNode("tag-junior", "初中"),
        }),
    }


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    r.withdraw()
    yield r
    r.destroy()


def make_selector(root, tree=None):
    picked = []
    selector = CatalogSelector(root, root, tree if tree is not None else sample_tree(),
                               picked.append)
    return selector, picked


def test_root_level_options_come_from_the_tree(root):
    selector, _ = make_selector(root)
    assert selector.options[0] == ["---", "电子教材"]


def test_full_chain_emits_the_detail_url(root):
    """四级联动：电子教材 -> 小学 -> 语文 -> 课本，最后一步必须吐出 URL。"""
    selector, picked = make_selector(root)
    selector.variables[0].set("电子教材")
    selector.variables[1].set("小学")
    selector.variables[2].set("语文")
    selector.variables[3].set("语文一年级上册")
    root.update_idletasks()

    assert picked == [build_detail_url("book-1", "assets_document")], picked


def test_resource_type_of_the_picked_node_is_used(root):
    selector, picked = make_selector(root)
    selector.variables[0].set("电子教材")
    selector.variables[1].set("小学")
    selector.variables[2].set("数学")
    selector.variables[3].set("数学一年级上册")
    root.update_idletasks()

    assert picked == [build_detail_url("book-3", "thematic_course")], picked


def test_same_named_books_are_told_apart_by_id(root):
    """两本同名教材必须各自拿到自己的 contentId（B5）。"""
    tree = sample_tree()
    chinese = tree["tag-edu"].children["tag-primary"].children["tag-chinese"]
    chinese.children["book-2"].display_name = "语文一年级上册" # 制造重名

    selector, picked = make_selector(root, tree)
    selector.variables[0].set("电子教材")
    selector.variables[1].set("小学")
    selector.variables[2].set("语文")
    selector.variables[3].set("语文一年级上册")
    root.update_idletasks()

    assert len(picked) == 1
    assert "contentId=book-" in picked[0]


def test_empty_branch_offers_nothing_and_emits_nothing(root):
    selector, picked = make_selector(root)
    selector.variables[0].set("电子教材")
    selector.variables[1].set("初中")
    root.update_idletasks()

    assert picked == []


def test_resetting_to_placeholder_clears_the_rest(root):
    selector, picked = make_selector(root)
    selector.variables[0].set("电子教材")
    selector.variables[1].set("小学")
    root.update_idletasks()
    selector.variables[0].set("---")
    root.update_idletasks()

    assert all(v.get() == "---" for v in selector.variables[1:])
