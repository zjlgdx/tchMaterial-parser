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
from contextlib import closing
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


def new_download_state(url: str, save_path: str) -> dict:
    """一条下载任务的初始状态。

    只此一处构造：投递、直接调用、测试替身各抄一份的话，任何一处少一个键都要
    等到真跑起来才报 KeyError，而测试恰恰是绿的。
    """
    return { "download_url": url, "save_path": save_path, "downloaded_size": 0,
             "total_size": 0, "started": False, "finished": False,
             "failed_reason": None, "attempts": 0 }


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


# RFC 9110 §14.4：range-unit 大小写不敏感，Bytes 4-7/8 也是合法的 206
CONTENT_RANGE_RE = re.compile(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


def parse_content_range(response):
    """解析 206 的 Content-Range，返回 (起点, 终点, 资源总长)。

    头缺失、形如 bytes */8 这种没说清区间的、或者起点晚于终点的，一律返回
    None——这个响应证明不了正文是从我们请求的偏移开始的。总长写成 * 时第三项
    为 None，但起点与终点仍然是服务端明确承诺的。
    """
    raw = response.headers.get("Content-Range")
    if not raw:
        return None
    match = CONTENT_RANGE_RE.match(raw)
    if not match:
        return None
    first, last = int(match.group(1)), int(match.group(2))
    if first > last: # bytes 15-8/16 这种自相矛盾的头，当它不存在
        return None
    complete = match.group(3)
    return first, last, (None if complete == "*" else int(complete))


def content_range_starts_at(response, expected_start: int) -> bool:
    """确认 206 的 Content-Range 确实从我们要的位置开始。

    头缺失一律判为不可信：相同的校验子只说明资源版本没变，证明不了正文是从
    我们请求的偏移开始的——接着 append 一段起点不对的正文，拼出来的是一个
    「看起来成功」的损坏文件。整份重下只是慢一点。
    """
    parsed = parse_content_range(response)
    return parsed is not None and parsed[0] == expected_start


def is_unencoded(response) -> bool:
    """线路上的字节与写进文件的字节是不是同一批。

    有内容编码时它们不是：`iter_content` 交给我们的是解码后的字节，而
    `Content-Length` 数的是线路上的、`Range` 区间也是对着线路上那个表示算的。
    这一条同时否决两件事——长度对不上，偏移也对不上——所以两处共用这一个判断，
    别各判各的。`Accept-Encoding: identity` 从源头避免这种局面，而 identity
    本身是例外：RFC 9110 §8.4.1 里它的语义恰恰是「没有内容编码」，而我们主动
    发的那个请求头最容易招来服务端原样回显。
    """
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    return not encoding or encoding == "identity"


def declared_total_size(response, start: int):
    """这条响应说整份文件有多长；它没说就返回 None。

    只认真正说明**全长**的两个来源：`Content-Range` 的 `/Z`，以及首次下载时
    200 的 `Content-Length`。`/Z` 写成 `*` 不算——那只说明了这一段。
    """
    if not is_unencoded(response):
        return None

    parsed = parse_content_range(response)
    if parsed is not None:
        return parsed[2] # /Z；写成 * 就是「这条响应没说」

    if start: # 续上了一段却没有 Content-Range，这条响应什么也没说
        return None

    declared = response.headers.get("Content-Length")
    if declared is None:
        return None
    try:
        return int(declared)
    except ValueError:
        return None


def expected_bytes_on_disk(response, start: int, known_total=None):
    """这一轮写完之后 .part 应当有多长；无从判断就返回 None。

    判据按强弱排，**强的一旦拿到就不许被弱的顶掉**：

    1. `known_total`——之前某一轮已经问出来的全长。它是这份文件的属性，不会
       因为后面某条响应偷懒写了 `/*` 就失效。与它**矛盾**的声明是另一回事，
       那说明这次续传本身不可信，`_open_stream` 已经把那种响应挡在外面了，
       走到这里的 known 与 declared 不可能互相打架。
    2. 这条响应自己声明的全长（`/Z`，或首次的 `Content-Length`）。
    3. 都没有时，退到 `Content-Range` 的**终点加一**。它只说明「这一段发完之后
       盘上该到哪个偏移」，不能当成全长：RFC 9110 §15.3.7 允许 206 只满足所请求
       区间的一部分（CDN 按块切分就会这样）。够检出这一段的短传，不足以证明
       整份文件已经完整——**从头到尾都没人说过全长时，整份完整性无从验证**。

    「不知道」必须是 None 而不是 0。0 一旦参与加法就会变成一个看起来合理的
    错数：分块传输的 206 按 RFC 9110 §8.6 严禁带 Content-Length，拿
    start + Content-Length 当全长的话，每一次收全的续传都会被判成短传。
    """
    if known_total is not None:
        return known_total

    declared = declared_total_size(response, start)
    if declared is not None:
        return declared

    if not is_unencoded(response):
        return None

    parsed = parse_content_range(response)
    if parsed is None:
        return None
    return parsed[1] + 1


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


class DownloadCancelled(Exception):
    """关窗时置位取消标志，正在下载的任务据此提前退出。"""


class PermanentDownloadError(Exception):
    """重试也不会变好的失败：授权被拒、资源不存在、本地写盘失败。"""


class RetryableDownloadError(Exception):
    """值得再试一次的失败：服务端 5xx、限流、请求超时。"""


# 这些状态码重试才有意义：服务端临时故障、限流、请求超时
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class PartFile:
    """下载中的 .part 文件，以及它里面那些字节的身份。

    校验子（If-Range 用的 ETag / Last-Modified）与文件里的字节必须一体：
    只要两者能各自变化，就一定有分支让「记着的身份」描述的不是文件里现在
    躺着的那些字节，而续传会照着这个错误的身份把两份不同版本拼在一起——
    拼出来的是一个看起来成功的损坏 PDF，比半截文件更难发现。

    所以校验子只有 open_fresh() 一个赋值点，而那同时就是「清空文件、准备
    写入这一轮字节」的那一步。不写盘的路径（错误响应、取消、退避）在构造上
    够不到它；discard() 与 promote() 在交出或丢弃文件的同时清掉它。

    total 同理：它是「这份资源一共多长」，和校验子一样是这批字节的属性，因此
    绑在同一个赋值点上。记住它才挡得住「首轮明说了 100 字节，后一轮回个
    bytes 8-15/* 就把 16 字节当成整份文件交付」。
    """

    def __init__(self, path: str):
        self.path = path
        self.validator = None # 描述的就是此刻 path 里那些字节
        self.total = None # 这份资源的全长；从没问出来过就是 None

    @property
    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def discard(self) -> None:
        """丢弃残件；身份与全长随之作废。"""
        remove_part_file(self.path)
        self.validator = None
        self.total = None

    def open_fresh(self, validator, total=None):
        """清空重写：文件内容与它的身份、全长在同一步里一起换掉。

        身份等文件真的建出来再赋：open() 会因磁盘满、无写权限而抛，
        先赋上就等于让这个对象短暂地描述一个并不存在的文件。
        """
        self.discard()
        handle = open(self.path, "wb")
        self.validator = validator
        self.total = total
        return handle

    def open_append(self, expected_size: int):
        """续写：文件里已有的字节仍然属于 self.validator 描述的那份资源。

        打开之后复核一次大小。采样续传起点与真正打开之间隔着一整个网络往返，
        .part 若在这期间被外部删掉或截断（用户清理、杀毒软件），"ab" 会新建
        一个空文件，把服务端发来的后半段当成整份写下去，promote() 再把它当
        完整文件交出去——一个看起来成功的截断 PDF。
        """
        handle = open(self.path, "ab")
        actual = os.fstat(handle.fileno()).st_size
        if actual != expected_size:
            # 文件内容变了，身份就不再描述它——留着残件会让下一轮带着旧校验子
            # 从一个被篡改过的偏移继续往下接
            handle.close()
            self.discard()
            raise RetryableDownloadError(
                f"续传起点已失效（预期 {expected_size} 字节，实际 {actual} 字节）")
        return handle

    def promote(self, save_path: str) -> None:
        """完整写完了，原子改名到目标位置。"""
        os.replace(self.path, save_path)
        self.validator = None
        self.total = None


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

        登记必须发生在这里、由调用方线程同步完成，而不是挪进工作线程：线程池
        排队的任务、调用方还没解析完的后续链接，都还不在 _states 里；把「是否
        全部完成」建立在「此刻已登记的那几条」之上，第一个跑完的任务就会被当成
        全部跑完。也必须排在投递**之前**：投递一返回，工作线程随时可能已经在
        写盘了，此时它还不在 _states 里，快照就会说「整批完成」。

        投递失败是这里唯一棘手的地方。ThreadPoolExecutor 是先入队、再起线程，
        起线程失败时那条任务可能已经在队列里、待会真的会跑，调用方既拿不到
        future 也无从判断——猜哪一边都会漏。所以不猜：这条任务的归属由**锁**
        来定，工作线程开头那次检查与这里的判死在同一把 self._lock 里，两者
        只有一个能拿到它。
        """
        state = new_download_state(url, save_path)
        with self._lock:
            self._states.append(state)

        try:
            return self._ensure_executor().submit(self.download_file, url, save_path, state)
        except Exception as e:
            logger.warning("下载任务投递失败：%s（%s）", url, e)
            with self._lock:
                if state["started"]:
                    # 工作线程已经接手：它一定会走到自己的 finally，判定与清理
                    # 都归它。这里动任何一个字段都会和它打架
                    raise
                # 还没被接手：判死并摘掉。finished 置位就是留给那条「可能已入队」
                # 的任务看的暗号——它开跑前会读到，然后原地退出
                state["finished"] = True
                self._states[:] = [item for item in self._states if item is not state]
            naming.release_path(save_path) # 那条任务已被劝退，不会再用这个路径
            raise

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

    def _open_stream(self, url: str, resume_from: int, validator, known_total=None):
        """发起请求；resume_from > 0 时尝试续传，返回 (响应, 实际起点)。

        「这个 206 值不值得接着写」这件事，三道检查都在这里做完：校验子对不对、
        起点对不对、声明的全长与已知的矛不矛盾。走出这个函数时，拿到 206 就
        意味着三样都过了。
        """
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

        # 全长有三种情形，别混成「记住的赢」一句话：
        #   这条响应没说（/*）——弱信息，不许顶掉已经问出来的强信息；
        #   说了同一个——一致，照常往下接；
        #   说了另一个——矛盾。校验子对得上却给出不同的全长，说明我们对这份资源
        #   的认知已经不成立了，盘上那批字节不能再往下接。
        # 注意「一段的 Content-Range」不会误触发：/Z 始终是整份资源的长度，
        # bytes 8-15/100 里的 100 与已知的 100 一致
        declared = declared_total_size(response, resume_from)
        if known_total is not None and declared is not None and declared != known_total:
            logger.info("续传声明的全长与已知的不符（本次 %d，已知 %d），改为整份重下：%s",
                        declared, known_total, url)
            response.close()
            return self._stream(url), 0

        return response, resume_from

    def _download_once(self, url: str, part: PartFile, current_state: dict, resume_from: int):
        response, start = self._open_stream(url, resume_from, part.validator, part.total)

        # stream=True 的响应不关掉，连接要等 GC 才归还。出路不止状态码分类
        # 这一条：中途断流、写盘失败、取消、正常读完都要关，而中途断流恰恰是
        # 这条路径的常客——续传就是为它存在的
        with closing(response):
            if response.status_code >= 400:
                if response.status_code in (401, 403):
                    raise PermanentDownloadError("授权失败，Access Token 可能已过期或无效，请重新设置")
                if response.status_code in RETRYABLE_STATUS:
                    raise RetryableDownloadError(f"服务器返回状态码 {response.status_code}")
                # 404 这类结果重试三次也还是同一个答案，白等 1+2+4 秒
                raise PermanentDownloadError(f"服务器返回状态码 {response.status_code}")

            if start == 0:
                # 校验子与全长在这里、也只在这里设置：它们描述的就是紧接着写
                # 进去的那批字节。有内容编码时不留校验子——下一轮的 Range 会拿
                # 解码后的长度去请求编码后的偏移，对不上，而编码又恰好关掉了
                # 长度校验，拼出来的东西会被当成成功交付
                validator = response_validator(response) if is_unencoded(response) else None
                # 整份重来，旧的全长跟着旧字节一起作废，不能带到这一轮
                known_total = declared_total_size(response, 0)
                handle = part.open_fresh(validator, known_total)
            else:
                known_total = part.total # 记住的全长描述的正是盘上这批字节
                handle = part.open_append(start)

            # 「这一轮写完之后盘上该有多少字节」只由这一处回答，进度条与短传
            # 校验共用它。拿不到就是 None，绝不退化成一个能参与算术的 0
            expected = expected_bytes_on_disk(response, start, known_total)

            with self._lock:
                current_state["total_size"] = expected or 0
                current_state["downloaded_size"] = start

            with handle as file:
                for chunk in response.iter_content(chunk_size=self.config.chunk_size):
                    if self._cancelled.is_set():
                        raise DownloadCancelled()
                    file.write(chunk)
                    with self._lock: # 只改自己那一条；聚合由 snapshot() 在读的时候做
                        current_state["downloaded_size"] += len(chunk)

        # 服务端说清了全长就核一遍。少收的字节同样会被 promote() 当成完整文件
        # 交出去，而截断的 PDF 是「看起来成功」的那一类失败
        if expected is not None and part.size != expected:
            raise RetryableDownloadError(
                f"收到的字节数与服务端声明的不符（{part.size}/{expected}）")

    def download_file(self, url: str, save_path: str, current_state: dict = None) -> None: # 在工作线程中执行
        if current_state is None: # 直接调用（测试）时也要登记，保持与 submit 一致
            current_state = new_download_state(url, save_path)
            with self._lock:
                self._states.append(current_state)

        with self._lock:
            if current_state["finished"]:
                # 投递侧已经替这条任务判了死：它入了队，但线程没起来，调用方
                # 以为它不会跑。此刻它已经不在 _states 里、文件名预留也归还了，
                # 再跑就是写一个没人认得的文件，还可能和拿到同一路径的新任务
                # 对着写同一个 .part。认赔退出
                logger.info("投递侧已放弃这条任务，工作线程不再执行：%s", url)
                return
            current_state["started"] = True # 从这里起，这条任务归工作线程管

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
