# -*- coding: utf-8 -*-
"""主窗口与程序生命周期。"""

import logging
import os
import sys
import tkinter as tk
from functools import partial
from tkinter import ttk, messagebox, filedialog

import pyperclip

from .. import __version__
from ..config import AppConfig, os_name
from ..core import tokens
from ..core.catalog import ResourceHelper
from ..core.downloader import DownloadManager, build_save_path
from ..core.http import HttpClient
from ..core.parser import parse
from ..logging_setup import setup_logging
from .catalog_tree import CatalogSelector
from .platform_ui import apply_dpi_scaling, set_window_icon, ui_font
from .token_dialog import attach_context_menu, show_access_token_window

logger = logging.getLogger(__name__)

DESCRIPTION = """\
📌 请在下面的文本框中输入一个或多个资源页面的网址（每个网址一行）。
🔗 资源页面网址示例：
    https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document&contentId=...
📝 您也可以直接在下方的选项卡中选择教材。
📥 点击 “下载” 按钮后，程序会解析并下载资源。
⚠️ 注：为了更可靠地下载，建议点击 “设置 Token” 按钮，参照里面的说明完成设置。"""


class App:
    def __init__(self, config: AppConfig = None):
        self.config = config or AppConfig()
        self.client = HttpClient(config=self.config, access_token=tokens.load_token())

        self.root = tk.Tk()
        self.scale = apply_dpi_scaling(self.root)
        self.root.title(f"国家中小学智慧教育平台 资源下载工具 v{__version__}")
        set_window_icon(self.root)

        # 工作线程只交出纯数据，投递回主线程由这里负责——Tkinter 非线程安全
        self.downloads = DownloadManager(
            self.client, config=self.config,
            on_progress=lambda progress, text: self.root.after(0, partial(self.update_progress, progress, text)),
            on_finish=lambda dir_path, detail: self.root.after(0, partial(self.finish_downloads, dir_path, detail)))

        self.resource_list = {}
        self.build_widgets()
        self.load_resource_list()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing) # 注册窗口关闭事件的处理函数

    # ---- 构建界面 ----

    def build_widgets(self) -> None:
        scale = self.scale
        container = ttk.Frame(self.root)
        container.pack(anchor="center", expand="yes", padx=int(40 * scale), pady=int(20 * scale))
        self.container = container

        title_label = ttk.Label(container, text="国家中小学智慧教育平台 资源下载工具",
                                font=ui_font(16, bold=True, root=self.root))
        title_label.pack(pady=int(5 * scale))

        description_label = ttk.Label(container, text=DESCRIPTION, justify="left",
                                      font=ui_font(9, root=self.root))
        description_label.pack(pady=int(5 * scale))

        # 长宽是字符数而非像素，不跟随缩放
        self.url_text = tk.Text(container, width=70, height=12, font=ui_font(9, root=self.root))
        self.url_text.pack(padx=int(15 * scale), pady=int(15 * scale))
        attach_context_menu(self.url_text, self.root, [
            ("剪切 (Ctrl＋X)", "<<Cut>>"),
            ("复制 (Ctrl＋C)", "<<Copy>>"),
            ("粘贴 (Ctrl＋V)", "<<Paste>>"),
        ])

        self.dropdown_frame = ttk.Frame(self.root)
        self.dropdown_frame.pack(padx=int(10 * scale), pady=int(10 * scale))
        self.selector = None

        self.token_btn = ttk.Button(container, text="设置 Token", command=self.open_token_window)
        self.token_btn.pack(side="left", padx=int(5 * scale), pady=int(5 * scale), ipady=int(5 * scale))

        self.download_btn = ttk.Button(container, text="下载", command=self.download)
        self.download_btn.pack(side="right", padx=int(5 * scale), pady=int(5 * scale), ipady=int(5 * scale))

        self.copy_btn = ttk.Button(container, text="解析并复制", command=self.parse_and_copy)
        self.copy_btn.pack(side="right", padx=int(5 * scale), pady=int(5 * scale), ipady=int(5 * scale))

        self.progress_bar = ttk.Progressbar(container, length=(125 * scale), mode="determinate")
        self.progress_bar.pack(side="bottom", padx=int(40 * scale), pady=int(10 * scale), ipady=int(5 * scale))

        self.progress_label = ttk.Label(container, text="等待下载", anchor="center")
        self.progress_label.pack(side="bottom", padx=int(5 * scale), pady=int(5 * scale))

    def load_resource_list(self) -> None:
        try:
            self.resource_list = ResourceHelper(self.client).fetch_resource_list()
        except Exception:
            self.resource_list = {}
            logger.warning("获取资源列表失败", exc_info=True)
            # 必须在 tk.Tk() 之后：没有 root 时 messagebox 会隐式建出第二个 root
            messagebox.showwarning("警告", "获取资源列表失败，请手动填写资源链接，或重新打开本程序")

        self.selector = CatalogSelector(self.dropdown_frame, self.root, self.resource_list,
                                        self.insert_url, scale=self.scale)
        self.selector.pack()

    # ---- 界面回调，只在主线程执行 ----

    def insert_url(self, url: str) -> None:
        if self.url_text.get("1.0", tk.END) == "\n": # 输入框为空时，插入的内容前面不加换行
            self.url_text.insert("end", url)
        else:
            self.url_text.insert("end", "\n" + url)

    def update_progress(self, progress: float, text: str) -> None:
        self.progress_bar["value"] = progress
        self.progress_label.config(text=text)

    def finish_downloads(self, dir_path: str, failed_detail: str) -> None:
        self.progress_bar["value"] = 0 # 重置进度条
        self.progress_label.config(text="等待下载") # 清空进度标签
        self.download_btn.config(state="normal") # 设置下载按钮为启用状态

        if failed_detail:
            messagebox.showwarning("下载完成", f"文件已下载到：{dir_path}\n以下链接下载失败：\n{failed_detail}")
        else:
            messagebox.showinfo("下载完成", f"文件已下载到：{dir_path}") # 显示完成对话框

    def open_token_window(self) -> None:
        show_access_token_window(self.root, self.client,
                                 on_saved=lambda: self.download_btn.config(state="normal"))

    # ---- 动作 ----

    def input_urls(self) -> list:
        return [line.strip() for line in self.url_text.get("1.0", tk.END).splitlines() if line.strip()]

    def parse_and_copy(self) -> None: # 解析并复制链接
        resource_links = []
        failed_links = []

        for url in self.input_urls():
            resource_url = parse(self.client, url)[0]
            if not resource_url:
                failed_links.append(url) # 添加到失败链接
                continue
            resource_links.append(resource_url)

        if failed_links:
            messagebox.showwarning("警告", "以下 “行” 无法解析：\n" + "\n".join(failed_links))

        if resource_links:
            pyperclip.copy("\n".join(resource_links)) # 将链接复制到剪贴板
            messagebox.showinfo("提示", "资源链接已复制到剪贴板")

    def download(self) -> None: # 下载资源文件
        self.download_btn.config(state="disabled") # 设置下载按钮为禁用状态

        # 有线程在飞时清空状态会丢掉它们的进度与完成判定，进度与完成提示随之全乱
        if not self.downloads.reset():
            messagebox.showinfo("提示", "仍有下载任务未完成，请等待其结束。")
            return

        urls = self.input_urls()
        failed_links = []
        submitted = 0 # 已投递的下载线程数；只要不为 0，解禁按钮的权力就归完成回调

        if len(urls) > 1:
            messagebox.showinfo("提示", "您选择了多个链接，将在选定的文件夹中使用教材名称作为文件名进行下载。")
            dir_path = filedialog.askdirectory() # 选择文件夹
            if os_name == "Windows":
                dir_path = dir_path.replace("/", "\\")
            if not dir_path:
                self.download_btn.config(state="normal")
                return
        else:
            dir_path = None

        for url in urls:
            resource_url, content_id, title = parse(self.client, url)
            if not resource_url:
                failed_links.append(url) # 添加到失败链接
                continue

            if dir_path:
                save_path = build_save_path(dir_path, title)
            else:
                save_path = filedialog.asksaveasfilename(
                    defaultextension=".pdf", filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
                    initialfile=os.path.basename(build_save_path(os.getcwd(), title))) # 选择保存路径
                if not save_path: # 用户取消了文件保存操作
                    if submitted == 0:
                        self.download_btn.config(state="normal")
                    return
                if os_name == "Windows":
                    save_path = save_path.replace("/", "\\")

            self.downloads.submit(resource_url, save_path)
            submitted += 1

        if failed_links:
            messagebox.showwarning("警告", "以下 “行” 无法解析：\n" + "\n".join(failed_links))

        if submitted == 0: # 没有任何线程在飞，完成回调不会到来，只能在这里解禁
            self.download_btn.config(state="normal")

    def on_closing(self) -> None: # 处理窗口关闭事件
        if not self.downloads.all_finished(): # 当正在下载时，询问用户
            if not messagebox.askokcancel("提示", "下载任务未完成，是否退出？"):
                return

        # 下载线程均为守护线程，不会阻止解释器退出
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    setup_logging()
    try:
        App().run()
    except Exception:
        logger.exception("程序异常退出")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
