# -*- coding: utf-8 -*-
"""资源目录的层级选择控件。"""

import tkinter as tk
from tkinter import ttk
from functools import partial

DEPTH = 8 # 下拉框的数量，也就是能选中的最大层级深度
DETAIL_URL = ("https://basic.smartedu.cn/tchMaterial/detail"
              "?contentType={resource_type}&contentId={content_id}"
              "&catalogType=tchMaterial&subCatalog=tchMaterial")


def build_detail_url(content_id: str, resource_type: str) -> str:
    return DETAIL_URL.format(resource_type=resource_type or "assets_document", content_id=content_id)


class CatalogSelector:
    """一组联动的下拉框：选到叶子节点时把资源页面 URL 插进输入框。"""

    def __init__(self, parent, root, resource_list, on_pick, scale=1.0):
        self.root = root
        self.resource_list = resource_list
        self.on_pick = on_pick
        self.event_flag = False # 防止事件循环调用

        self.frame = ttk.Frame(parent)
        self.options = [["---"] + [resource_list[k]["display_name"] for k in resource_list]] \
            + [["---"] for _ in range(DEPTH - 1)] # 构建选择项
        self.variables = [tk.StringVar(root) for _ in range(DEPTH)]
        self.drops = []

        for i in range(DEPTH):
            drop = ttk.OptionMenu(self.frame, self.variables[i], *self.options[i])
            drop.config(state="active") # 配置下拉菜单为始终活跃状态，保证下拉菜单一直有形状
            drop.bind("<Leave>", lambda e: "break") # 鼠标移出时中止事件传递
            drop.grid(row=i // 4, column=i % 4, padx=int(15 * scale), pady=int(15 * scale)) # 2 行 4 列
            self.variables[i].set("---")
            self.drops.append(drop)

        for index in range(DEPTH): # 绑定事件
            self.variables[index].trace_add("write", partial(self.selection_handler, index))

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def _reset_from(self, start: int) -> None:
        for i in range(start, len(self.drops)):
            self.drops[i]["menu"].delete(0, "end")
            self.drops[i]["menu"].add_command(label="---", command=tk._setit(self.variables[i], "---"))
            self.event_flag = True
            self.variables[i].set("---")

    def selection_handler(self, index: int, *args) -> None:
        if self.event_flag:
            self.event_flag = False # 检测到循环调用，重置标志位并返回
            return

        if self.variables[index].get() == "---": # 重置后面的选择项
            self._reset_from(index + 1)
            return

        if index < len(self.drops) - 1: # 更新选择项
            current_drop = self.drops[index + 1]

            current_hier = self.resource_list
            current_id = [e for e in current_hier if current_hier[e]["display_name"] == self.variables[0].get()][0]
            current_hier = current_hier[current_id]["children"]

            end_flag = False # 是否到达最终目标
            for i in range(index):
                try:
                    current_id = [e for e in current_hier if current_hier[e]["display_name"] == self.variables[i + 1].get()][0]
                    current_hier = current_hier[current_id]["children"]
                except KeyError: # 无法继续向下选择，说明已经到达最终目标
                    end_flag = True
                    break

            if not current_hier or end_flag:
                current_options = ["---"]
            else:
                current_options = ["---"] + [current_hier[k]["display_name"] for k in current_hier.keys()]

            current_drop["menu"].delete(0, "end")
            for choice in current_options:
                current_drop["menu"].add_command(label=choice, command=tk._setit(self.variables[index + 1], choice))

            if end_flag: # 到达目标，显示 URL
                current_id = [e for e in current_hier if current_hier[e]["display_name"] == self.variables[index].get()][0]
                self.on_pick(build_detail_url(current_id, current_hier[current_id]["resource_type_code"]))
                self.drops[-1]["menu"].delete(0, "end")
                self.drops[-1]["menu"].add_command(label="---", command=tk._setit(self.variables[-1], "---"))
                self.variables[-1].set("---")

            for i in range(index + 2, len(self.drops)): # 重置后面的选择项
                self.drops[i]["menu"].delete(0, "end")
                self.drops[i]["menu"].add_command(label="---", command=tk._setit(self.variables[i], "---"))

            for i in range(index + 1, len(self.drops)):
                self.event_flag = True
                self.variables[i].set("---")

        else: # 最后一项，必为最终目标，显示 URL
            if self.variables[-1].get() == "---":
                return

            current_hier = self.resource_list
            current_id = [e for e in current_hier if current_hier[e]["display_name"] == self.variables[0].get()][0]
            current_hier = current_hier[current_id]["children"]
            for i in range(index - 1):
                current_id = [e for e in current_hier if current_hier[e]["display_name"] == self.variables[i + 1].get()][0]
                current_hier = current_hier[current_id]["children"]

            current_id = [e for e in current_hier if current_hier[e]["display_name"] == self.variables[index].get()][0]
            self.on_pick(build_detail_url(current_id, current_hier[current_id]["resource_type_code"]))
