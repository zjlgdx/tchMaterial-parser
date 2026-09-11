# -*- coding: utf-8 -*-
"""主窗口与程序生命周期。"""

import logging
import queue
import sys
import threading
import tkinter as tk
from functools import partial
from tkinter import ttk, messagebox, filedialog

import pyperclip

from .. import __version__
from ..config import AppConfig, os_name
from ..core import tokens
from ..core.startup import load_catalog
from ..core.downloader import DownloadManager, build_save_path
from ..core.naming import sanitize_filename
from ..core.catalog import CatalogCancelled, ResourceHelper
from ..core.errors import ParserError
from ..core.http import HttpClient
from ..core.parser import parse
from ..logging_setup import setup_logging
from .catalog_tree import PLACEHOLDER_TEXT, CatalogTree
from .platform_ui import apply_dpi_scaling, set_window_icon, ui_font
from .token_dialog import attach_context_menu, show_access_token_window

logger = logging.getLogger(__name__)

DESCRIPTION = """\
📌 请在下面的文本框中输入一个或多个资源页面的网址（每个网址一行）。
🔗 资源页面网址示例：
    https://basic.smartedu.cn/tchMaterial/detail?contentType=assets_document&contentId=...
📝 您也可以在下方的教材目录里逐层展开，双击教材即可加入上方；也可以直接搜索教材名。
📥 点击 “下载” 按钮后，程序会解析并下载资源。
⚠️ 注：为了更可靠地下载，建议点击 “设置 Token” 按钮，参照里面的说明完成设置。"""



def format_failures(failed_links: list) -> str:
    """每一行都带上它自己的失败原因，而不是一句笼统的“无法解析”。"""
    return "\n".join(f"{url}\n    原因：{reason}" for url, reason in failed_links)


class App:
    def __init__(self, config: AppConfig = None):
        self.config = config or AppConfig()
        self.client = HttpClient(config=self.config, access_token=tokens.load_token())

        self.root = tk.Tk()
        self.scale = apply_dpi_scaling(self.root)
        self.root.title(f"国家中小学智慧教育平台 资源下载工具 v{__version__}")
        set_window_icon(self.root)

        # 工作线程只改自己那条状态；界面每个 tick 读一次聚合快照——Tkinter 非线程安全
        self.ui_queue = queue.Queue()
        self.downloads = DownloadManager(self.client, config=self.config)
        self.download_session = False # 本批下载是否还在进行；完成判定只由轮询器做

        self.resource_list = {}
        self.catalog_is_stale = False
        self.catalog_helper = ResourceHelper(self.client, config=self.config)
        self.build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing) # 注册窗口关闭事件的处理函数
        self.root.after(self.config.progress_poll_ms, self.drain_ui_queue) # 由主线程排期，合法
        self.start_catalog_load()

    # ---- 构建界面 ----

    def build_widgets(self) -> None:
        scale = self.scale
        pad = int(10 * scale)

        # 整窗一个垂直栈：标题 / 说明 / 链接输入 / 目录 / 操作区。
        # 每一段各占一行，宽度由 grid 的列权重统一拉伸，控件之间不会互相挤。
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        container = ttk.Frame(self.root, padding=pad)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(3, weight=1) # 只有目录区跟着窗口一起长
        self.container = container

        title_label = ttk.Label(container, text="国家中小学智慧教育平台 资源下载工具",
                                font=ui_font(16, bold=True, root=self.root))
        title_label.grid(row=0, column=0, sticky="w", pady=(0, int(4 * scale)))

        description_label = ttk.Label(container, text=DESCRIPTION, justify="left",
                                      font=ui_font(9, root=self.root))
        description_label.grid(row=1, column=0, sticky="w", pady=(0, pad))

        url_group = ttk.LabelFrame(container, text="资源链接", padding=int(8 * scale))
        url_group.grid(row=2, column=0, sticky="ew", pady=(0, pad))
        url_group.columnconfigure(0, weight=1)
        # 宽高是字符数而非像素，不跟随缩放
        self.url_text = tk.Text(url_group, width=72, height=8, font=ui_font(9, root=self.root))
        self.url_text.grid(row=0, column=0, sticky="ew")
        url_scroll = ttk.Scrollbar(url_group, orient="vertical", command=self.url_text.yview)
        self.url_text.configure(yscrollcommand=url_scroll.set)
        url_scroll.grid(row=0, column=1, sticky="ns")
        attach_context_menu(self.url_text, self.root, [
            ("剪切 (Ctrl＋X)", "<<Cut>>"),
            ("复制 (Ctrl＋C)", "<<Copy>>"),
            ("粘贴 (Ctrl＋V)", "<<Paste>>"),
        ])

        catalog_group = ttk.LabelFrame(container, text="选择教材（双击加入上方链接）",
                                       padding=int(8 * scale))
        catalog_group.grid(row=3, column=0, sticky="nsew", pady=(0, pad))
        catalog_group.columnconfigure(0, weight=1)
        catalog_group.rowconfigure(0, weight=1)
        self.selector = CatalogTree(catalog_group, self.insert_url, scale=scale)
        self.selector.grid(row=0, column=0, sticky="nsew")

        # 操作区：进度独占一行拿到整宽，按钮在下面一行左右分组，互不挤压
        action = ttk.Frame(container)
        action.grid(row=4, column=0, sticky="ew")
        action.columnconfigure(0, weight=1)

        progress_row = ttk.Frame(action)
        progress_row.grid(row=0, column=0, sticky="ew", pady=(0, int(6 * scale)))
        progress_row.columnconfigure(0, weight=1)
        # 进度文本独占一行：把它和进度条并排会挤掉进度条的宽度，
        # 而文本长度随下载数量变化，挤压幅度还不固定
        self.progress_label = ttk.Label(progress_row, text="等待下载", anchor="w")
        self.progress_label.grid(row=0, column=0, sticky="w", pady=(0, int(3 * scale)))
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate")
        self.progress_bar.grid(row=1, column=0, sticky="ew")

        button_row = ttk.Frame(action)
        button_row.grid(row=1, column=0, sticky="ew")
        button_row.columnconfigure(1, weight=1) # 中间留白把两组按钮推到两端

        self.token_btn = ttk.Button(button_row, text="设置 Token", command=self.open_token_window)
        self.token_btn.grid(row=0, column=0, sticky="w")

        self.copy_btn = ttk.Button(button_row, text="解析并复制", command=self.parse_and_copy)
        self.copy_btn.grid(row=0, column=2, sticky="e", padx=(0, int(6 * scale)))

        self.download_btn = ttk.Button(button_row, text="下载", command=self.download)
        self.download_btn.grid(row=0, column=3, sticky="e")

        self.root.update_idletasks()
        self.root.minsize(self.root.winfo_reqwidth(), self.root.winfo_reqheight()) # 不让用户把窗口缩到内容被裁切

    def start_catalog_load(self) -> None:
        """目录加载放后台：它可能要拉四十余 MB，放在主线程上就是「双击图标后毫无反应」。"""
        self.selector.show_placeholder(PLACEHOLDER_TEXT)
        thread = threading.Thread(target=self._load_catalog_worker, name="catalog-load", daemon=True)
        thread.start()
        self.catalog_thread = thread

    def post_to_ui(self, callback) -> None:
        """把一个回调排进队列，由主线程的轮询器执行。

        不直接用 root.after：它只能由主线程、或在主线程已进入 mainloop 之后
        调用。命中缓存时目录加载可能比 mainloop 起得还早，那一次投递会直接失败，
        界面就永远停在加载占位上了。
        """
        self.ui_queue.put(callback)

    def drain_ui_queue(self) -> None: # 只在主线程执行
        # 下一次 tick 先排上，再做这个 tick 该做的事：后面任何一步漏出异常，
        # 重排语句都已经执行过了。否则轮询器会就此死掉——目录结果、进度、
        # 完成提示全部停摆，而窗口看上去一切正常
        try:
            self.root.after(self.config.progress_poll_ms, self.drain_ui_queue)
        except tk.TclError:
            return # 窗口已销毁，轮询到此为止

        while True:
            try:
                callback = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                logger.exception("界面更新回调执行失败")

        try:
            self.poll_downloads()
        except Exception:
            logger.exception("刷新下载进度失败")

    def poll_downloads(self) -> None: # 只在主线程执行
        """每个 tick 读一次快照，顺便判定这批下载是否已经结束。

        完成判定必须在这里做：只有主线程知道这一批一共提交了几个任务，
        工作线程看到的永远只是「此刻已登记的那几条」。
        """
        if not self.download_session:
            return

        snapshot = self.downloads.snapshot()
        self.progress_bar["value"] = snapshot.percent
        self.progress_label.config(text=snapshot.progress_text())

        if snapshot.all_finished:
            self.download_session = False
            self.finish_downloads(snapshot)

    def _load_catalog_worker(self) -> None: # 在后台线程中执行
        try:
            result = load_catalog(self.client, helper=self.catalog_helper,
                                  progress_cb=self._report_catalog_progress)
        except CatalogCancelled:
            return # 关窗了，界面已经不在了
        except Exception as e:
            logger.exception("资源目录加载线程异常退出")
            result = ({}, False, str(e))

        self.post_to_ui(partial(self.apply_catalog, *result)) # 结果交回主线程

    def _report_catalog_progress(self, done: int, total: int) -> None: # 在后台线程中执行
        self.post_to_ui(partial(self.selector.show_placeholder,
                                f"{PLACEHOLDER_TEXT}（{done}/{total}）"))

    def apply_catalog(self, resource_list, is_stale: bool, failure) -> None: # 只在主线程执行
        self.resource_list, self.catalog_is_stale = resource_list, is_stale

        if failure is not None:
            logger.warning("获取资源列表失败：%s", failure)
            self.selector.show_placeholder(f"资源目录加载失败：{failure}")
            # 必须在 tk.Tk() 之后：没有 root 时 messagebox 会隐式建出第二个 root
            messagebox.showwarning("警告", f"获取资源列表失败：{failure}\n请手动填写资源链接，或重新打开本程序")
            return

        note = "（离线缓存，内容可能不是最新的）" if is_stale else None
        self.selector.set_catalog(self.resource_list, note=note)

    # ---- 界面回调，只在主线程执行 ----

    def insert_url(self, url: str) -> None:
        if self.url_text.get("1.0", tk.END) == "\n": # 输入框为空时，插入的内容前面不加换行
            self.url_text.insert("end", url)
        else:
            self.url_text.insert("end", "\n" + url)

    def finish_downloads(self, snapshot) -> None: # 只在主线程执行
        self.progress_bar["value"] = 0 # 重置进度条
        self.progress_label.config(text="等待下载") # 清空进度标签
        self.download_btn.config(state="normal") # 设置下载按钮为启用状态

        detail = snapshot.failure_detail()
        if detail:
            messagebox.showwarning("下载完成",
                                   f"文件已下载到：{snapshot.last_dir}\n以下链接下载失败：\n{detail}")
        else:
            messagebox.showinfo("下载完成", f"文件已下载到：{snapshot.last_dir}") # 显示完成对话框

    def open_token_window(self) -> None:
        # 不在这里解禁下载按钮：按钮的恢复只能由「没有任务在飞」派生，
        # 否则下载进行中保存一次 Token 就能把它解禁
        show_access_token_window(self.root, self.client)

    # ---- 动作 ----

    def input_urls(self) -> list:
        return [line.strip() for line in self.url_text.get("1.0", tk.END).splitlines() if line.strip()]

    def parse_and_copy(self) -> None: # 解析并复制链接
        resource_links = []
        failed_links = []

        for url in self.input_urls():
            try:
                resource_links.append(parse(self.client, url)[0])
            except ParserError as e:
                logger.info("解析失败：%s（%s）", url, e.message)
                failed_links.append((url, e.message)) # 连同原因一起记下

        if failed_links:
            messagebox.showwarning("警告", "以下 “行” 无法解析：\n" + format_failures(failed_links))

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

        # 整批提交完了才开闸让轮询器判定。放在循环里开的话，只要循环中途跑过
        # 一次嵌套事件循环（模态对话框就会），轮询器就可能看到「才登记了两个、
        # 而这两个恰好都跑完了」的半截快照，把它当成整批结束。
        # 而 finally 是必须的：循环里任何一个意外异常都会跳过开闸，于是轮询器
        # 不刷进度也不弹完成框，按钮永远停在 disabled 上——用户只能重启程序
        try:
            for url in urls:
                # 兜底包的是循环体而不是整个循环：包整个循环的话，第 3 条链接上的
                # 意外异常会让第 4 条之后根本不被投递，而同一循环里的解析失败是
                # continue 的——「一条坏链接拖垮整批」会以另一种形式留下来
                try:
                    resource_url, _content_id, title = parse(self.client, url)

                    if dir_path:
                        save_path = build_save_path(dir_path, title)
                    else:
                        # 建议名只做清洗：拿 build_save_path 取名字会在当前工作目录登记一条
                        # 预留，而用户取消或改名之后这条预留永远回不来，下次下载同一本书
                        # 建议名就变成了 xxx (2).pdf
                        save_path = filedialog.asksaveasfilename(
                            defaultextension=".pdf",
                            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
                            initialfile=sanitize_filename(title)) # 选择保存路径
                        if not save_path: # 用户取消了文件保存操作
                            return
                        if os_name == "Windows":
                            save_path = save_path.replace("/", "\\")

                    self.downloads.submit(resource_url, save_path)
                    submitted += 1
                except ParserError as e:
                    logger.info("解析失败：%s（%s）", url, e.message)
                    failed_links.append((url, e.message)) # 添加到失败链接
                except Exception as e:
                    # 走到这里说明是没预料到的失败。它同样只属于这一条链接，
                    # 并入同一张失败清单，用户看到的是具体原因而不是「请查看日志」
                    logger.exception("投递下载任务时出错：%s", url)
                    failed_links.append((url, f"准备下载任务时出错：{e}"))
        finally:
            self.download_session = submitted > 0
            if submitted == 0: # 没有任何线程在飞，完成回调不会到来，只能在这里解禁
                self.download_btn.config(state="normal")

        if failed_links:
            messagebox.showwarning("警告", "以下 “行” 无法解析：\n" + format_failures(failed_links))

    def on_closing(self) -> None: # 处理窗口关闭事件
        if not self.downloads.all_finished(): # 当正在下载时，询问用户
            if not messagebox.askokcancel("提示", "下载任务未完成，是否退出？"):
                return

        # 线程池的工作线程不是守护线程，解释器退出时会等它们；必须显式置位取消标志。
        # 正阻塞在网络读取上的线程要等到读取超时才看得到标志，退出延迟的上界即读取超时
        self.downloads.cancel_all()
        self.catalog_helper.cancel() # 目录加载线程也要能退
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
