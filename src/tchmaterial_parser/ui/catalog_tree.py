# -*- coding: utf-8 -*-
"""资源目录的树形选择控件。"""

import tkinter as tk
from tkinter import ttk

DETAIL_URL = ("https://basic.smartedu.cn/tchMaterial/detail"
              "?contentType={resource_type}&contentId={content_id}"
              "&catalogType=tchMaterial&subCatalog=tchMaterial")

MIN_QUERY_LENGTH = 2 # 少于两个字的关键词几乎匹配一切，过滤没有意义
MAX_SEARCH_RESULTS = 200 # 超过这个数说明关键词没有区分度，继续往下翻不如改关键词
PLACEHOLDER_TEXT = "正在加载教材目录…"
TOO_MANY_TEXT = "结果过多，请细化关键词"


def build_detail_url(content_id: str, resource_type: str) -> str:
    return DETAIL_URL.format(resource_type=resource_type or "assets_document", content_id=content_id)


def iter_books(tree, path=()):
    """深度优先产出 (路径, 课本节点)；课本以 resource_type_code 标识。"""
    for node in tree.values():
        if node.resource_type_code is not None:
            yield path, node
        else:
            yield from iter_books(node.children, path + (node.display_name,))


class CatalogTree:
    """按需填充的资源目录树，外加一个搜索框。

    节点直接携带 CatalogNode，选中时读 node.node_id——不再按显示名反查，
    因此同名教材各选各的。
    """

    def __init__(self, parent, on_pick, scale=1.0, height=12):
        self.on_pick = on_pick
        self.resource_list = {}
        self.nodes = {}      # Treeview 的 iid -> CatalogNode
        self.filled = set()  # 已经填充过子节点的 iid，防止重复插入
        self._next_iid = 0

        self.frame = ttk.Frame(parent)

        search_row = ttk.Frame(self.frame)
        search_row.pack(fill="x", pady=(0, int(6 * scale)))
        ttk.Label(search_row, text="搜索教材：").pack(side="left")
        self.query = tk.StringVar()
        self.search_entry = ttk.Entry(search_row, textvariable=self.query)
        self.search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(search_row, text="清除", command=self.clear_search).pack(side="left", padx=(int(6 * scale), 0))
        self.query.trace_add("write", lambda *a: self.refresh())

        tree_row = ttk.Frame(self.frame)
        tree_row.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_row, show="tree", height=height, selectmode="browse")
        scrollbar = ttk.Scrollbar(tree_row, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewOpen>>", self._on_open)
        self.tree.bind("<Double-Button-1>", self._on_activate)
        self.tree.bind("<Return>", self._on_activate)

        self.show_placeholder(PLACEHOLDER_TEXT)

    # ---- 对外 ----

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)

    def set_catalog(self, resource_list, note: str = None) -> None:
        self.resource_list = resource_list or {}
        self.note = note
        self.refresh()

    def clear_search(self) -> None:
        self.query.set("")

    def show_placeholder(self, text: str) -> None:
        self._clear()
        self.tree.insert("", "end", iid="placeholder", text=text)

    def refresh(self) -> None:
        query = self.query.get().strip()
        if len(query) >= MIN_QUERY_LENGTH:
            self._show_search_results(query)
        else:
            self._show_tree()

    # ---- 内部 ----

    def _clear(self) -> None:
        self.tree.delete(*self.tree.get_children(""))
        self.nodes.clear()
        self.filled.clear()

    def _new_iid(self) -> str:
        self._next_iid += 1
        return "n%d" % self._next_iid

    def _insert(self, parent_iid: str, node, text: str = None) -> str:
        iid = self._new_iid()
        self.tree.insert(parent_iid, "end", iid=iid, text=text or node.display_name)
        self.nodes[iid] = node
        if node.children:
            # 占位子项让节点显示成可展开；真正的子节点等展开时才填
            self.tree.insert(iid, "end", iid=iid + "_stub", text="")
        return iid

    def _fill(self, iid: str) -> int:
        """填充一个节点的直接子节点，返回新插入的数量。"""
        if iid in self.filled:
            return 0
        self.filled.add(iid)

        node = self.nodes.get(iid)
        if node is None:
            return 0

        stub = iid + "_stub"
        if self.tree.exists(stub):
            self.tree.delete(stub)

        for child in node.children.values():
            self._insert(iid, child)
        return len(node.children)

    def _show_tree(self) -> None:
        self._clear()
        if not self.resource_list:
            self.tree.insert("", "end", iid="empty", text="（暂无可选教材，可在上方直接粘贴链接）")
            return

        if getattr(self, "note", None):
            self.tree.insert("", "end", iid="note", text=self.note)

        # 只插入根层级；四千余条记录一次性灌进 Treeview 会让界面在插入期间无响应
        for node in self.resource_list.values():
            self._insert("", node)

    def _show_search_results(self, query: str) -> None:
        self._clear()
        shown = 0
        for path, node in iter_books(self.resource_list):
            if query not in node.display_name:
                continue
            if shown >= MAX_SEARCH_RESULTS:
                self.tree.insert("", "end", iid="too_many", text=TOO_MANY_TEXT)
                break
            # 带上完整路径，同名教材才分得清是哪一本
            label = " / ".join(path + (node.display_name,)) if path else node.display_name
            self._insert("", node, text=label)
            shown += 1

        if shown == 0:
            self.tree.insert("", "end", iid="no_match", text="没有匹配的教材")

    def _on_open(self, event=None) -> None:
        iid = self.tree.focus()
        if iid:
            self._fill(iid)

    def _on_activate(self, event=None) -> str:
        iid = self.tree.focus()
        node = self.nodes.get(iid)
        if node is None:
            return "break"

        if node.resource_type_code is None: # 标签节点：展开而不是拼链接
            self._fill(iid)
            self.tree.item(iid, open=not self.tree.item(iid, "open"))
            return "break"

        self.on_pick(build_detail_url(node.node_id, node.resource_type_code))
        return "break"
