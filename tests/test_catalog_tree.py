# -*- coding: utf-8 -*-
"""树形目录控件：按需填充、搜索、按 id 选中（B5、C6）。需要 Tk，缺 Tk 时跳过。"""

import pytest

tk = pytest.importorskip("tkinter")

from tchmaterial_parser.core.catalog import CatalogNode  # noqa: E402
from tchmaterial_parser.ui import catalog_tree as ct  # noqa: E402
from tchmaterial_parser.ui.catalog_tree import CatalogTree, build_detail_url  # noqa: E402


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("无可用的图形环境: %s" % exc)
    r.withdraw()
    yield r
    r.destroy()


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


def big_tree(books_per_subject=200, subjects=20):
    """约 4000 本课本，用来验证初次插入没有把整棵树灌进去。"""
    stages = {}
    for s in range(subjects):
        children = {"book-%d-%d" % (s, i): CatalogNode("book-%d-%d" % (s, i),
                                                       "课本 %d-%d" % (s, i), "assets_document")
                    for i in range(books_per_subject)}
        stages["subject-%d" % s] = CatalogNode("subject-%d" % s, "学科 %d" % s, children=children)
    return {"tag-edu": CatalogNode("tag-edu", "电子教材", children={
        "tag-primary": CatalogNode("tag-primary", "小学", children=stages)})}


def make(root, tree=None):
    picked = []
    widget = CatalogTree(root, picked.append)
    widget.pack()
    widget.set_catalog(tree if tree is not None else sample_tree())
    root.update_idletasks()
    return widget, picked


def all_items(widget):
    """Treeview 里当前存在的全部 item（含占位子项）。"""
    out = []

    def walk(parent):
        for iid in widget.tree.get_children(parent):
            out.append(iid)
            walk(iid)

    walk("")
    return out


def real_items(widget):
    """排除占位子项后的真实节点数。"""
    return [i for i in all_items(widget) if not i.endswith("_stub")]


# ---- 按需填充 ----

def root_labels(widget):
    return [widget.tree.item(i, "text") for i in real_items(widget)]


def test_initial_insert_only_covers_the_root_level(root):
    widget, _ = make(root)
    assert root_labels(widget) == ["电子教材"]
    assert len(real_items(widget)) == 1


def test_expanding_adds_only_direct_children(root):
    widget, _ = make(root)
    before = len(real_items(widget))

    widget.tree.focus(real_items(widget)[0])
    widget._on_open()
    root.update_idletasks()
    after_first = len(real_items(widget))

    # 电子教材的直接子节点是 小学 / 初中 两个
    assert after_first - before == 2, (before, after_first)

    primary = [i for i, n in widget.nodes.items() if n.display_name == "小学"][0]
    widget.tree.focus(primary)
    widget._on_open()
    root.update_idletasks()

    # 小学的直接子节点是 语文 / 数学 两个；孙子（课本）不该被带出来
    assert len(real_items(widget)) - after_first == 2
    assert not any(n.display_name.startswith("语文一年级") for n in widget.nodes.values())


def test_expanding_twice_does_not_duplicate(root):
    widget, _ = make(root)
    widget.tree.focus(real_items(widget)[0])
    widget._on_open()
    root.update_idletasks()
    count = len(real_items(widget))

    widget._on_open() # 再次触发同一个节点
    root.update_idletasks()
    assert len(real_items(widget)) == count


def test_four_thousand_books_are_not_inserted_up_front(root):
    tree = big_tree()
    total_books = sum(1 for _ in ct.iter_books(tree))
    assert total_books == 4000

    widget, _ = make(root, tree)
    inserted = len(real_items(widget))
    assert inserted == 1, inserted # 只有根层级那一个节点

    widget.tree.focus(real_items(widget)[0])
    widget._on_open()
    root.update_idletasks()
    assert len(real_items(widget)) == 2 # 再多一个「小学」


# ---- 按 id 选中：B5 ----

def test_pick_uses_the_node_id_not_the_display_name(root):
    widget, picked = make(root)
    widget.tree.focus(real_items(widget)[0])
    widget._on_open()
    primary = [i for i, n in widget.nodes.items() if n.display_name == "小学"][0]
    widget.tree.focus(primary)
    widget._on_open()
    chinese = [i for i, n in widget.nodes.items() if n.display_name == "语文"][0]
    widget.tree.focus(chinese)
    widget._on_open()
    root.update_idletasks()

    book = [i for i, n in widget.nodes.items() if n.node_id == "book-1"][0]
    widget.tree.focus(book)
    widget._on_activate()

    assert picked == [build_detail_url("book-1", "assets_document")]


def test_same_named_books_resolve_to_different_ids(root):
    """实测同名教材多达 19 本：同名叶子必须各拼各的 contentId。"""
    tree = sample_tree()
    chinese = tree["tag-edu"].children["tag-primary"].children["tag-chinese"]
    title = "（根据2022年版课程标准修订）义务教育教科书·英语三年级下册"
    chinese.children = {"dup-%d" % i: CatalogNode("dup-%d" % i, title, "assets_document")
                        for i in range(19)}

    widget, picked = make(root, tree)
    widget.query.set(title[:6]) # 走搜索，19 条同名结果一次列全
    root.update_idletasks()

    items = real_items(widget)
    assert len(items) == 19

    for iid in items:
        widget.tree.focus(iid)
        widget._on_activate()

    ids = [u.split("contentId=")[1].split("&")[0] for u in picked]
    assert len(ids) == 19
    assert len(set(ids)) == 19, ids # 19 个互不相同的 contentId


def test_activating_a_tag_node_emits_nothing(root):
    widget, picked = make(root)
    widget.tree.focus(real_items(widget)[0]) # 「电子教材」是标签节点
    widget._on_activate()
    assert picked == []


def test_resource_type_of_the_node_is_used(root):
    tree = sample_tree()
    widget, picked = make(root, tree)
    widget.query.set("数学一年级")
    root.update_idletasks()
    widget.tree.focus(real_items(widget)[0])
    widget._on_activate()
    assert picked == [build_detail_url("book-3", "thematic_course")]


# ---- 搜索 ----

def test_short_query_keeps_the_tree_view(root):
    widget, _ = make(root)
    widget.query.set("语")
    root.update_idletasks()
    assert root_labels(widget) == ["电子教材"] # 仍是树视图


def test_search_shows_full_path(root):
    widget, _ = make(root)
    widget.query.set("语文一年级上册")
    root.update_idletasks()
    labels = [widget.tree.item(i, "text") for i in real_items(widget)]
    assert labels == ["电子教材 / 小学 / 语文 / 语文一年级上册"]


def test_search_result_cap(root):
    """匹配数超过上限时只插入 200 条，并给出提示行。"""
    tree = big_tree(books_per_subject=300, subjects=2) # 600 本，名字都含「课本」
    widget, _ = make(root, tree)
    widget.query.set("课本")
    root.update_idletasks()

    children = widget.tree.get_children("")
    assert len(children) == ct.MAX_SEARCH_RESULTS + 1, len(children)
    assert widget.tree.item(children[-1], "text") == ct.TOO_MANY_TEXT
    assert len([i for i in children if i in widget.nodes]) == ct.MAX_SEARCH_RESULTS


def test_no_match_message(root):
    widget, _ = make(root)
    widget.query.set("不存在的教材名")
    root.update_idletasks()
    labels = [widget.tree.item(i, "text") for i in widget.tree.get_children("")]
    assert labels == ["没有匹配的教材"]


def test_clearing_query_restores_the_tree(root):
    widget, _ = make(root)
    widget.query.set("语文一年级上册")
    root.update_idletasks()
    widget.clear_search()
    root.update_idletasks()
    assert root_labels(widget) == ["电子教材"]


def test_offline_note_is_shown(root):
    widget, _ = make(root)
    widget.set_catalog(sample_tree(), note="（离线缓存，内容可能不是最新的）")
    root.update_idletasks()
    labels = [widget.tree.item(i, "text") for i in widget.tree.get_children("")]
    assert labels[0] == "（离线缓存，内容可能不是最新的）"


def test_placeholder_before_catalog_arrives(root):
    widget = CatalogTree(root, lambda url: None)
    widget.pack()
    root.update_idletasks()
    labels = [widget.tree.item(i, "text") for i in widget.tree.get_children("")]
    assert labels == [ct.PLACEHOLDER_TEXT]
