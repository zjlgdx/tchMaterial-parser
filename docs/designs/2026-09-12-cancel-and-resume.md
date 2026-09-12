# 批量下载的取消与暂停/续传

**Date:** 2026-09-12

## Context

`tchMaterial-parser` 的资源树支持勾选整个分类批量下载，勾选顶层分类可能产生数千个末级资源。当前实现里，点击“下载”之后没有任何中止手段：`download_btn` 在整批期间保持 `state="disabled"`，唯一的退路是关闭整个程序（`app.py` 的 `on_closing` 弹一次确认框，然后用 `psutil` 结束子进程并 `root.destroy()`）。一次误点可能是几十分钟、几十 GB 且不可中止的下载。

本设计只覆盖：

1. 整批下载可随时**取消**（覆盖解析阶段与下载阶段）。
2. 整批下载可**暂停 / 继续**，继续时用 HTTP Range 续传半截文件，不从头重来。
3. 多文件下载前的提示写明具体文件数。

不覆盖（与 issue 的“不做”一致，本设计不扩这个边界）：

- 跨会话续传——半截 `.tmp` 在程序退出后作废，不新增落盘的状态文件。
- 单个任务级别的暂停/取消。
- 与本需求无关的重构。

涉及的现有文件：

- `src/tchmaterial_parser/ui/download_panel.py`（主要改动位置）
- `src/tchmaterial_parser/app.py`（底部两个按钮的初始绑定；本设计确认它**不需要改动**，见 Architecture）
- `tests/test_download.py`、`tests/test_download_batch.py`、`tests/test_download_progress.py`（现有测试，改动必须保持通过）
- 新增 `tests/test_download_cancel_resume.py`

## Discussion

### 对 issue 中断言的逐条核实

issue 里引用的行号是撰写时的快照，本节按当前工作树（基线 `9ccc116`）重新核对，凡是核实为真的直接采纳，不再重复各自的行号截图。

| 断言 | 核实结果 |
|---|---|
| `download_panel.py` 中 `cancel/stop/pause/abort/Event(` 出现次数均为 0 | **属实**。已用 `grep -o` 逐词统计，五个计数确实都是 0。 |
| `download_btn` 整批期间 `state="disabled"`，唯一退路是退出程序 | **属实**。`download()` 入口即 `download_btn.config(state="disabled")`；批内多处“提前返回”分支和 `finish_download_batch` 才会把它设回 `"normal"`；期间没有任何其它入口能让用户中止。`app.py` 的 `on_closing` 逐字匹配 issue 描述（`askokcancel("提示", "下载任务未完成，是否退出？")` → 结束子进程 → `root.destroy()`）。 |
| `current_state["total_size"] = int(response.headers.get("Content-Length", 0))` | **属实，且行号仍对得上**（当前第 432 行）。这一行完全没有考虑 206 响应，是本设计要修的核心 bug 之一。 |
| `REQUEST_TIMEOUT` 读超时 60 秒 | **属实**。`network.py`：`REQUEST_TIMEOUT = (10, 60)`（连接 10s / 相邻两次收数据 60s）。 |
| `_MIN_REQUEST_INTERVAL = 0.2` 是跨线程共享的全局锁，工作线程数为 3 | **属实**。`_rate_lock` 是模块级 `threading.Lock`，`_download_slots = threading.BoundedSemaphore(3)`，`start_download_batch` 用 `ThreadPoolExecutor(max_workers=3)`。 |
| 全文没有 `If-Range` / `Content-Range` / `Accept-Encoding` / 206 / 416 相关代码 | **属实**（对整个 `src/` 做了 grep，零命中）。说明续传能力目前完全不存在，不是“有 bug 的续传”，是“没有续传”。 |
| `python -m pytest` 基线 110 passed / 47 subtests，flake8 输出 0 | **属实**，已在当前工作树上原样跑过一遍复现。 |
| 目录/耗时/文件大小的具体数字（2920 个、9 分钟、70 GB 等） | **未独立复核**。这些是 navigator 用真实账号对真实接口实测的结果，本次设计没有对线上接口发起验证性请求（没有必要，也不在“设计”阶段的职责内）。这些数字只影响“这个问题有多严重”的严重性判断，不影响架构：本设计对任意 N 都成立，不依赖具体数字，所以数字本身是否精确不构成设计漂移。 |

结论：issue 正文没有发现失实之处，唯一需要提醒的是“已知的坑”里部分描述是**预判**（因为续传功能还不存在），下面单独核实这些预判是否成立、以及本设计怎么应对。

### 对“已知的坑”十条的核实与应对

逐条给出：现状核实 → 本设计的应对 → 对应测试（测试名均在 `tests/test_download_cancel_resume.py` 新增，除非另有说明）。

1. **206 的总长必须来自 `Content-Range` 而不是 `Content-Length`。**
   核实：属实，见上表。
   应对：新增 `parse_content_range(header_value) -> tuple[start, end, total] | None`，只在 `response.status_code == 206` 时解析并使用其中的 `total` 作为 `total_size`；非 206 响应仍取 `Content-Length`。这一步是 Architecture (b) 里“一次决定、三个输出”的一部分，不是独立分支。
   测试：`test_206_total_size_comes_from_content_range_not_content_length`。

2. **必须带 `If-Range`，服务端返回 200 时必须截断重下，不能追加，且计数器要跟着截断一起归零。**
   核实：属实，代码里完全没有校验子概念，也没有任何“是否可以追加”的判断。
   应对：见 Architecture (b)。追加/截断、`downloaded_size` 的起始值、`total_size` 的取值来源，三者由同一处判断一次性给出，不允许出现“开 `wb` 但计数器仍按旧偏移起算”这种组合。
   测试：`test_resume_fallback_to_full_restart_resets_downloaded_size_and_keeps_the_file`（服务端在续传请求上回 200：断言临时文件被截断重写、`downloaded_size` 从 0 起算、最终文件不被误判成不完整而删除）。

3. **必须带 `Accept-Encoding: identity`。**
   核实：属实，`network.py` 的公共 `headers` 没有设置该字段，`requests`/`urllib3` 会用默认值（通常是 `gzip, deflate`），字节偏移在压缩传输下没有意义；而且首次下载与续传若使用不同的编码协商，两次拿到的表示（原始字节 vs. 解压后字节）可能不一致，`Content-Length` 与实际写入字节数、以及两次响应的校验子都可能对不上。
   应对：`identity` 编码是 `request_download`（下载专用的请求函数）对**所有**下载请求的固定请求头，不分是否带 Range；`api.py`/`catalog.py` 里目录抓取、详情解析用的请求不改，仍走各自原有的 `session`/`request_headers` 调用。
   测试：`test_download_requests_always_set_identity_encoding`（断言首次请求与续传请求的 `Accept-Encoding` 都是 `identity`）。

4. **416 要当“范围无效”处理，从 0 重下。**
   核实：属实，零处理。
   应对：见 Architecture (b) 的 `plan_download_write`。带 Range 的请求收到 416 时，关闭这次响应，放弃这次的偏移与校验子，原样以一次全新的、不带 Range 的请求重新走一遍，其结果按“非续传”对待（`wb`、`downloaded_size` 归零、`total_size` 取新响应的 `Content-Length`）。
   测试：`test_resume_retries_from_scratch_on_416`。

5. **暂停不能靠挂起读线程，必须断开连接。**
   核实：属实——目前没有暂停功能，但如果照搬“sleep 等待”的直觉实现，确实会撞上 60 秒读超时（`REQUEST_TIMEOUT` 已核实）。
   应对：暂停由“主动断开”和“协作式检查”两条路径共同保证，见 Architecture (a)。任何一条命中都不会让工作线程停留在一次阻塞的 socket 读上超过一个 chunk 的时间。
   测试：`test_pause_disconnects_instead_of_blocking_the_reader`（用一个必须等待 `close()` 被调用才会让 `iter_content` 返回的假响应，断言暂停发生在“读超时”量级之外的短时间内，且 `close()` 确实被调用过）。

6. **`downloaded_size` 续传时必须从已有偏移起算。**
   核实：属实，目前 `create_download_state` 永远从 0 起算，没有续传路径。
   应对：起始偏移**永远现读 `os.path.getsize(temp_path)`**，不信任内存里可能过期的计数器；这个偏移只有在 Architecture (b) 判定“确实是可追加的续传”时才会被采纳为 `downloaded_size` 的起始值，否则连同 `total_size`、`open_mode` 一起按全新下载处理（见坑 2）。
   测试：`test_resume_accumulates_downloaded_size_from_existing_temp_file_offset`。

7. **完整性校验 (`downloaded_size != total_size`) 续传后仍要成立。**
   核实：属实，且坑 1、坑 2 的 bug 若不修，这条校验在续传场景下必然是错的：要么 `total_size` 被误设成“本次剩余长度”，要么 `downloaded_size` 沿用了旧偏移却搭配了一次全新的 `wb` 写入，两种情况都会让这条校验在文件其实完好时误判为不完整并删除它。
   应对：坑 1、坑 2 修好后，`downloaded_size` 与 `total_size` 在任何时刻都指向同一份“这次写入”的口径（要么都是“旧偏移 + 新增字节”，要么都是“从 0 开始”），此校验不需要为续传单独改逻辑。
   测试：`test_resume_completes_integrity_check_successfully`。

8. **镜像轮换与续传：不同镜像续传时 `If-Range` 是唯一的安全网，且要防止“接上错误的位置”。**
   核实：属实，`download_mirror_urls` 会在 r1/r2/r3 之间轮换，且轮换逻辑与续传逻辑目前完全不相交（续传还不存在）；另外光凭状态码 206 不能保证服务端真的从我们请求的偏移开始返回——服务端也可能因为自身实现而回一个别的区间。
   应对：`request_download` 的镜像轮换逻辑保持不变、原样复用，续传只是多带了 `Range`/`If-Range`/`Accept-Encoding`，这些头在每一个候选镜像上都会原样发送。是否可追加不只看状态码是不是 206，还要看 `Content-Range` 解析出的起始位置是否等于本次请求的偏移（见 Architecture (b)）；一旦不等，按不可续传处理。因此不管最终由哪个镜像应答、应答的区间是否符合预期，正确性都由这个校验兜底，不依赖“猜哪个镜像会命中”。
   测试：`test_resume_across_mirror_rotation_still_sends_if_range`、`test_resume_requires_content_range_start_to_match_requested_offset`、`test_unusable_206_is_never_written_as_if_it_were_the_full_file`、`test_206_with_unparseable_content_range_is_never_written_as_if_it_were_the_full_file`（后两条走完整的 `download_file`，断言最终文件的字节，不只是 `open_mode` 与计数器）。

9. **信号量/全局锁在取消/暂停时不能泄漏。**
   核实：属实需要留意，但当前代码结构本身是安全的（`with _download_slots:` 包住了整个请求与写入过程，任何 `return`/异常都会走 `__exit__` 释放）。风险点在于**新增的提前返回分支**是否都写在这个 `with` 块内部。
   应对：所有新增的“检测到取消/暂停就提前结束”的分支都放在现有 `with _download_slots:` 内部，不额外发明新的锁；`_pace_request` 的 `_rate_lock` 只在其自身函数体内临界区持有，不受本次改动影响。
   测试：`test_pause_and_cancel_do_not_leak_download_slots`（暂停/取消后立即用 `_download_slots.acquire(timeout=...)` 三次不阻塞，证明许可数恢复到 3）。

10. **暂停期间点取消，此时批次线程已经退出，没有人来收尾。**
    核实：属实，且需要格外小心的是“已经发出暂停请求”和“批次已经因暂停而停稳”是两个不同的时间点——中间有一段窗口，此时批次线程仍然存活、仍然持有 `.tmp` 的文件句柄。如果把这两个时间点混为一谈（例如只用“暂停请求”这一个标志来判断“批次线程是否已经退出”），会导致在这段窗口里点“取消”时，主线程去删一个工作线程仍在写的文件，以及批次线程随后退出时又重复走一遍收尾。
    应对：见 Architecture (a)(d)，用两个独立的标志分别表达“已请求暂停”与“批次已经停稳为暂停态”，后者只由主线程在批次线程确认退出之后才置位，取消按钮只认后者来决定是否需要自己同步收尾。
    测试：`test_cancel_while_truly_paused_cleans_up_without_a_live_batch_thread`、`test_cancel_immediately_after_pause_request_is_handled_by_the_batch_thread`。

## Approach

整体思路：**不引入新的持久化状态、不新增控件**，只在现有的 `download_panel.py` 模块级状态旁边增加一个贯穿“点下下载”到“批次终结”整段生命周期的批次控制对象，并把底部两个按钮从“下载期间闲置/禁用”改成“下载期间承担取消/暂停/继续”。

关键取舍：

- 续传所需的校验子、总长、已下载偏移，全部放进已经存在的每文件状态字典（`download_states[i]`），不新建状态文件、不新建模块。这既满足“不做跨会话续传”的边界，也满足“最小修复”。
- 续传相关的三个输出——用什么模式打开文件、`downloaded_size` 从哪起算、`total_size` 取哪个响应头——由同一段逻辑一次性给出，不允许分开判断，避免出现“判了截断却没归零计数器”这类不一致。
- 暂停的停止机制是“协作式检查 + 主动断连”双保险，而不是挂起线程，直接对应坑 5；“已请求暂停”与“已经停稳为暂停”是两个独立的标志，只有后者代表“批次线程已经退出”。
- 批次控制对象在用户点下“下载”的那一刻就创建，覆盖解析阶段与下载阶段的整段生命周期，不依赖“已经选好保存目录”这个后置条件。
- 取消/暂停/继续三个动作全部由**已经存在的两个按钮**承担，样式（ttk style）不变，只改 `text`/`command`/`state`，因此不需要额外检查深色/浅色主题——`theme.py` 的样式表是按 ttk style 名（`Accent.TButton` 等）索引的，不依赖按钮文案，逐行确认过 `theme.py` 没有任何按文案分支的逻辑。

## Architecture

### (a) 暂停/取消的控制状态放在哪、谁读写、线程安全怎么保证

新增一个模块级单例（`download_panel.py` 内，紧邻现有的 `download_states`），在 `download()` 一开始就创建，覆盖解析阶段与下载阶段：

```python
class BatchControl:
    def __init__(self) -> None:
        self.cancel_event = threading.Event()   # 已请求取消
        self.pause_event = threading.Event()    # 已请求暂停（用户点下“暂停”的瞬间即置位）
        self.paused_settled = False             # 批次真正停稳为“暂停”后才置位；只在主线程读写
        self.directory: str | None = None       # 解析阶段尚未选定目录；选定后再写入
        self.lock = threading.Lock()            # 只保护 active_responses
        self.active_responses: dict[int, object] = {}  # id(state) -> Response，用于主动断连

_batch_control: BatchControl | None = None  # 当前批次；空闲时为 None
```

- **写入方**：
  - 主线程（按钮回调 `cancel_current_batch` / `pause_current_batch` / `resume_current_batch`，以及负责判定终态的 `handle_batch_outcome`）调用 `cancel_event`/`pause_event` 的 `.set()`/`.clear()`，并在需要时遍历 `active_responses` 主动 `close()`。
  - `paused_settled` 只由 `handle_batch_outcome` 在判定结局为“暂停”时置位为 `True`，只由 `cancel_current_batch` 读取；`resume_current_batch` 在重新发起下载前把它连同 `pause_event` 一起清回初始状态，让这一轮可能再次发生的暂停从头计起。两端都在主线程（Tkinter 的事件循环单线程执行），不需要加锁，也不允许工作线程读写这个字段——它表达的是“已经请求暂停”与“暂停已经生效、线程已经退出”这两件不同的事，不能用 `pause_event` 兼职表达，否则无法区分坑 10 描述的那段窗口期。
  - 工作线程在 `request_download` 成功拿到响应后，把自己的响应对象登记进 `active_responses`（`with control.lock: control.active_responses[id(state)] = response`），下载结束/失败/暂停时移除。
- **读取方**：工作线程在“每写完一个 chunk 之后”“开始处理下一个排队任务之前”两个点读 `cancel_event.is_set()` / `pause_event.is_set()`；两者都命中时取消优先（更彻底的操作胜出），这个优先级在 (c) 的判定逻辑与 `download_file` 的分类逻辑里保持一致。
- **线程安全**：
  - `threading.Event.set/clear/is_set` 本身是线程安全的原子操作，标准库保证。
  - 唯一在工作线程与主线程之间共享的可变结构是 `active_responses`，用一个专门的 `threading.Lock` 保护增删和“遍历并关闭”，临界区只做字典操作和调用 `.close()`（`.close()` 不会长时间阻塞），与文件里现有的 `_rate_lock` 用法是同一套约定，没有引入新的加锁风格。
  - 不会死锁：持锁期间不会去等待另一个需要同一把锁的操作。

### (b) 续传所需的校验子与总长跟着什么走；为什么不会把新内容追加到旧半截文件后面

校验子、总长、已下载量全部跟着**已经存在的 per-file 状态字典**走，不新建文件、不新建落盘结构：

```python
def create_download_state(url, save_path, chapters=None) -> dict:
    return {
        "download_url": url, "save_path": save_path,
        "downloaded_size": 0, "total_size": 0,
        "finished": False, "failed_reason": None,
        "chapters": chapters,       # 续传时重新提交任务要用到
        "validator": None,          # ETag 优先，否则 Last-Modified；都没有则为 None
    }
```

`request_download` 对所有下载请求（不分是否续传）固定加上 `Accept-Encoding: identity`（对应坑 3）；只在调用方传入偏移时才附加 `Range`/`If-Range`：

```python
def request_download(url, range_from: int | None = None, validator: str | None = None):
    extra_headers = {"Accept-Encoding": "identity"}
    if range_from is not None:
        extra_headers["Range"] = f"bytes={range_from}-"
        if validator:
            extra_headers["If-Range"] = validator
    # 其余（镜像轮换、400 退避重试）与现有实现完全一致，只是把 extra_headers 并入每次请求头
    ...
```

“用什么模式打开文件”“`downloaded_size` 从哪起算”“`total_size` 取哪个响应头”“要不要刷新校验子”是同一个决定的四个输出，写在同一段逻辑里，不允许分开判断。这里有一条容易漏掉的分界线，必须显式说清楚：**响应是不是 206，和响应能不能被当整份正文使用，是两件不同的事**——一个不可用的 206（起点不匹配、或 `Content-Range` 解析不出来）不能落回“当成普通响应，`wb` 截断、`total_size` 取这次的 `Content-Length`”，因为它的响应体只是被请求的那一段，不是完整正文；把它的 `Content-Length` 当成整份文件的长度、把它的 body 当成整份文件的内容写下去，会产出一个大小和计数器都自洽、内容却是错的文件，且不会触发任何失败提示。**判定为“不可用”的 206 必须和 416 走同一条路：关闭这次响应，重新发一次不带 Range 的全新请求，把新响应当作真正的完整正文来源。**

```python
def parse_content_range(header_value: str | None) -> tuple[int, int, int] | None:
    """解析 `Content-Range: bytes start-end/total`；缺失或格式不对时返回 None，
    调用方一律按不可续传处理（对应坑 9）。"""
    ...

def plan_download_write(current_state: dict, temp_path: str, url: str):
    offset = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
    can_attempt_range = offset > 0 and bool(current_state["validator"])

    if can_attempt_range:
        response, attempted_urls = request_download(url, range_from=offset, validator=current_state["validator"])
    else:
        response, attempted_urls = request_download(url)

    if can_attempt_range and response.ok:
        content_range = parse_content_range(response.headers.get("Content-Range")) if response.status_code == 206 else None
        # 追加的前提：本地确有偏移、手上有校验子、服务端真的回了 206、且这段的起点正好等于我们请求的偏移（对应坑 8）
        usable_206 = response.status_code == 206 and content_range is not None and content_range[0] == offset
    else:
        content_range, usable_206 = None, False

    # 416（范围无效）与“回了 206 但接不上”是同一类不可信响应：这次的响应体不是完整正文，
    # 绝不能当整份写下去。两者一律不信任这次响应，关掉后按一次全新的、不带 Range 的请求重来
    # （对应坑 4，以及“206 只是校验起点、落回分支对 206 本身仍然错误”这个此前遗漏的情形）。
    if can_attempt_range and (response.status_code == 416 or (response.status_code == 206 and not usable_206)):
        response.close()
        offset = 0
        can_attempt_range = False
        response, attempted_urls = request_download(url)
        content_range, usable_206 = None, False

    if not response.ok:  # 失败响应交给调用方走既有的失败分支，这里不去碰它未必存在的响应头
        return "wb", response, attempted_urls

    open_mode = "ab" if usable_206 else "wb"
    current_state["downloaded_size"] = offset if usable_206 else 0
    current_state["total_size"] = content_range[2] if usable_206 else int(response.headers.get("Content-Length", 0))

    if response.status_code == 200:  # 这次响应携带的是完整正文，不论请求时有没有带 Range（对应坑 7）
        current_state["validator"] = response.headers.get("ETag") or response.headers.get("Last-Modified")

    return open_mode, response, attempted_urls
```

即：**`open_mode`、`downloaded_size`、`total_size` 由同一组条件一次性算出，任何一种不满足“本地有偏移 + 有校验子 + 响应是 206 + 起点对得上”的组合，都会一致地走向“`wb` 截断 + `downloaded_size` 归零 + `total_size` 取（重新请求后的）`Content-Length`”，不存在“判成截断但计数器没归零”这种组合，也不存在“判成截断但仍在用一个不可信 206 的响应体/响应头”这种组合。** 这是一个可以在单测里逐一构造反例、覆盖每个分支的不变量，不是“我们记得住状态”这种不可验证的承诺；且这条不变量必须用**最终文件的字节**去验证，不能只验证 `open_mode` 和计数器——一个自洽但错误的计数器同样能通过“数值匹配”的检查，只有比对写到磁盘上的实际字节才能揭穿它。

- 校验子的刷新只看**这次响应是不是完整正文**（`status_code == 200`），而不是看“这次请求有没有带 Range”：无论是从未续传过的首次下载，还是带着 Range 但被服务端判定失配、回落成 200 的续传请求，只要拿到的是 200，就意味着这是当下这份文件内容的最新校验子，必须覆盖写入，否则下一次暂停/续传会拿着一份对不上的旧校验子，只能反复触发全量重下。
- 镜像轮换（坑 8）：`request_download` 的镜像轮换逻辑不改，续传只是多带了 `Range`/`If-Range`/`Accept-Encoding`，这些头在每一个候选镜像上都会原样发送；是否可追加完全由 `plan_download_write` 里那组条件判定，不依赖“猜哪个镜像会命中”。

`download_file` 用 `plan_download_write` 的结果替换现有的“直接取 `Content-Length` 再以 `wb` 打开”那几行，其余（分块写入、chunk 大小按 `total_size` 分档、`refresh_download_progress`）不变。

### (c) 批次的三种终态（完成/暂停/取消）由谁判定、在哪个线程判定

批次一共有两处可能需要判定“到这里就算终结了”：解析阶段和下载阶段，两处都只在各自唯一的判定点上处理一次，不存在竞争。

**解析阶段**：`collect_parsed_resources` 的循环体每次迭代前检查 `control.cancel_event.is_set()`，命中就提前结束。解析线程结束后把结果通过 `ui_call` 交给主线程的 `start_downloads` 回调；这个回调**首先**检查 `control.cancel_event.is_set()`——命中就直接按“取消”收尾（不弹任何对话框、不打开目录/保存对话框、不创建 `download_states`），调用 `set_ui_phase("idle")` 并把 `_batch_control` 置回 `None`。这就是“取消覆盖解析阶段”这条硬要求的落点：`control` 从 `download()` 一开始就存在，解析阶段自然拿得到它。

**下载阶段**：由**批次工作线程自己**（`start_download_batch`/续传复用的 `_run_batch_worker` 里，`with ThreadPoolExecutor(...) as executor: ...` 代码块**退出之后**）判定，判定时机是所有提交的 `future.result()` 都已经返回（即所有工作线程函数调用都已经返回，不代表文件本身处理完）：

```python
def _run_batch_worker(states_to_run, control, all_states):
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(download_file, state["download_url"], state["save_path"], state["chapters"], state) for state in states_to_run]
        for future in futures:
            future.result()

    if control.cancel_event.is_set():
        outcome = "cancelled"
    elif control.pause_event.is_set():
        outcome = "paused"
    else:
        outcome = "completed"
    ui_call(handle_batch_outcome, outcome, all_states, control)
```

`handle_batch_outcome`（主线程）按结局收尾：

- `"completed"`：现有 `finish_download_batch` 的行为原样保留（弹“下载完成”，展示失败清单），额外调用 `set_ui_phase("idle")`，把 `_batch_control` 置回 `None`。
- `"cancelled"`：不弹“下载完成”弹窗；对 `all_states` 里仍是 `finished == False` 的任务做兜底清理（正常情况下这些任务在 `download_file` 内部已经各自处理过，这里只是批次生命周期边界上的最后一道保险，不是给理论上不会发生的情况加复杂逻辑）；调用 `set_ui_phase("idle")`，把 `_batch_control` 置回 `None`。
- `"paused"`：把 `control.paused_settled` 置为 `True`，更新界面到“已暂停”对应的按钮/文案（见 (e)），**不**清空 `_batch_control`、**不**清空 `download_states`——两者都要留给“继续”使用。

`download_file` 内部对“单个文件为什么停下来”的分类，判据同样是读 `control.cancel_event`/`control.pause_event`（取消优先），并且明确规定每种情况下 `finished` 字段的终值：

- 成功 → `finished = True`（不变）。
- 失败 → `finished = True`（不变）。
- 排队中被取消 → 不发起网络请求，不产生 `.tmp`，`finished = True`。
- 在飞中被取消 → 中止写入，删除 `.tmp`，`downloaded_size`/`total_size` 清零，`finished = True`（取消不算失败，不写 `failed_reason`）。
- 排队中/在飞中被暂停 → 保留已写的 `.tmp`，不清零 `downloaded_size`，`finished` 保持 `False`——这是批次里唯一不终结的情况，也是让 `downloads_active()`（定义 `not all(finished)`，完全不用改）继续把“已暂停”识别为“批次仍然活跃”的关键。

也就是说，**一个批次只要不是“暂停”这个结局，离开批次时它名下所有任务的 `finished` 最终都会是 `True`**；只有“暂停”结局允许 `finished` 停留在 `False`，而这些 `False` 的任务只会存在于“已暂停”这一个界面状态里，会被“继续”重新提交，或者被“取消”在 (d) 描述的路径里清理并强制置为 `True`。

### (d) 暂停期间点取消（批次线程已退出）这条路径怎么收尾

关键在于区分“已经点了暂停”和“暂停已经生效”：

- 用户点“暂停”的瞬间，`pause_event` 立刻置位，但批次工作线程未必已经停下——它可能正在写最后几个 chunk，也可能还没轮到检查这个标志。这段窗口里，`control.paused_settled` 仍然是 `False`，批次线程**仍然存活**。
- 只有 (c) 里的 `_run_batch_worker` 真正跑完 `with ThreadPoolExecutor(...)` 块、确认所有 `future.result()` 都返回、并让 `handle_batch_outcome` 把 `paused_settled` 置为 `True` 之后，批次线程才算真正退出。

`cancel_current_batch` 用 `paused_settled` 而不是 `pause_event` 来判断“批次线程是否还活着”，从而把“暂停期间点取消”与“刚点完暂停就点取消”这两种情况分流到正确的路径：

```python
def cancel_current_batch() -> None:
    control = _batch_control
    if control is None:
        return
    control.cancel_event.set()
    close_active_responses(control)   # 若批次线程仍存活（不论是不是刚被要求暂停），促使它尽快停下

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
        set_ui_phase("idle")
        globals()["_batch_control"] = None
```

- 若此时 `paused_settled` 仍是 `False`（不管 `pause_event` 有没有置位），说明批次线程还活着：`cancel_event.set()` 加上主动断连之后，工作线程会在 (c) 描述的判定逻辑里自己发现 `cancel_event` 已置位（取消优先于暂停），走 `outcome == "cancelled"` 分支收尾，`cancel_current_batch` 本身不需要、也不会去动 `.tmp` 文件，不存在主线程和工作线程同时操作同一个文件的窗口。
- 若 `paused_settled` 为 `True`，说明批次线程已经不存在了：`cancel_current_batch` 自己删除仍未终结（`finished == False`）任务的 `.tmp`，把 `downloaded_size`/`total_size` 清零，并把 `finished` 强制置为 `True`——这一步是让 `downloads_active()` 之后正确回到“空闲”，避免退出程序时被误判为“任务未完成”。

两条路径共用同一个 `cancel_current_batch`，用 `paused_settled` 这一个只由主线程读写的标志就能区分，不需要额外引入“批次线程是否存活”的探测。

### (e) 底部两个按钮在四个状态下的文案与行为

不新增控件，复用现有的 `copy_btn`（左，`解析并复制`）与 `download_btn`（右，强调色，`下载`），只改 `text` / `command` / `state`：

| 阶段 | `copy_btn`（左） | `download_btn`（右，强调色） |
|---|---|---|
| 空闲 | “解析并复制”，启用，`command=parse_and_copy` | “下载”，启用，`command=download` |
| 解析中（点了“下载”触发的解析，非“解析并复制”自己的解析） | “解析并复制”，**禁用**（暂停无从谈起，且避免和下载共用同一次解析产生混淆）| “取消”，启用，`command=cancel_current_batch` |
| 下载中 | “暂停”，启用，`command=pause_current_batch` | “取消”，启用，`command=cancel_current_batch` |
| 已暂停 | “继续”，启用，`command=resume_current_batch` | “取消”，启用，`command=cancel_current_batch`（走 (d) 的同步收尾分支） |

统一收口成一个 `set_ui_phase(phase: str)` 辅助函数，负责一次性设置两个按钮的 `text`/`command`/`state`（以及是否清空进度条/文案），避免这四种状态的切换代码散落在多个函数里各写一遍（对应 AGENTS.md“确保代码可维护性”）。由于只改 `text`/`command`，不改 ttk style（`Accent.TButton` 等保持不变），浅色/深色主题不需要额外适配——`theme.py` 的主题应用是按 style 名生效的，与按钮当前文案无关，已确认。

`download_btn` 原有的 `width=9` 约束：新文案“取消”“暂停”“继续”“下载”都明显短于原来的“下载”“解析并复制”，不会溢出。

**所有离开批次的路径都会调用 `set_ui_phase("idle")` 把界面复位到空闲**，逐一列出：

1. 下载全部完成 → `handle_batch_outcome("completed", ...)`。
2. 下载中/解析中点取消，批次线程自行收尾 → `handle_batch_outcome("cancelled", ...)`。
3. 已暂停时点取消，批次线程已退出 → `cancel_current_batch` 的同步收尾分支（(d)）。
4. 解析阶段被取消 → `start_downloads` 检测到 `cancel_event` 已置位（(c)）。
5. 解析完成后，用户关闭了目录/保存路径对话框（不是取消，是正常放弃这次下载）→ 现有 `restore_download_btn` 分支同样改为调用 `set_ui_phase("idle")`，并把 `_batch_control` 置回 `None`（这次批次不会再继续，留着控制对象没有意义）。

第 2、3、4 三条路径都会让离开批次的任务最终 `finished == True`（见 (c) 的终值规定，(d) 里额外补一次强制置位），因此 `downloads_active()` 之后正确变回 `False`：`on_closing` 不会再误弹“下载任务未完成，是否退出？”，`show_parse_progress` 的进度标签也能正常复位。

### 其它需要一并做、但不属于上面五个结构性问题的改动

- **多文件提示写明数量**：`download()` 里 `messagebox.showinfo("提示", "您将下载多个文件，...")` 改为把 `len(resources_info_list)` 插进文案，例如“您将下载 {N} 个文件，请选择要下载文件的位置。”其余文案不变。
- **`BatchControl` 的创建时机**：在 `download()` 入口创建（此时还不知道保存目录），赋给 `_batch_control`；用户选定目录/保存路径后，把它写入 `control.directory`，供最终“完成”弹窗展示相对路径使用。
- **控制对象通过状态字典传给工作线程，不新增 `download_file` 的参数**：`download_file(url, save_path, chapters, current_state)` 的四个位置参数保持不变（`tests/test_download_batch.py:62` 打的桩 `def download(url, path, chapters, state)` 只接受四个位置参数，新增第五个参数会让它 `TypeError`）。批次在创建/提交每个状态字典时多写一个 `state["control"] = control`，`download_file` 内部用 `current_state.get("control")` 取用；单独调用 `download_file`（不经过批次）时这个键不存在，等价于没有取消/暂停能力，符合“不做单任务级别的暂停/取消”的边界。
- **`start_download_batch(targets, directory)` 签名保持不变**，向后兼容 `tests/test_download_batch.py` 现有用例；内部改为创建/复用模块级 `_batch_control` 并调用共享的 `_run_batch_worker`，调用方不需要感知控制对象的存在。“完成”这一结局的行为、文案与现有测试逐字节保持一致，不做修改。
- **`chapters` 挪进状态字典**：`create_download_state` 新增 `chapters` 字段，`start_download_batch` 提交任务时从 `state["chapters"]` 读取而不是从原始 `targets` 里的 `ResourceInfo` 读取；这样“继续”只需要拿着 `download_states` 里未完成的子集重新提交，不需要额外保留一份 `targets`/`ResourceInfo` 列表活到暂停之后。
- **`app.py` 不需要改动**：`on_closing` 现有判断 `not all(state["finished"] for state in download_panel.download_states)` 已经天然覆盖“已暂停”状态（暂停的任务 `finished` 就是 `False`，见 (c)），也天然覆盖取消之后的状态（(c)(d) 规定取消收尾后 `finished` 一律为 `True`），关闭确认对话框的行为无需调整；进程退出时残留的 `.tmp` 本来就允许作废，符合“不做”边界，不需要额外的退出清理逻辑。

## Assumptions & Open Questions

以下每一条都给出默认倾向与理由，均按默认倾向直接实施，不再等待答复。

1. **已暂停批次里混有“真失败”的文件时，“继续”是否重试这些失败文件？** 默认：不重试，只续传因暂停而中断（`finished == False`）的文件；真正失败的文件 `finished` 已经是 `True`（现有失败路径行为），自然被排除在续传集合之外，最终仍在完成弹窗里报告失败。理由：issue 没有提出失败重试需求，重试策略是独立话题，混进本次改动会扩大 scope。
2. **解析阶段被取消，是否仍要弹出“以下行无法解析”的警告？** 默认：不弹，直接静默回到空闲界面。理由：用户已经主动中止，此时展示部分失败信息只会造成困惑，且与“取消”作为“逃生口”的定位（越干脆越好）一致。
3. **已暂停状态下的进度文案格式？** 默认：复用 `refresh_download_progress` 现有的“已下载 字节/总字节 (百分比%) 已完成 x/y”文本，前面加“已暂停 ”前缀，不新造格式。理由：复用现成的汇总计算，减少新代码，且用户已经熟悉这套数字的含义。
4. **暂停/取消按钮点击是否需要二次确认？** 默认：不需要，两者都是立即生效的“逃生口”动作。理由：issue 把现状定性为“缺一个逃生口”，如果取消/暂停还要弹确认框，等于没有解决“误点无法挽回”的核心痛点；下载中点“取消”本来就不会比现在的“关程序”更危险。
5. **解析阶段取消的生效延迟受限于当前这一条正在进行的解析请求（最坏情况到 `REQUEST_TIMEOUT` 的 60 秒），是否需要强制中断这一条在飞的解析请求？** 默认：不做，接受“至多晚于一条解析请求”的延迟。理由：解析请求本身很快（navigator 实测中位数约 0.18 秒），强制中断需要深入改动 `api.parse`/`network.session` 的请求发起方式，属于与本需求无关的重构，且收益（缩短一个几乎不可感知的尾延迟）远小于风险。
6. **续传时校验子只认 `ETag`/`Last-Modified`，不区分强/弱 ETag（`W/"..."`）？** 默认：原样透传 `ETag` 头的值，不做强弱校验子的甄别。理由：即使服务端把弱校验子当成不匹配从而多做一次全量重下，也只是牺牲一点效率，不会造成数据错误（截断重下分支本来就安全）；额外实现强弱区分的复杂度与收益不成正比。
7. **大批量暂停时可能同时存在多个几十 MB 的半截 `.tmp`，是否需要检查磁盘剩余空间？** 默认：不做。理由：现有的（无暂停/取消的）下载流程本来就不检查磁盘空间，这不是本次改动引入的新风险，加上去会超出这次的最小修复范围。
8. **主动断连（`response.close()`）之后，工作线程在 `iter_content` 里具体会抛出哪种异常，是否需要精确捕获？** 默认：不精确区分异常类型，沿用现有的 `except Exception as e:` 兜底结构，在该分支最前面先判断 `control` 的两个事件标志（取消优先于暂停）来分类“暂停/取消/真失败”，能命中就不生成 `failed_reason`、不落痕迹为失败。理由：不同 `requests`/`urllib3` 版本在连接被外部关闭时抛出的具体异常类型不保证一致，按事件标志分类比按异常类型分类更稳定，也更贴合“协作式检查 + 主动断连双保险”的设计初衷——断连只是用来缩短最坏情况下的等待时间，不是分类的依据。
9. **按钮命令函数放在哪个模块？** 默认：全部放进 `download_panel.py`（`cancel_current_batch`/`pause_current_batch`/`resume_current_batch`/`set_ui_phase`），不新增文件。理由：现有的取消/暂停/继续所需要的一切状态（`download_states`、`_batch_control`、六个受 `bind_widgets` 管理的控件句柄）都已经在这个模块里，符合模块现状的职责边界（模块顶部注释已声明“本模块持有与下载相关的几个控件句柄”）。
