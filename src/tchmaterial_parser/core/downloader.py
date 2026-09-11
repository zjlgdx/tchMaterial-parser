# -*- coding: utf-8 -*-
"""下载调度与状态。

工作线程只在锁内更新纯数据，界面变化一律经回调交回调用方——本模块因此
不需要知道 Tkinter 的存在。
"""

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

from ..config import AppConfig
from . import naming
from .errors import NetworkError

logger = logging.getLogger(__name__)

RETRY_BACKOFF = (1.0, 2.0, 4.0) # 退避秒数，第 n 次重试取第 n 项
# 校验子的优先级：判断续传拿到的是不是同一份文件。
# 不含 Content-Length——206 响应里它是剩余字节数，必然与首次的全长不等，
# 拿它做 If-Range 只会让每一次有效的续传都被判成不一致；它也不是合法的
# entity-tag 或 HTTP-date
VALIDATOR_HEADERS = ("ETag", "Last-Modified")


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


def content_range_starts_at(response, expected_start: int) -> bool:
    """确认 206 的 Content-Range 确实从我们要的位置开始。

    头缺失时按可信处理：不是所有服务端都给，而真正的防线是 If-Range 校验子。
    """
    raw = response.headers.get("Content-Range")
    if not raw:
        return True
    match = re.match(r"\s*bytes\s+(\d+)-", raw)
    if not match:
        return False
    return int(match.group(1)) == expected_start


@dataclass(frozen=True)
class DownloadSnapshot:
    """某一时刻的聚合状态。

    不可变，且只含纯数据：工作线程把状态改在锁里，界面每个 tick 读一次这个
    快照就够了——按分块回调会让主线程在每个 tick 重放上百次同样的刷新。
    """

    downloaded_size: int = 0
    total_size: int = 0
    finished: int = 0
    total: int = 0
    in_flight: int = 0
    failures: tuple = ()
    last_dir: str = ""

    @property
    def percent(self) -> float:
        if self.total_size <= 0:
            return 0.0
        return (self.downloaded_size / self.total_size) * 100

    @property
    def all_finished(self) -> bool:
        return self.total > 0 and self.in_flight == 0

    def progress_text(self) -> str:
        if self.total == 0:
            return "等待下载"
        return (f"{format_bytes(self.downloaded_size)}/{format_bytes(self.total_size)}"
                f" ({self.percent:.2f}%) 已下载 {self.finished}/{self.total}")

    def failure_detail(self) -> str:
        return "\n".join(f"{url}，原因：{reason}" for url, reason in self.failures)


class PartFile:
    """下载中的 .part 文件，以及它里面那些字节的身份。

    校验子曾经被单独维护在一个字典里，于是「记着的身份」和「文件里真实的
    字节」会在各种分支上悄悄脱钩——三轮评审抓到过三条不同的路径：校验太松、
    整份重下时忘了清、错误响应也参与赋值。根因是同一个：两者本该是一体的。

    这里把它们绑死：校验子只有一个赋值点，就在 open_fresh() 里，而那同时
    也是「把文件清空、准备写入这一轮字节」的那一步。不写盘的分支（错误响应、
    取消、退避）在构造上就够不到它；任何丢弃 .part 的地方都会同步清掉它。
    """

    def __init__(self, path: str):
        self.path = path
        self.validator = None # 描述的就是此刻 path 里那些字节

    @property
    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def discard(self) -> None:
        """丢弃残件；身份随之作废。"""
        remove_part_file(self.path)
        self.validator = None

    def open_fresh(self, validator):
        """清空重写：文件内容与它的身份在同一步里一起换掉。"""
        self.discard()
        self.validator = validator
        return open(self.path, "wb")

    def open_append(self):
        """续写：文件里已有的字节仍然属于 self.validator 描述的那份资源。"""
        return open(self.path, "ab")

    def promote(self, save_path: str) -> None:
        """完整写完了，原子改名到目标位置。"""
        os.replace(self.path, save_path)
        self.validator = None


class DownloadCancelled(Exception):
    """关窗时置位取消标志，正在下载的任务据此提前退出。"""


class PermanentDownloadError(Exception):
    """重试也不会变好的失败：授权被拒、资源不存在、本地写盘失败。"""


class RetryableDownloadError(Exception):
    """值得再试一次的失败：服务端 5xx、限流、请求超时。"""


# 这些状态码重试才有意义：服务端临时故障、限流、请求超时
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class DownloadManager:
    def __init__(self, client, config: AppConfig = None):
        self.client = client
        self.config = config or AppConfig()
        self._lock = threading.Lock()
        self._states = []
        self._cancelled = threading.Event()
        self._executor = None

    # ---- 状态 ----

    def states(self) -> list:
        with self._lock:
            return [dict(state) for state in self._states]

    def snapshot(self) -> DownloadSnapshot:
        """在锁内一次取齐聚合值，避免读到别的线程写到一半的状态。"""
        with self._lock:
            if not self._states:
                return DownloadSnapshot()
            return DownloadSnapshot(
                downloaded_size=sum(s["downloaded_size"] for s in self._states),
                total_size=sum(s["total_size"] for s in self._states),
                finished=len([s for s in self._states if s["finished"]]),
                total=len(self._states),
                in_flight=len([s for s in self._states if not s["finished"]]),
                failures=tuple((s["download_url"], s["failed_reason"])
                               for s in self._states if s["failed_reason"]),
                last_dir=os.path.dirname(self._states[-1]["save_path"]),
            )

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
            self._cancelled.clear()
            return True

    # ---- 调度 ----

    def _ensure_executor(self):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self.config.max_download_workers, thread_name_prefix="download")
        return self._executor

    def submit(self, url: str, save_path: str):
        """登记任务并投递。

        登记必须发生在这里而不是工作线程里：线程池排队的任务、调用方还没
        解析完的后续链接，都还不在 _states 里；把「是否全部完成」建立在
        「此刻已登记的那几条」之上，第一个跑完的任务就会被当成全部跑完。
        """
        state = { "download_url": url, "save_path": save_path, "downloaded_size": 0,
                  "total_size": 0, "finished": False, "failed_reason": None, "attempts": 0 }
        with self._lock:
            self._states.append(state)
        return self._ensure_executor().submit(self.download_file, url, save_path, state)

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

    def _stream(self, url: str, headers=None):
        """所有出站请求的唯一入口：发之前先看一眼取消标志。

        一次尝试里可能发两个请求（续传被判不可信之后还要整份重下），
        只在重试循环顶上检查是拦不住第二个的。
        """
        if self._cancelled.is_set():
            raise DownloadCancelled("资源下载已取消")
        return self.client.stream(url, headers=headers)

    def _open_stream(self, url: str, resume_from: int, validator):
        """发起请求；resume_from > 0 时尝试续传，返回 (响应, 实际起点)。"""
        headers = None
        if resume_from > 0 and validator is not None:
            headers = { "Range": f"bytes={resume_from}-", "If-Range": validator[1] }

        response = self._stream(url, headers=headers)

        if headers is None:
            return response, 0

        if response.status_code == 200:
            # 服务端按 HTTP 规范拒绝了 If-Range，直接把完整的新文件发了过来。
            # 它就是我们要的东西，从头写下去即可，不必关掉再重下一遍
            return response, 0

        if response.status_code == 416:
            # 416 是对「我们要的区间」的回答，不是对这个资源的回答：
            # .part 比远端还长。丢掉它整份重下才是对的，判永久失败会让一次
            # 本可成功的重下变成失败
            logger.info("续传区间无效（416），改为整份重下：%s", url)
            response.close()
            return self._stream(url), 0

        if response.status_code >= 400:
            # 其余错误响应原样交给调用方分类：在这里吞掉再重发一次，会让
            # 「4xx 不重试」在续传路径上失效，还平白多出一个出站请求
            return response, 0

        if response.status_code != 206:
            logger.info("续传返回了非预期状态码 %d，改为整份重下：%s", response.status_code, url)
            response.close()
            return self._stream(url), 0

        # 只有拿到与首次同类型、同值的校验子才敢接着写。校验子缺失、类型不同
        # （首次给 ETag、206 只带别的头）都说明我们无从判断这是不是同一份文件；
        # 对一个「接受 Range 但忽略 If-Range」的服务端，接着写就会拼出
        # 「旧文件前缀 + 新文件后缀」——一个看起来成功的损坏 PDF，比半截文件更难发现
        current = response_validator(response)
        if current is None or current[0] != validator[0] or current[1] != validator[1]:
            logger.info("续传校验子不可信（本次 %s，首次 %s），放弃续传并重新下载：%s",
                        current, validator, url)
            response.close()
            return self._stream(url), 0

        # 206 也要确认它续的是我们要的那一段：服务端回 206 却给 bytes 0-...
        # 时接着 append，拼出来的文件会带一段重复的前缀
        if not content_range_starts_at(response, resume_from):
            logger.info("续传的 Content-Range 起点与预期不符（%r，期望 %d），改为整份重下：%s",
                        response.headers.get("Content-Range"), resume_from, url)
            response.close()
            return self._stream(url), 0

        return response, resume_from

    def _download_once(self, url: str, part: PartFile, current_state: dict, resume_from: int):
        response, start = self._open_stream(url, resume_from, part.validator)

        if response.status_code >= 400:
            # stream=True 的响应不消费也不关闭，连接要等 GC 才归还
            response.close()
            if response.status_code in (401, 403):
                raise PermanentDownloadError("授权失败，Access Token 可能已过期或无效，请重新设置")
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableDownloadError(f"服务器返回状态码 {response.status_code}")
            # 404 这类结果重试三次也还是同一个答案，白等 1+2+4 秒
            raise PermanentDownloadError(f"服务器返回状态码 {response.status_code}")

        declared = int(response.headers.get("Content-Length", 0))
        if start == 0:
            total = declared
            # 校验子在这里、也只在这里设置：它描述的就是紧接着写进去的字节
            handle = part.open_fresh(response_validator(response))
        else:
            total = start + declared
            handle = part.open_append()

        with self._lock:
            current_state["total_size"] = total
            current_state["downloaded_size"] = start

        with handle as file:
            for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                if self._cancelled.is_set():
                    raise DownloadCancelled()
                file.write(chunk)
                with self._lock: # 只改自己那一条；聚合由 snapshot() 在读的时候做
                    current_state["downloaded_size"] += len(chunk)

    def download_file(self, url: str, save_path: str, current_state: dict = None) -> None: # 在工作线程中执行
        if current_state is None: # 直接调用（测试）时也要登记，保持与 submit 一致
            current_state = { "download_url": url, "save_path": save_path, "downloaded_size": 0,
                              "total_size": 0, "finished": False, "failed_reason": None, "attempts": 0 }
            with self._lock:
                self._states.append(current_state)

        # 先写临时文件，写完整了才改名，失败时不会留下能被当成课本打开的半截 PDF
        part = PartFile(save_path + ".part")
        failed_reason = None

        try:
            for attempt in range(self.config.max_retries + 1):
                if self._cancelled.is_set(): # 每轮开始前先看一眼，别在关窗后又发一次请求
                    failed_reason = "下载已取消"
                    break

                with self._lock:
                    current_state["attempts"] = attempt + 1

                # 第一轮不续上一次运行留下的残件：跨进程续传无法确认它还对应
                # 同一份远端文件
                resume_from = part.size if attempt else 0
                try:
                    self._download_once(url, part, current_state, resume_from)
                    part.promote(save_path) # 只有完整写完才会出现目标文件
                    failed_reason = None
                    break
                except DownloadCancelled:
                    failed_reason = "下载已取消"
                    break
                except (NetworkError, RetryableDownloadError, requests.RequestException) as e:
                    # 只有真正的网络类失败值得再试
                    failed_reason = str(e)
                    if attempt >= self.config.max_retries:
                        break
                    logger.info("下载失败将重试（第 %d 次）：%s（%s）", attempt + 1, url, e)
                    # 用 wait 而不是 sleep：关窗时不必陪着退避把 1+2+4 秒等完
                    if self._cancelled.wait(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]):
                        failed_reason = "下载已取消"
                        break
                except Exception as e:
                    # 授权被拒、404、本地写盘失败：重试也不会变好
                    failed_reason = str(e)
                    break
        finally:
            if failed_reason is not None:
                part.discard()
                logger.warning("下载失败：%s（%s）", url, failed_reason)

            naming.release_path(save_path) # 归还预留的文件名，失败重下时还能拿回原名

            with self._lock:
                current_state["finished"] = True
                current_state["failed_reason"] = failed_reason
                if failed_reason is not None:
                    current_state["downloaded_size"], current_state["total_size"] = 0, 0
                elif current_state["total_size"]:
                    current_state["downloaded_size"] = current_state["total_size"]
                else:
                    # 服务端没给 Content-Length：已写入的字节数就是总大小，
                    # 反过来把它抹成 0 会让完成瞬间的进度条掉回 0%
                    current_state["total_size"] = current_state["downloaded_size"]

        # 完成判定不在这里做：工作线程看不到「调用方还打算提交几个」，
        # 只有主线程知道一批下载什么时候算结束


def build_save_path(dir_path: str, title: str) -> str:
    """在目标目录里为一本教材分配安全且互不冲突的路径。"""
    save_path = naming.unique_path(dir_path, naming.sanitize_filename(title), ".pdf")
    naming.assert_within(dir_path, save_path)
    return save_path
