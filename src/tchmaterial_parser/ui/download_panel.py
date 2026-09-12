# -*- coding: utf-8 -*-
# 下载面板：解析并复制直链、下载资源文件与进度反馈
# 本模块持有与下载相关的几个控件句柄，因此这些控件的读写不必跨模块

import os, re, threading, time, traceback
import tkinter as tk
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk, messagebox, filedialog
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree

from requests import RequestException

from .runtime import thread_it, ui_call
from .. import config
from ..api import ResourceInfo, parse
from ..bookmarks import add_bookmarks
from ..network import REQUEST_TIMEOUT, request_headers, session
from ..platform_utils import print_error

download_states: list[dict] = [] # 初始化下载状态
_STOP_POLL_INTERVAL = 0.05 # 轮询停止请求的时间片；最长只用来切一次 3 秒的退避等待，代价可以忽略

class BatchStopped(Exception):
    """请求发起阶段命中取消/暂停：这一轮不再继续换镜像或退避重试。

    取消/暂停不是下载失败，调用方应按 stop_reason() 分类收尾（取消→清理，暂停→留给“继续”），
    不要写 failed_reason；用专用异常而不是返回空响应，正是为了不让它落进“响应不可信”那条失败路径。
    """

class BatchControl:
    """一个批次（从点下“下载”到批次终结的整段生命周期）的取消/暂停控制状态。

    “已请求暂停”（pause_event）与“批次已经停稳为暂停态”（paused_settled）是两个不同的时刻，
    中间有一段窗口批次线程仍然存活；paused_settled 只由 handle_batch_outcome 在主线程判定
    结局为“暂停”时置位，也只由 cancel_current_batch 在主线程读取，不允许工作线程读写。
    """

    def __init__(self) -> None:
        self.cancel_event = threading.Event() # 已请求取消
        self.pause_event = threading.Event() # 已请求暂停
        self.paused_settled = False # 批次真正停稳为“暂停”后才置位；只在主线程读写
        self.directory: str | None = None # 解析阶段尚未选定目录；选定后再写入
        self.lock = threading.Lock() # 只保护 active_responses
        self.active_responses: dict[int, object] = {} # id(state) -> Response，用于主动断连

    def stop_requested(self) -> bool:
        """是否已经请求取消或暂停。两个 Event 始终是唯一事实来源，不另外维护派生出来的标志位。"""
        return self.cancel_event.is_set() or self.pause_event.is_set()

    def wait_or_stop(self, timeout: float) -> bool:
        """等待至多 timeout 秒，期间一旦请求取消/暂停就立刻醒来并返回 True；等满则返回 False。

        用小时间片轮询现有的两个 Event，而不是新增一个“取消或暂停”的合并 Event——后者必须在
        每一个置位取消/暂停的地方同步维护，多出一条只能靠人工守住的不变量。
        """
        deadline = time.monotonic() + timeout
        while not self.stop_requested():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, _STOP_POLL_INTERVAL))
        return True

_batch_control: BatchControl | None = None # 当前批次；空闲时为 None
PRIVATE_DOWNLOAD_HOSTS = tuple(f"r{index}-ndr-private.ykt.cbern.com.cn" for index in range(1, 4))
# 私有 CDN 在短时间连打时会回 400（有时带 InvalidArgument，有时几乎空包）。
# 立刻换 r2/r3 只会把限流打得更死；同地址稍等再签一次即可。
_400_RETRY_DELAYS = (1.0, 3.0)
_MIN_REQUEST_INTERVAL = 0.2
# 批量下载时限制同时占用私有 CDN 的任务数，避免 GUI 一开十几个线程又打出 400。
_download_slots = threading.BoundedSemaphore(3)
_rate_lock = threading.Lock()
_last_request_at = 0.0

def redact_access_token(text: str) -> str:
    """隐藏查询串里可能残留的 accessToken。本工具不再主动拼接该参数，但异常或用户粘贴的 URL 仍可能带上。"""
    return re.sub(r"([?&]accessToken=)[^&\s'\"]+", r"\1<已隐藏>", text, flags=re.IGNORECASE)

def download_mirror_urls(url: str) -> list[str]:
    """按原地址优先的顺序生成私有 CDN 镜像，普通下载地址保持不变。"""
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    if hostname not in PRIVATE_DOWNLOAD_HOSTS:
        return [url]

    ordered_hosts = [hostname, *(host for host in PRIVATE_DOWNLOAD_HOSTS if host != hostname)]
    return [urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)) for host in ordered_hosts]

def _pace_request() -> None:
    """避免批量任务在同一瞬间打出一串私有 CDN 请求。"""
    global _last_request_at
    interval = _MIN_REQUEST_INTERVAL
    if interval <= 0:
        return
    with _rate_lock:
        wait = interval - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()

def request_download(url: str, range_from: int | None = None, validator: str | None = None, *, control: BatchControl | None = None):
    """请求资源并在镜像出错时自动切换，返回最终响应和已尝试的无凭据地址。

    鉴权只放在 request_headers 生成的 X-ND-AUTH 里，URL 保持原样。
    官网谁拼 ?accessToken=：不是 UC SDK，是阅读器。普通教材用站点 pdf.js，不拼；
    专题课用 x-edu-microapp-detail 的 docplayer，会拼，但头里仍有按完整 URL
    现算的 MAC。我们的抉择是永远不拼，避免 2efcd89 那种无效 Token 进查询串
    导致的 400 InvalidArgument（#81）。有真实 MAC 时，#76 和专题课都不需要它。

    400 也按鉴权/限流处理：同地址用新 nonce 退避重试，不要立刻改打 r2/r3。

    所有下载请求统一带 Accept-Encoding: identity——压缩传输下字节偏移没有意义，
    首次下载与续传若协商出不同的编码，两次响应的字节内容、长度、校验子都可能对不上。
    传入 range_from 时附加 Range 续传；再带上 validator（ETag 或 Last-Modified）时
    一并附加 If-Range，远端内容已变化时服务端会回整个 200 而不是 206。

    传入 control 时，镜像轮换与 400 退避都受取消/暂停约束：换下一个镜像之前、每次退避重试
    之前各检查一次，退避本身也改成可被唤醒的等待，命中就抛 BatchStopped。这样“点下取消到
    真正停下”的上界就只剩当前这一条在飞的请求，不会再被镜像数与退避次数叠乘。
    不传 control 时逐字保持原有行为（一路重试到底，等待就是普通 sleep）。
    """
    extra_headers = {"Accept-Encoding": "identity"}
    if range_from is not None:
        extra_headers["Range"] = f"bytes={range_from}-"
        if validator:
            extra_headers["If-Range"] = validator

    attempted_urls: list[str] = []
    last_response = None
    last_exception: RequestException | None = None

    def close_and_stop() -> BatchStopped:
        """关掉手上那个已经用不上的响应，并产出调用方要抛的 BatchStopped。"""
        if last_response is not None:
            last_response.close()
        return BatchStopped()

    def wait_before_retry(delay: float) -> bool:
        """退避等待：有批次控制时切片轮询，命中取消/暂停立刻醒来；没有时就是一次普通 sleep。"""
        if control is None:
            time.sleep(delay)
            return False
        return control.wait_or_stop(delay)

    for candidate_url in download_mirror_urls(url):
        if control is not None and control.stop_requested(): # 已经要停了就不再多打一个镜像
            raise close_and_stop()
        attempted_urls.append(candidate_url)
        retry = 0
        while True:
            try:
                _pace_request()
                response = session.get(
                    candidate_url,
                    headers={**request_headers(candidate_url), **extra_headers},
                    stream=True,
                    timeout=REQUEST_TIMEOUT,
                )
            except RequestException as e:
                last_exception = e
                break

            if last_response is not None:
                last_response.close()
            last_response = response

            if response.ok:
                return response, attempted_urls

            # 401/403 换镜像也过不了。400 多半是突发限流，连打镜像会更糟。
            if response.status_code in (401, 403):
                return last_response, attempted_urls
            if response.status_code == 400:
                if retry < len(_400_RETRY_DELAYS):
                    # 这几秒的退避是取消/暂停最容易被卡住的地方，等待必须能被唤醒
                    if wait_before_retry(_400_RETRY_DELAYS[retry]):
                        raise close_and_stop()
                    retry += 1
                    continue
                return last_response, attempted_urls
            break

    if last_response is not None:
        return last_response, attempted_urls
    if last_exception is not None:
        # requests 的异常文字通常包含完整请求 URL，此处重新包装以清除查询参数中的 Token。
        raise RuntimeError(redact_access_token(str(last_exception))) from None
    raise RuntimeError("没有可用的下载地址")

def storage_error_code(response) -> str | None:
    """读取对象存储返回的 XML 错误码；非 XML 响应保持原有通用提示。"""
    try:
        root = ElementTree.fromstring(response.content)
        return root.findtext("Code")
    except (AttributeError, ElementTree.ParseError, TypeError):
        return None

def download_failure_reason(response, attempted_urls: list[str]) -> str:
    status_code = response.status_code
    error_code = storage_error_code(response)
    reason = f"服务器返回 HTTP 状态码 {status_code}"
    if error_code:
        reason += f"（{error_code}）"

    if status_code in (401, 403):
        if config.access_token:
            reason += "，Access Token 可能已过期或无效，请重新设置"
        else:
            reason += "，该资源需要有效的 Access Token，请先设置"
    elif status_code == 400 and error_code == "InvalidArgument":
        # 占位头、过期 Token、或短时间连打私有 CDN 都会回这个码。前面已经同地址重试过。
        if config.access_token:
            reason += "，私有资源暂时无法访问。请稍后重试；若持续失败，请重新设置 Access Token"
        else:
            reason += "，该私有资源需要有效的 Access Token，请先设置"

    if len(attempted_urls) > 1:
        reason += f"，已尝试 {len(attempted_urls)} 个下载镜像"
    return reason

# Windows 禁止在文件名中使用半角 ? * : / 等；改为对应全角字符，尽量保留原标题读法（#86）。
_INVALID_FILENAME_REPLACEMENTS = str.maketrans({
    "<": "＜",
    ">": "＞",
    ":": "：",
    '"': "＂",
    "/": "／",
    "\\": "＼",
    "|": "｜",
    "?": "？",
    "*": "＊",
})
_CONTROL_FILENAME_CHARS = re.compile(r"[\x00-\x1f]")
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
})

def sanitize_filename(filename: str) -> str:
    """将非法文件名字符换成全角对应字符，并避开 Windows 保留设备名。"""
    filename = _CONTROL_FILENAME_CHARS.sub("_", filename.translate(_INVALID_FILENAME_REPLACEMENTS))
    filename = filename.rstrip(" .")
    if not filename:
        return "download"

    stem, extension = os.path.splitext(filename)
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        return f"_{stem}{extension}"
    return filename

def download_filename(resource: ResourceInfo) -> str:
    return sanitize_filename(f"{resource.title or 'download'}.{resource.file_format}")

def filename_key(filename: str) -> str:
    """以跨平台保守方式比较文件名，提前避开 Windows/macOS 上的大小写冲突。"""
    return os.path.normcase(filename).casefold()

def allocate_download_paths(resources: list[ResourceInfo], directory: str) -> list[str]:
    """在线程启动前为批量任务分配唯一目标路径，防止多个线程共用同一个 .tmp 文件。"""
    base_filenames = [download_filename(resource) for resource in resources]
    base_counts = Counter(filename_key(filename) for filename in base_filenames)

    edition_filenames: list[str] = []
    for resource, filename in zip(resources, base_filenames):
        # 例如人教版与北师大版的 “普通高中教科书·英语必修 第三册” 同名时，优先使用易读的版别前缀区分。
        if base_counts[filename_key(filename)] > 1 and resource.edition:
            filename = sanitize_filename(f"[{resource.edition}] {filename}")
        edition_filenames.append(filename)

    reserved_paths: set[str] = set()
    allocated_paths: list[str] = []
    for resource, filename in zip(resources, edition_filenames):
        # 按资源的分类层级（学段/学科/版本）归入子目录，段名中的非法字符换为全角
        subdirectory = os.path.join(directory, *(sanitize_filename(part) for part in resource.relative_dir))
        candidate = os.path.join(subdirectory, filename)
        stem, extension = os.path.splitext(candidate)
        sequence = 2

        # 同时检查最终文件和可辨识的 “最终文件.tmp”；后者可能属于另一个仍在运行的程序实例。
        while (
            filename_key(candidate) in reserved_paths
            or os.path.exists(candidate)
            or os.path.exists(f"{candidate}.tmp")
        ):
            candidate = f"{stem} ({sequence}){extension}"
            sequence += 1

        reserved_paths.add(filename_key(candidate))
        allocated_paths.append(candidate)
    return allocated_paths

def bind_widgets(text: tk.Text, bookmark: tk.BooleanVar, button: ttk.Button, copy_button: ttk.Button, progress_bar: ttk.Progressbar, label: ttk.Label) -> None: # 由 app.py 在创建控件后写入
    global url_text, bookmark_var, download_btn, copy_btn, download_progress_bar, progress_label
    url_text, bookmark_var, download_btn, copy_btn, download_progress_bar, progress_label = text, bookmark, button, copy_button, progress_bar, label

def downloads_active() -> bool: # 是否存在尚未完成的下载任务
    return bool(download_states) and not all(state["finished"] for state in download_states)

def show_parse_progress(current: int, total: int) -> None: # 后台解析大量链接时在进度标签上反馈进度；下载进行中则让位给下载进度
    if downloads_active():
        return
    ui_call(progress_label.config, text=f"正在解析链接 {current}/{total}")

def refresh_download_progress() -> None: # 汇总全部任务状态刷新进度条与标签，没有 Content-Length 或出现失败时也能看到进展
    states = list(download_states)
    all_downloaded_size = sum(state["downloaded_size"] for state in states)
    all_total_size = sum(state["total_size"] for state in states)
    finished_number = len([state for state in states if state["finished"]])
    failed_number = len([state for state in states if state["failed_reason"]])
    total_number = len(states)
    if all_total_size > 0: # 防止下面一行代码除以 0 而报错
        download_progress = (all_downloaded_size / all_total_size) * 100
        ui_call(download_progress_bar.config, value=download_progress) # 更新进度条
        progress_text = f"{format_bytes(all_downloaded_size)}/{format_bytes(all_total_size)} ({download_progress:.2f}%) 已下载 {finished_number}/{total_number}"
    else:
        progress_text = f"已下载 {format_bytes(all_downloaded_size)}，已完成 {finished_number}/{total_number} 个文件"
    if failed_number:
        progress_text += f"，{failed_number} 个失败"
    if _batch_control is not None and _batch_control.paused_settled: # 已暂停：文案要能和正在下载区分开，不能看起来像卡住了
        progress_text = f"已暂停 {progress_text}"
    ui_call(progress_label.config, text=progress_text) # 更新标签以显示当前下载进度

def collect_parsed_resources(
    parse_fn: Callable[[str, bool], list[ResourceInfo] | None],
    urls: list[str],
    bookmarks: bool,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[ResourceInfo], set[str]]:
    """逐条解析链接并汇总结果：按资源直链去重，解析失败的链接单独收集。

    should_stop 在每条 URL 开始解析前检查一次，命中就提前结束，返回目前已收集到的结果；
    不中断正在进行的单次解析请求，只避免开始下一条（对应“取消覆盖解析阶段”的要求）。
    """
    resources_info_list: list[ResourceInfo] = []
    resource_urls: set[str] = set()
    failed_urls: set[str] = set()
    for index, url in enumerate(urls):
        if should_stop and should_stop():
            break
        if on_progress:
            on_progress(index + 1, len(urls))
        resources_info = parse_fn(url, bookmarks)
        if not resources_info:
            failed_urls.add(url)
            continue
        for resource in resources_info:
            if resource.url in resource_urls: # 直接使用 resources_info_list 会报错（list 不可哈希）
                continue
            resources_info_list.append(resource)
            resource_urls.add(resource.url)
    return resources_info_list, failed_urls

def parse_urls_in_background(
    urls: list[str],
    bookmarks: bool,
    on_finished: Callable[[list[ResourceInfo], set[str]], None],
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """在后台线程逐条解析链接，完成后回到主线程执行 on_finished(资源列表, 失败链接集合)。

    批量选择的链接可能多达上百条，逐条解析需多次网络请求，放在主线程会让界面未响应。
    """
    def worker() -> None:
        resources_info_list, failed_urls = collect_parsed_resources(parse, urls, bookmarks, show_parse_progress, should_stop)
        ui_call(on_finished, resources_info_list, failed_urls)

    thread_it(worker)

def parse_and_copy() -> None: # 解析并复制链接
    urls = {line.strip() for line in url_text.get("1.0", "end").splitlines() if line.strip()} # 获取所有非空行并去重
    if not urls:
        return

    copy_btn.config(state="disabled") # 解析期间禁用按钮，避免重复触发

    def copy_urls(resources_info_list: list[ResourceInfo], failed_urls: set[str]) -> None: # 解析完成后在主线程复制链接
        if _batch_control is None: # 没有下载批次在跑才由这里恢复界面；否则听 set_ui_phase/show_parse_progress 的
            copy_btn.config(state="normal")
            progress_label.config(text="等待下载") # 解析进度已无用，恢复默认文案

        resource_urls = {resource.url for resource in resources_info_list}
        if failed_urls:
            messagebox.showwarning("警告", "以下 “行” 无法解析：\n" + "\n".join(failed_urls))

        if resource_urls:
            try:
                resource_urls_str = "\n".join(resource_urls)
                url_text.clipboard_clear()
                url_text.clipboard_append(resource_urls_str) # 将链接复制到剪贴板
                if url_text.clipboard_get() == resource_urls_str: # 检查剪贴板内容是否正确
                    # 真实 X-ND-AUTH 必须按每条 URL 现算，不能把某一次的 nonce/mac 当作通用头复制出去。
                    messagebox.showinfo(
                        "提示",
                        f'资源链接已复制到剪贴板。\n注意：链接可能无法直接下载。官网私有资源使用按地址单独计算的 X-ND-AUTH，请优先用本工具下载。{"若需手动请求，至少带上以下标头（含隐私信息，请勿分享）：" if config.access_token else "未登录时可以尝试："}\n\nAuthorization: Bearer {config.access_token or "0"}\nX-ND-AUTH: MAC id="{config.access_token or "0"}",nonce="0",mac="0"',
                    )
                else:
                    messagebox.showerror("错误", "无法将链接复制到剪贴板，请手动复制。")
            except Exception as e:
                print_error(e)
                messagebox.showerror("错误", "无法将链接复制到剪贴板，请手动复制。")

    parse_urls_in_background(list(urls), False, copy_urls)

def download() -> None: # 下载资源文件
    global download_states, _batch_control
    control = BatchControl() # 覆盖从这里到批次终结的整段生命周期，解析阶段就能取消
    _batch_control = control
    set_ui_phase("parsing") # 解析阶段只有取消有意义，暂停无从谈起；进度条清零也收在这里
    download_states = [] # 初始化下载状态
    urls = {line.strip() for line in url_text.get("1.0", "end").splitlines() if line.strip()} # 获取所有非空行并去重

    if config.access_token and not config.access_token.isascii(): # 判断 Access Token 中是否包含非 ASCII 字符
        messagebox.showwarning("警告", "Access Token 不正确（包含非 ASCII 字符），请点击“设置 Token”按钮重新填写。")
        set_ui_phase("idle")
        _batch_control = None
        return

    if not urls:
        set_ui_phase("idle")
        _batch_control = None
        return

    def start_downloads(resources_info_list: list[ResourceInfo], failed_urls: set[str]) -> None: # 解析完成后在主线程选择保存位置并开始下载
        def restore_idle_ui() -> None: # 放弃这次下载（不是取消）：恢复界面到空闲，控制对象也一并收回
            global _batch_control
            set_ui_phase("idle") # 进度条清零、文案复位为“等待下载”都收在这里
            _batch_control = None

        if control.cancel_event.is_set(): # 解析阶段被取消：不弹任何对话框，直接回到空闲
            restore_idle_ui()
            return

        if len(resources_info_list) > 1:
            messagebox.showinfo("提示", f"您将下载 {len(resources_info_list)} 个文件，请选择要下载文件的位置。本程序将在该文件夹中按教材分类创建子文件夹，并以资源名称命名文件。")
            dir_path = filedialog.askdirectory() # 选择文件夹
            if not dir_path: # 用户取消或关闭对话框
                restore_idle_ui()
                return
            dir_path = os.path.normpath(dir_path)
            # 路径必须在任何线程启动前统一预留，否则同名资源仍可能同时打开同一个 .tmp 文件。
            download_targets = list(zip(resources_info_list, allocate_download_paths(resources_info_list, dir_path)))
        elif resources_info_list:
            download_targets: list[tuple[ResourceInfo, str]] = []
            for resource in resources_info_list:
                save_path = filedialog.asksaveasfilename( # 选择保存路径
                    defaultextension=f".{resource.file_format}",
                    filetypes=[(f"{resource.file_format.upper()} 文件", f"*.{resource.file_format}"), ("所有文件", "*.*")],
                    initialfile=sanitize_filename(resource.title or "download"),
                )
                if not save_path: # 用户取消了文件保存操作
                    restore_idle_ui()
                    return
                save_path = os.path.normpath(save_path)
                download_targets.append((resource, save_path))
        else: # 没有可下载的资源
            restore_idle_ui()
            if failed_urls:
                messagebox.showwarning("警告", "以下 “行” 无法解析：\n" + "\n".join(failed_urls)) # 显示警告对话框
            return

        progress_label.config(text=f"正在下载 {len(download_targets)} 个文件")
        directory = dir_path if len(resources_info_list) > 1 else os.path.dirname(download_targets[0][1])
        start_download_batch(download_targets, directory) # 复用已经存在的 _batch_control，切到“下载中”

        if failed_urls:
            messagebox.showwarning("警告", "以下 “行” 无法解析：\n" + "\n".join(failed_urls)) # 显示警告对话框

    parse_urls_in_background(list(urls), bookmark_var.get(), start_downloads, should_stop=lambda: control.cancel_event.is_set())

def create_download_state(url: str, save_path: str, chapters: list[dict] | None = None) -> dict:
    return {
        "download_url": url, "save_path": save_path,
        "downloaded_size": 0, "total_size": 0,
        "finished": False, "failed_reason": None,
        "validator": None, # 续传校验子：ETag 优先，否则 Last-Modified；都没有则为 None，此时不尝试续传
        "chapters": chapters, # 续传/暂停后“继续”要重新提交任务，跟着状态字典走，不必另外保留 targets
    }

def parse_content_range(header_value: str | None) -> tuple[int, int, int] | None:
    """解析 `Content-Range: bytes 起-止/总长`，返回 (起始, 结束, 总长)；缺失或格式不对时返回 None，
    调用方一律按不可续传处理（不能相信这次响应真的从我们请求的偏移开始）。"""
    if not header_value:
        return None
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", header_value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))

def _response_usability(response, offset: int, can_attempt_range: bool) -> tuple[str | None, tuple[int, int, int] | None]:
    """判定这次响应能不能当正文用，对首次请求、重试请求、有没有带 Range 都一视同仁。

    返回 ("resumed", content_range) 表示可续传的 206（起点对得上）；
    返回 ("full", None) 表示这就是完整正文（严格要求 status_code == 200，
    而不是 response.ok/`< 400`——204/304 这类“ok 但没有正文”的响应不算数，
    否则会产出一个零字节文件却判成功）；
    返回 (None, None) 表示两者都不是，调用方不能信任这次响应的正文/响应头。
    """
    if can_attempt_range and response.status_code == 206:
        content_range = parse_content_range(response.headers.get("Content-Range"))
        if content_range is not None and content_range[0] == offset:
            return "resumed", content_range
    if response.status_code == 200:
        return "full", None
    return None, None

def plan_download_write(current_state: dict, temp_path: str, url: str) -> tuple[str | None, int, int, str | None, object, list[str]]:
    """决定这次写入用什么模式、downloaded_size/total_size 从哪起算、要不要刷新校验子——
    四件事由同一次判断给出，不允许出现互相矛盾的组合（例如判成截断却没有归零计数器）。

    206 只有在“服务端真的从我们请求的偏移开始返回”时才可信；只要它的起点不匹配、或
    Content-Range 解析不出来，这次响应体就只是那一段，不是完整正文，绝不能当整份写下去
    ——必须像 416 一样不信任这次响应，关掉后按一次全新的、不带 Range 的请求重来，且这次
    重来只做一遍：重试后的响应依然不是可续传的 206、也不是完整的 200 正文，就直接判定
    “不可信”交给调用方走失败清理，不再加一层重试。

    这里只返回“计划”出来的 downloaded_size/total_size/validator，不直接写回 current_state：
    调用方必须在真正确定要按 open_mode 打开文件、且已经打开成功之后再应用这些值，
    否则某个提前退出的分支（比如响应刚拿到就被要求暂停）会把 current_state 里的校验子
    改成新版本，磁盘上却还留着旧版本的半截文件，两者从此不同源。open_mode 为 None
    表示这次响应不可信，调用方不应该把它的正文当数据源，应该走失败分支。

    响应头到达之前命中取消/暂停时，request_download 抛出的 BatchStopped 会原样向上传递，
    调用方按停止收尾即可——这条路径上什么都还没写过，不需要、也不应该判成失败。
    """
    offset = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
    can_attempt_range = offset > 0 and bool(current_state["validator"])
    control = current_state.get("control") # 单独下载文件时没有这个键，等价于没有取消/暂停能力

    if can_attempt_range:
        response, attempted_urls = request_download(url, range_from=offset, validator=current_state["validator"], control=control)
    else:
        response, attempted_urls = request_download(url, control=control)

    kind, content_range = _response_usability(response, offset, can_attempt_range)

    # 只有“范围本身有问题”（416，或回了 206 但接不上）才值得不带 Range 重来一次：
    # 换个偏移/校验子可能就通了。真正的失败（404/500 等，与 Range 无关）重来一次
    # 大概率还是失败，直接走下面的失败分支，不做这次多余的尝试。
    range_itself_is_the_problem = can_attempt_range and (response.status_code == 416 or (response.status_code == 206 and kind is None))
    if range_itself_is_the_problem:
        response.close()
        offset = 0
        can_attempt_range = False
        response, attempted_urls = request_download(url, control=control)
        kind, content_range = _response_usability(response, offset, can_attempt_range)

    if kind is None: # 不可信：交给调用方走失败清理，不去碰它未必存在的响应头
        return None, 0, 0, None, response, attempted_urls

    if kind == "resumed":
        return "ab", offset, content_range[2], None, response, attempted_urls

    # kind == "full"：这次响应携带的是完整正文，不论请求时有没有带 Range，都要刷新校验子
    validator = response.headers.get("ETag") or response.headers.get("Last-Modified")
    return "wb", 0, int(response.headers.get("Content-Length", 0)), validator, response, attempted_urls

def set_ui_phase(phase: str) -> None:
    """统一切换底部两个按钮在四个阶段的文案/命令/启用状态，以及进度条/进度文案是否清空——
    是所有离开/进入批次的路径共用的唯一收口点，不要在调用方各自散着写一份。

    只改按钮的 text/command/state，不改 ttk style，因此浅色/深色主题不需要额外适配。
    """
    if phase == "idle":
        copy_btn.config(text="解析并复制", state="normal", command=parse_and_copy)
        download_btn.config(text="下载", state="normal", command=download)
        download_progress_bar.config(value=0)
        progress_label.config(text="等待下载")
    elif phase == "parsing": # 点了“下载”触发的解析：暂停无从谈起，只有取消有意义
        copy_btn.config(text="解析并复制", state="disabled", command=parse_and_copy)
        download_btn.config(text="取消", state="normal", command=cancel_current_batch)
        download_progress_bar.config(value=0) # 重置上一批任务可能残留的进度；文案交给 show_parse_progress
    elif phase == "downloading":
        copy_btn.config(text="暂停", state="normal", command=pause_current_batch)
        download_btn.config(text="取消", state="normal", command=cancel_current_batch)
    elif phase == "paused":
        copy_btn.config(text="继续", state="normal", command=resume_current_batch)
        download_btn.config(text="取消", state="normal", command=cancel_current_batch)
    else:
        raise ValueError(f"未知的界面阶段：{phase}")

def pause_current_batch() -> None: # “暂停”按钮：只是发出请求，批次线程自己去发现并停稳
    control = _batch_control
    if control is None:
        return
    control.pause_event.set()
    close_active_responses(control) # 促使在飞的请求尽快断开，而不是等它们各自撞上读超时

def cancel_current_batch() -> None: # “取消”按钮：解析中/下载中/已暂停三种阶段都可能点到
    global _batch_control
    control = _batch_control
    if control is None:
        return
    control.cancel_event.set()
    close_active_responses(control) # 若批次线程仍存活（不论是不是刚被要求暂停），促使它尽快停下

    if control.paused_settled:
        # 批次线程已经在 handle_batch_outcome 里确认退出，没有人会再来收尾，这里同步收尾
        for state in download_states:
            if not state["finished"]:
                try:
                    os.remove(f"{state['save_path']}.tmp")
                except OSError:
                    pass
                state["downloaded_size"], state["total_size"] = 0, 0
                state["finished"] = True
        set_ui_phase("idle") # 进度条清零、文案复位为“等待下载”都收在这里
        _batch_control = None
    # 否则批次线程仍然存活（不论是解析中、下载中，还是刚发出暂停请求但还没停稳），
    # 交给它自己在 _run_batch_worker 退出后通过 handle_batch_outcome 收尾。

def resume_current_batch() -> None: # “继续”按钮：只在批次真正停稳为暂停态时才有意义
    control = _batch_control
    if control is None or not control.paused_settled:
        return # 批次线程还没确认退出（可能只是刚点了暂停），不能贸然再起一个批次线程
    control.pause_event.clear()
    control.paused_settled = False

    pending = [state for state in download_states if not state["finished"]] # 判定为“暂停”的前提就是仍有未完成任务
    set_ui_phase("downloading")
    progress_label.config(text=f"正在下载 {len(pending)} 个文件")
    thread_it(lambda: _run_batch_worker(pending, control))

def start_download_batch(targets: list[tuple[ResourceInfo, str]], directory: str) -> None:
    global download_states, _batch_control
    if _batch_control is None: # 未经 download() 创建控制对象时（如直接调用本函数），现建一个
        _batch_control = BatchControl()
    control = _batch_control
    control.directory = directory

    # 所有排队任务先登记，快速失败或完成的线程也不会漏算尚未启动的任务。
    states = [create_download_state(resource.url, save_path, resource.chapters) for resource, save_path in targets]
    for state in states:
        state["control"] = control
    download_states = states

    set_ui_phase("downloading")
    thread_it(lambda: _run_batch_worker(states, control))

def _run_batch_worker(states_to_run: list[dict], control: BatchControl) -> None:
    # 批量勾选可能产生数千个文件，仅保留少量工作线程，其余任务排队；
    # “继续”复用同一个函数，只是只提交未完成的子集，终态判定逻辑完全一致。
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(download_file, state["download_url"], state["save_path"], state["chapters"], state)
            for state in states_to_run
        ]
        for future in futures:
            future.result()

    if control.cancel_event.is_set(): # 取消优先于暂停
        outcome = "cancelled"
    elif control.pause_event.is_set() and any(not state["finished"] for state in download_states):
        # 暂停恰好落在最后一个文件的最后一块之后：批次层面已经没有未完成任务了，
        # 不该因为 pause_event 还留着置位就落成“暂停”——否则界面会卡在“已暂停 100%”，
        # 点“继续”要空转一轮才弹完成，点“取消”则全程不会有任何完成提示。
        outcome = "paused"
    else:
        outcome = "completed"
    ui_call(handle_batch_outcome, outcome, control) # 全部线程退出后，仅由批次通知一次

def handle_batch_outcome(outcome: str, control: BatchControl) -> None: # 在主线程判定/收尾一个批次的终态
    global _batch_control

    if control.cancel_event.is_set(): # outcome 算出之后到这次回调真正执行之前，取消随时可能追上来；
        outcome = "cancelled"         # 取消一旦置位，不允许再落成 paused/completed

    if outcome == "paused":
        control.paused_settled = True # 批次线程确认退出后才置位；全模块唯一的赋值点
        set_ui_phase("paused")
        refresh_download_progress()
        return

    states = download_states
    directory = control.directory
    for state in states: # 批次生命周期边界上的最后一道保险；正常情况下 download_file 内部已经各自处理过
        if not state["finished"]:
            try:
                os.remove(f"{state['save_path']}.tmp")
            except OSError:
                pass
            state["downloaded_size"], state["total_size"] = 0, 0
            state["finished"] = True

    set_ui_phase("idle") # 进度条清零、文案复位为“等待下载”都收在这里
    _batch_control = None

    if outcome == "cancelled": # 取消不弹“下载完成”弹窗
        return

    failed_states = [state for state in states if state["failed_reason"]]
    if failed_states:
        failed_message = "\n\n".join(
            f"{os.path.relpath(state['save_path'], directory)}\n{state['failed_reason']}"
            for state in failed_states
        )
        messagebox.showwarning("下载完成", f"文件已下载到：{directory}\n以下文件下载失败：\n{failed_message}")
    else:
        messagebox.showinfo("下载完成", f"文件已下载到：{directory}")

def close_active_responses(control: BatchControl) -> None:
    """主动断连该批次所有登记在案的响应，促使阻塞在读取上的线程尽快停下，而不是等它们撞上读超时。"""
    with control.lock:
        responses = list(control.active_responses.values())
    for response in responses:
        try:
            response.close()
        except Exception:
            pass

def download_file(url: str, save_path: str, chapters: list[dict] | None = None, current_state: dict | None = None) -> None: # 下载文件
    if current_state is None: # 保留单独下载文件的调用方式
        current_state = create_download_state(url, save_path)
        download_states.append(current_state)
    control: BatchControl | None = current_state.get("control") # 单独调用时没有这个键，等价于没有取消/暂停能力
    temp_path = f"{save_path}.tmp"

    def stop_reason() -> str | None: # 取消优先于暂停；按事件标志分类，不按触发它的异常类型分类
        if control is not None and control.cancel_event.is_set():
            return "cancelled"
        if control is not None and control.pause_event.is_set():
            return "paused"
        return None

    response = None
    registered_key = None
    paused = False # 暂停时 finished 保持 False，留给“继续”重新提交；其余情况都会在 finally 里置为 True
    finalizing = False # 传输已确认完整、进入“加书签 + 改名”收尾阶段之后置位：.tmp 从这一刻起
    # 可能不再是服务端正文的前缀（add_bookmarks 会整份重写它），因此这个阶段一旦抛出异常，
    # 期间命中的暂停请求不能再把任务回滚成“可续传”状态，只能判定为失败并清理，逼下一次发起全新下载

    def discard_temp_and_zero_counters() -> None: # 结局既不是“暂停”、也不是“传输已确认完整”的路径都要走这里：
        # 此时 .tmp 只是个半成品（可能是这次建的，也可能是上一轮暂停/续传留下的），一律清掉，计数器一律归零
        current_state["downloaded_size"], current_state["total_size"] = 0, 0
        try:
            os.remove(temp_path)
        except Exception:
            pass

    try:
        with _download_slots:
            reason = stop_reason()
            if reason == "cancelled": # 排队中被取消：不发起网络请求；.tmp 可能是上一轮暂停留下的，一并清理
                discard_temp_and_zero_counters()
                return
            if reason == "paused": # 排队中被暂停：不发起网络请求，留给“继续”重新提交
                paused = True
                return

            open_mode, planned_downloaded_size, planned_total_size, planned_validator, response, attempted_urls = plan_download_write(current_state, temp_path, url)
            if control is not None: # 登记这次响应，供主线程暂停/取消时主动断连
                registered_key = id(current_state)
                with control.lock:
                    control.active_responses[registered_key] = response

            # 请求在飞时也可能已经被暂停/取消：响应是否可用已经不重要，不能把它判成真失败，
            # 也不能在这里就把 current_state 改成这次“计划”出来的值——万一就此提前退出，
            # 磁盘上的 .tmp 还是旧内容，current_state 却已经指向新版本，两者不再同源。
            reason = stop_reason()
            if reason == "cancelled":
                discard_temp_and_zero_counters()
            elif reason == "paused":
                paused = True
            elif open_mode is None: # 响应不可信：不是可续传的 206，也不是完整的 200 正文
                current_state["failed_reason"] = download_failure_reason(response, attempted_urls)
                discard_temp_and_zero_counters()
            else:
                os.makedirs(os.path.dirname(save_path), exist_ok=True) # 分类下载时子目录可能尚不存在
                with open(temp_path, open_mode) as file:
                    # 走到这里，open() 已经按 open_mode 打开成功（"wb" 已经截断），
                    # 磁盘状态与即将写入的 current_state 必然同源，这才应用“计划”里的值。
                    current_state["downloaded_size"] = planned_downloaded_size
                    current_state["total_size"] = planned_total_size
                    if open_mode == "wb": # 全新正文：无条件覆盖校验子，哪怕这次响应没给、该清空成 None
                        current_state["validator"] = planned_validator
                    # open_mode == "ab"（续传）：不动校验子，沿用发起这次请求时用的旧值——
                    # planned_validator 在这条分支上恒为 None，不代表“应当清空”，只是“不归它管”，
                    # 不能用同一个 None 表达两种语义，只能靠 open_mode 来分辨该不该写。
                    for chunk in response.iter_content( # 分块下载；total_size 续传时也是文件全长，分档依据不变
                        chunk_size=131072 if current_state["total_size"] < 20971520 else 262144 if current_state["total_size"] < 52428800 else 524288
                    ):
                        if chunk: # 过滤掉 Keep-Alive 块
                            file.write(chunk)
                            current_state["downloaded_size"] += len(chunk)
                            refresh_download_progress()
                        if stop_reason(): # 每写完一块检查一次，命中就停止读取，不再等下一块
                            break

                # 循环退出后重新读一次：流干净结束（EOF）时不能沿用循环里最后一次的 reason，
                # 否则暂停恰好撞上 EOF 会被当成“下载不完整”，把好不容易保住的半截文件删掉。
                reason = stop_reason()
                # 暂停恰好落在最后一块之后：文件其实已经下完，只是还没来得及被判定成功，
                # 不该当成“暂停”留着 .tmp 不改名——已知总长且确实下满，就按完成处理。
                reached_full_length = current_state["total_size"] > 0 and current_state["downloaded_size"] == current_state["total_size"]
                if reason == "cancelled": # 传输尚未确认完整时被取消：中止写入，删除这份半成品，不算失败
                    discard_temp_and_zero_counters()
                elif reason == "paused" and not reached_full_length: # 在飞中被暂停：保留已写的 .tmp，不清零已下载量
                    paused = True
                elif current_state["total_size"] > 0 and current_state["downloaded_size"] != current_state["total_size"]: # 文件下载不完整
                    current_state["failed_reason"] = f"文件下载不完整，需下载 {current_state['total_size']} 字节，实际下载 {current_state['downloaded_size']} 字节"
                    discard_temp_and_zero_counters()
                else:
                    # 传输已确认完整；从这一刻起 .tmp 随时可能不再是服务端正文的前缀。
                    # 收尾两步都不抛异常时，不论期间命中的是暂停还是取消都按完成交付——
                    # 取消只回收尚未完整的半成品，不回收已经完整的成果。
                    finalizing = True
                    if chapters: # 添加书签：会把 .tmp 整份重写，字节内容、长度都会变
                        ui_call(progress_label.config, text="添加书签")
                        add_bookmarks(temp_path, chapters)

                    os.replace(temp_path, save_path) # 重命名临时文件为目标文件

    except Exception as e:
        # 主动断连会让 iter_content/文件写入抛出异常，具体异常类型不保证一致，按事件标志分类更稳定；
        # 响应头到达之前命中取消/暂停时 request_download 抛出的 BatchStopped 也落在这里，同样按标志分类
        reason = stop_reason()
        if reason == "cancelled":
            discard_temp_and_zero_counters()
        elif reason == "paused" and not finalizing:
            paused = True
        else:
            # finalizing 阶段命中暂停也落到这里：加书签/改名失败时 .tmp 可能已经被整份重写，
            # 不再是服务端正文的前缀，没有“继续”这回事，只能判定失败并清理，逼下一次全新下载
            print_error(e)
            current_state["failed_reason"] = redact_access_token(traceback.format_exc().rstrip())
            discard_temp_and_zero_counters()
    finally:
        if registered_key is not None: # 登记过就一定要注销，避免别的任务的主动断连误关到这次已经用不上的响应
            with control.lock:
                control.active_responses.pop(registered_key, None)
        if response is not None:
            response.close()
        if not paused:
            current_state["finished"] = True

    refresh_download_progress() # 每个任务结束时刷新一次，重试等待期间也能看到完成数与失败数

def format_bytes(size: float) -> str: # 将数据单位进行格式化，返回以 KB、MB、GB、TB、PB 为单位的数据大小
    for x in ["字节", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:3.1f} {x}"
        size /= 1024.0
    return f"{size:3.1f} PB"
