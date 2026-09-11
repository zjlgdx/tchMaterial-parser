# -*- coding: utf-8 -*-
"""下载调度与状态。

工作线程只在锁内更新纯数据，界面变化一律经回调交回调用方——本模块因此
不需要知道 Tkinter 的存在。
"""

import logging
import os
import threading

from ..config import AppConfig
from . import naming

logger = logging.getLogger(__name__)


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

    def states(self) -> list:
        with self._lock:
            return [dict(state) for state in self._states]

    def in_flight(self) -> int:
        with self._lock:
            return len([state for state in self._states if not state["finished"]])

    def all_finished(self) -> bool:
        with self._lock:
            return all(state["finished"] for state in self._states)

    def reset(self) -> bool:
        """清空下载状态；仍有任务在飞时不动它并返回 False。"""
        with self._lock:
            if any(not state["finished"] for state in self._states):
                return False
            self._states.clear() # 就地清空而非重新绑定，工作线程持有的是同一个列表对象
            self._completion_notified = False
            return True

    def submit(self, url: str, save_path: str) -> None:
        t = threading.Thread(target=self.download_file, args=(url, save_path))
        t.daemon = True # 非守护线程会阻止解释器退出，关窗后进程残留
        t.start()

    def download_file(self, url: str, save_path: str) -> None: # 在工作线程中执行
        current_state = { "download_url": url, "save_path": save_path, "downloaded_size": 0,
                          "total_size": 0, "finished": False, "failed_reason": None }
        with self._lock:
            self._states.append(current_state)

        part_path = save_path + ".part" # 先写临时文件，写完整了才改名，失败时不会留下能被当成课本打开的半截 PDF
        response = self.client.stream(url)

        # 服务器返回 401 或 403 状态码
        if response.status_code == 401 or response.status_code == 403:
            remove_part_file(part_path)
            with self._lock:
                current_state["finished"] = True
                current_state["failed_reason"] = "授权失败，Access Token 可能已过期或无效，请重新设置"
        elif response.status_code >= 400:
            remove_part_file(part_path)
            with self._lock:
                current_state["finished"] = True
                current_state["failed_reason"] = f"服务器返回状态码 {response.status_code}"
        else:
            with self._lock:
                current_state["total_size"] = int(response.headers.get("Content-Length", 0))

            try:
                with open(part_path, "wb") as file:
                    for chunk in response.iter_content(chunk_size=self.config.chunk_size):
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

                os.replace(part_path, save_path) # 只有完整写完才会出现目标文件
                with self._lock:
                    current_state["downloaded_size"] = current_state["total_size"]
                    current_state["finished"] = True
            except Exception as e:
                remove_part_file(part_path)
                logger.warning("下载失败：%s（%s）", url, e)
                with self._lock:
                    current_state["downloaded_size"], current_state["total_size"] = 0, 0
                    current_state["finished"] = True
                    current_state["failed_reason"] = str(e)

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
