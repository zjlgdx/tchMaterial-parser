# -*- coding: utf-8 -*-
"""下载调度与状态。

工作线程只在锁内更新纯数据，界面变化一律经回调交回调用方——本模块因此
不需要知道 Tkinter 的存在。
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..config import AppConfig
from . import naming

logger = logging.getLogger(__name__)

RETRY_BACKOFF = (1.0, 2.0, 4.0) # 退避秒数，第 n 次重试取第 n 项
# 校验子的优先级：判断续传拿到的是不是同一份文件
VALIDATOR_HEADERS = ("ETag", "Last-Modified", "Content-Length")


def format_bytes(size: float) -> str: # 将数据单位进行格式化，返回以 KB、MB、GB、TB 为单位的数据大小
    for x in ["字节", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:3.1f} {x}"
        size /= 1024.0
    return f"{size:3.1f} PB"


def remove_part_file(part_path: str) -> None: # 清理下载残件
    try:
        os.remove(part_path)
    except FileNotFoundError: # 失败发生在建出文件之前，没有残件可清
        pass


def response_validator(response):
    """取出用于 If-Range 的校验子；三个都拿不到就返回 None（降级为不续传）。"""
    for header in VALIDATOR_HEADERS:
        value = response.headers.get(header)
        if value:
            return header, value
    return None


class DownloadCancelled(Exception):
    """关窗时置位取消标志，正在下载的任务据此提前退出。"""


class DownloadManager:
    def __init__(self, client, config: AppConfig = None, on_progress=None, on_finish=None):
        self.client = client
        self.config = config or AppConfig()
        # 两个回调都在工作线程里被调用，调用方负责把它们转投到自己的主线程
        self.on_progress = on_progress or (lambda progress, text: None)
        self.on_finish = on_finish or (lambda dir_path, failed_detail: None)
        self._lock = threading.Lock()
        self._states = []
        self._completion_notified = False
        self._cancelled = threading.Event()
        self._executor = None
        self._live = 0      # 当前真正在执行的任务数
        self._peak_live = 0 # 观察到的峰值，用于验证并发上限

    # ---- 状态 ----

    def states(self) -> list:
        with self._lock:
            return [dict(state) for state in self._states]

    def in_flight(self) -> int:
        with self._lock:
            return len([state for state in self._states if not state["finished"]])

    def all_finished(self) -> bool:
        with self._lock:
            return all(state["finished"] for state in self._states)

    def peak_concurrency(self) -> int:
        with self._lock:
            return self._peak_live

    def reset(self) -> bool:
        """清空下载状态；仍有任务在飞时不动它并返回 False。"""
        with self._lock:
            if any(not state["finished"] for state in self._states):
                return False
            self._states.clear() # 就地清空而非重新绑定，工作线程持有的是同一个列表对象
            self._completion_notified = False
            self._peak_live = 0
            self._cancelled.clear()
            return True

    # ---- 调度 ----

    def _ensure_executor(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.config.max_download_workers, thread_name_prefix="download")
        return self._executor

    def submit(self, url: str, save_path: str):
        return self._ensure_executor().submit(self.download_file, url, save_path)

    def cancel_all(self) -> None:
        """关窗时调用。

        已在执行的任务撤不掉，正阻塞在读取上的线程要等到读取超时才会看到标志，
        退出延迟的上界因此是读取超时而不是一个分块的时间。
        """
        self._cancelled.set()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    # ---- 下载 ----

    def _open_stream(self, url: str, resume_from: int, validator):
        """发起请求；resume_from > 0 时尝试续传，返回 (响应, 实际起点)。"""
        headers = None
        if resume_from > 0 and validator is not None:
            headers = { "Range": f"bytes={resume_from}-", "If-Range": validator[1] }

        response = self.client.stream(url, headers=headers)

        if headers is None:
            return response, 0

        if response.status_code != 206:
            # 服务端不接受续传（或文件已变），从零重来
            return response, 0

        current = response_validator(response)
        if current is not None and current[0] == validator[0] and current[1] != validator[1]:
            # 拼出「旧文件前缀 + 新文件后缀」比半截文件更难发现，宁可重下
            logger.info("续传校验子不一致，放弃续传并重新下载：%s", url)
            response.close()
            return self.client.stream(url), 0

        return response, resume_from

    def _download_once(self, url: str, part_path: str, current_state: dict,
                       resume_from: int, ctx: dict):
        response, start = self._open_stream(url, resume_from, ctx.get("validator"))

        # 校验子要在拿到响应头的当下就记住：中途断流时这一轮不会走到结尾，
        # 而那恰恰是下一轮需要拿它去续传的场合
        seen = response_validator(response)
        if seen is not None and (start == 0 or ctx.get("validator") is None):
            ctx["validator"] = seen

        if response.status_code == 401 or response.status_code == 403:
            raise PermissionError("授权失败，Access Token 可能已过期或无效，请重新设置")
        if response.status_code >= 400:
            raise ConnectionError(f"服务器返回状态码 {response.status_code}")

        if start == 0:
            remove_part_file(part_path) # 从零重来，旧残件先清掉
            total = int(response.headers.get("Content-Length", 0))
        else:
            total = start + int(response.headers.get("Content-Length", 0))

        with self._lock:
            current_state["total_size"] = total
            current_state["downloaded_size"] = start

        with open(part_path, "ab" if start else "wb") as file:
            for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                if self._cancelled.is_set():
                    raise DownloadCancelled()
                file.write(chunk)
                with self._lock: # 汇总值必须在同一临界区内一次取齐，否则会读到别的线程写到一半的状态
                    current_state["downloaded_size"] += len(chunk)
                    all_downloaded_size = sum(state["downloaded_size"] for state in self._states)
                    all_total_size = sum(state["total_size"] for state in self._states)
                    downloaded_number = len([state for state in self._states if state["finished"]])
                    total_number = len(self._states)

                if all_total_size > 0: # 防止下面一行代码除以 0 而报错
                    progress = (all_downloaded_size / all_total_size) * 100
                    text = (f"{format_bytes(all_downloaded_size)}/{format_bytes(all_total_size)}"
                            f" ({progress:.2f}%) 已下载 {downloaded_number}/{total_number}")
                    self.on_progress(progress, text)

    def download_file(self, url: str, save_path: str) -> None: # 在工作线程中执行
        current_state = { "download_url": url, "save_path": save_path, "downloaded_size": 0,
                          "total_size": 0, "finished": False, "failed_reason": None, "attempts": 0 }
        with self._lock:
            self._states.append(current_state)
            self._live += 1
            self._peak_live = max(self._peak_live, self._live)

        part_path = save_path + ".part" # 先写临时文件，写完整了才改名，失败时不会留下能被当成课本打开的半截 PDF
        ctx = {} # 跨重试保留的上下文，目前只有续传校验子
        failed_reason = None

        try:
            for attempt in range(self.config.max_retries + 1):
                with self._lock:
                    current_state["attempts"] = attempt + 1

                resume_from = os.path.getsize(part_path) if (attempt and os.path.exists(part_path)) else 0
                try:
                    self._download_once(url, part_path, current_state, resume_from, ctx)
                    os.replace(part_path, save_path) # 只有完整写完才会出现目标文件
                    failed_reason = None
                    break
                except (DownloadCancelled, PermissionError) as e:
                    failed_reason = str(e) or "下载已取消"
                    break # 取消与授权失败都不该重试
                except Exception as e:
                    failed_reason = str(e)
                    if attempt >= self.config.max_retries:
                        break
                    logger.info("下载失败将重试（第 %d 次）：%s（%s）", attempt + 1, url, e)
                    time.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
        finally:
            if failed_reason is not None:
                remove_part_file(part_path)
                logger.warning("下载失败：%s（%s）", url, failed_reason)

            naming.release_path(save_path) # 归还预留的文件名，失败重下时还能拿回原名

            with self._lock:
                self._live -= 1
                current_state["finished"] = True
                current_state["failed_reason"] = failed_reason
                if failed_reason is not None:
                    current_state["downloaded_size"], current_state["total_size"] = 0, 0
                else:
                    current_state["downloaded_size"] = current_state["total_size"]

        # 完成判定与“是否已通知”的置位必须在同一临界区内完成：
        # 否则最后两个线程可能同时看到“全部完成”，把完成对话框弹两次
        with self._lock:
            should_notify = all(state["finished"] for state in self._states) and not self._completion_notified
            if should_notify:
                self._completion_notified = True
                failed_states = [state for state in self._states if state["failed_reason"]]
                failed_detail = "\n".join(f"{state['download_url']}，原因：{state['failed_reason']}"
                                          for state in failed_states)

        if should_notify:
            self.on_finish(os.path.dirname(save_path), failed_detail)


def build_save_path(dir_path: str, title: str) -> str:
    """在目标目录里为一本教材分配安全且互不冲突的路径。"""
    save_path = naming.unique_path(dir_path, naming.sanitize_filename(title), ".pdf")
    naming.assert_within(dir_path, save_path)
    return save_path
