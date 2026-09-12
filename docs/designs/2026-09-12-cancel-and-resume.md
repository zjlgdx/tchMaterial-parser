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

“用什么模式打开文件”“`downloaded_size` 从哪起算”“`total_size` 取哪个响应头”“要不要刷新校验子”是同一个决定的四个输出。这里有两条容易漏掉的分界线，必须显式说清楚：

1. **响应是不是 206，和响应能不能被当整份正文使用，是两件不同的事**——一个不可用的 206（起点不匹配、或 `Content-Range` 解析不出来）不能落回“当成普通响应，`wb` 截断、`total_size` 取这次的 `Content-Length`”，因为它的响应体只是被请求的那一段，不是完整正文；把它的 `Content-Length` 当成整份文件的长度、把它的 body 当成整份文件的内容写下去，会产出一个大小和计数器都自洽、内容却是错的文件，且不会触发任何失败提示。**判定为“不可用”的 206 必须和 416 走同一条路：关闭这次响应，重新发一次不带 Range 的全新请求，把新响应当作真正的完整正文来源。**“能不能当整份正文用”这条判据必须对**每一次**响应都成立——首次请求、重试之后的响应、没带 Range 的普通下载——不能只在带 Range 的那一次上收紧；而且判据必须是 `status_code == 200`，不是 `response.ok`（`< 400`），否则 204/304 这类“ok 但没有正文”的响应会产出一个零字节文件却判成功。只有“范围本身有问题”（416，或回了 206 但接不上）才值得不带 Range 重来一次；真正的失败（404/500 等，与 Range 无关）重来一次大概率还是失败，直接判定不可信、交给调用方走失败分支，不做这次多余的尝试，重来之后也不再加第二层重试。
2. **判断出来的 `open_mode`/`downloaded_size`/`total_size`/校验子，必须在“真正确定要写”之后才应用到 `current_state` 上，不能在判断出来的那一刻就写回去。** 从拿到响应、到真正打开文件写入之间，隔着“登记响应供主线程主动断连”“检查是否已经被要求暂停/取消”两步——如果这中间提前把 `current_state` 改成了这次判断出来的新值（尤其是校验子），一旦紧接着命中暂停就此退出，磁盘上的 `.tmp` 还是旧内容，`current_state` 却已经指向新内容，两者不再同源；下一次“继续”会带着这份还没被磁盘内容证实过的新校验子发起续传，服务端一旦认可，就会把新内容接在旧字节后面。正确的顺序是：先把这次的判断结果作为**返回值**带出来，调用方确认真的要写（也就是文件已经用 `open_mode` 打开成功——“wb” 这一步本身就是真正的截断动作）之后，再把这些返回值写回 `current_state`。

```python
def parse_content_range(header_value: str | None) -> tuple[int, int, int] | None:
    """解析 `Content-Range: bytes start-end/total`；缺失或格式不对时返回 None，
    调用方一律按不可续传处理（对应坑 9）。"""
    ...

def response_usability(response, offset, can_attempt_range):
    """判定这次响应能不能当正文用，对首次请求、重试请求、有没有带 Range 都一视同仁。
    返回 ("resumed", content_range) / ("full", None) / (None, None)。"""
    if can_attempt_range and response.status_code == 206:
        content_range = parse_content_range(response.headers.get("Content-Range"))
        if content_range is not None and content_range[0] == offset:
            return "resumed", content_range
    if response.status_code == 200:  # 不是 response.ok；204/304 这类不算数
        return "full", None
    return None, None

def plan_download_write(current_state: dict, temp_path: str, url: str):
    """只返回“计划”，不直接改 current_state；open_mode 为 None 表示这次响应不可信。"""
    offset = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
    can_attempt_range = offset > 0 and bool(current_state["validator"])

    if can_attempt_range:
        response, attempted_urls = request_download(url, range_from=offset, validator=current_state["validator"])
    else:
        response, attempted_urls = request_download(url)

    kind, content_range = response_usability(response, offset, can_attempt_range)

    # 只有“范围本身有问题”才值得不带 Range 重来一次；真正的失败直接走下面的失败分支。
    range_itself_is_the_problem = can_attempt_range and (response.status_code == 416 or (response.status_code == 206 and kind is None))
    if range_itself_is_the_problem:
        response.close()
        offset = 0
        can_attempt_range = False
        response, attempted_urls = request_download(url)
        kind, content_range = response_usability(response, offset, can_attempt_range)

    if kind is None:  # 不可信：交给调用方走失败清理，不去碰它未必存在的响应头
        return None, 0, 0, None, response, attempted_urls

    if kind == "resumed":
        return "ab", offset, content_range[2], None, response, attempted_urls

    validator = response.headers.get("ETag") or response.headers.get("Last-Modified")  # 对应坑 7
    return "wb", 0, int(response.headers.get("Content-Length", 0)), validator, response, attempted_urls
```

`download_file` 侧的用法：

```python
open_mode, planned_downloaded_size, planned_total_size, planned_validator, response, attempted_urls = plan_download_write(current_state, temp_path, url)
# 登记响应、检查 stop_reason() ——命中暂停/取消就此退出，不touch current_state，见 (c)
...
else:
    with open(temp_path, open_mode) as file:  # "wb" 在这一刻真正截断
        current_state["downloaded_size"] = planned_downloaded_size
        current_state["total_size"] = planned_total_size
        if open_mode == "wb":  # 全新正文：无条件覆盖，哪怕这次响应没给校验子也要覆盖成 None
            current_state["validator"] = planned_validator
        # open_mode == "ab"：不动校验子，沿用旧值——planned_validator 在这条分支上恒为
        # None，但那只表示“不归它管”，不是“应当清空”，不能用同一个 None 兼职表达两种语义。
        for chunk in response.iter_content(...):
            ...
```

即：**`open_mode`、`downloaded_size`、`total_size` 由同一组条件一次性算出，任何一种不满足“本地有偏移 + 有校验子 + 响应是 206 + 起点对得上”的组合，都会一致地走向“`wb` 截断 + `downloaded_size` 归零 + `total_size` 取（重新请求后的）`Content-Length`”，不存在“判成截断但计数器没归零”这种组合，也不存在“判成截断但仍在用一个不可信 206 的响应体/响应头”这种组合；而且这四个值只有在真正打开文件写入的那一刻才会出现在 `current_state` 上，不存在“判断已经算出新值、但磁盘还是旧内容”的中间态。** 这是一个可以在单测里逐一构造反例、覆盖每个分支的不变量，不是“我们记得住状态”这种不可验证的承诺；且这条不变量必须用**最终文件的字节**去验证，不能只验证 `open_mode` 和计数器——一个自洽但错误的计数器同样能通过“数值匹配”的检查，只有比对写到磁盘上的实际字节才能揭穿它；校验子是否被提前泄漏，也必须用“暂停一轮、再继续一轮，两轮之间校验子有没有变”这种跨轮次的测试才能揭穿，单轮测试看不出来。

- 校验子的刷新只看**这次响应是不是完整正文**（`status_code == 200`），而不是看“这次请求有没有带 Range”：无论是从未续传过的首次下载，还是带着 Range 但被服务端判定失配、回落成 200 的续传请求，只要拿到的是 200，就意味着这是当下这份文件内容的最新校验子，必须覆盖写入，否则下一次暂停/续传会拿着一份对不上的旧校验子，只能反复触发全量重下。**“覆盖”与“保留”必须靠 `open_mode` 分辨，不能都用 `planned_validator is not None` 这一个判据去决定要不要写：** `plan_download_write` 对“206 续传”和“200 完整正文但服务端没给 ETag/Last-Modified”都会返回 `validator=None`，但前者的 `None` 意思是“不归它管、保留原值”，后者的 `None` 意思是“这次正文确实没有校验子，应当清空”——两种语义一旦被压进同一个 `None` 再用“是不是 None”去判断该不该写，后者会被误判成前者，跳过覆盖，磁盘上已经换成了新正文，内存里却还留着旧版本的校验子。正确的判据是 `open_mode == "wb"` 就无条件覆盖（哪怕覆盖成 `None`），`open_mode == "ab"` 就完全不碰这个字段。
- **穷举自查：把每一个会碰 `.tmp` 的写入/删除路径列全，而不是只列“下载循环的结局”。** 早先的版本按“下载循环的结局”（成功/失败/取消/暂停）分行，漏了循环*之外*也会碰 `.tmp` 的路径——`add_bookmarks` 就是这样一条路径，也是坑 4 藏身的地方；而且循环结局那张表没有任何一行是“暂停”，但只有 `finished` 停留在 `False` 的那几行才会被下一轮真的读回去拼请求，其余结局都终结这个任务（`finished = True`，不会被“继续”重新提交），所以“循环的结局”本来就不是这张表该按的维度。正确的维度是“谁碰过 `.tmp`” × “碰完之后 `finished` 是不是 `False`”：

  | 写入/删除 `.tmp` 的路径 | 磁盘上的 `.tmp` | `validator` | `finished` | 下一轮会不会读到 |
  | --- | --- | --- | --- | --- |
  | 响应到手后、真正打开文件前就早退暂停（坑 2） | 不碰，还是进入本轮之前的旧内容 | 不碰，还是进入本轮之前的旧值 | `False` | **会**——但没有任何新东西被写过，这一行的自洽性单纯来自“沿用了上一轮本就自洽的状态” |
  | `"ab"` 续传写入循环中途被暂停 | 旧前缀 + 本次已写入的追加字节，全部同属校验子标识的那个版本 | 不碰，保留旧值 | `False` | **会**——校验子从未失效过，已写入的追加字节经服务端用 If-Range 确认过与它同源 |
  | `"wb"` 全新正文写入循环中途被暂停 | 本次响应体已写入的前缀（`wb` 已在 `open()` 时截断旧内容） | 已在 `open()` 那一刻覆盖成这次响应给出的新值（或 `None`） | `False` | **会**——已写入的字节全部来自这次响应，与刚覆盖的新校验子同源 |
  | `"wb"`/`"ab"` 写入循环中途被取消，或下载不完整 | `discard_temp_and_zero_counters()` 尽力删除（`os.remove` 包在 `except Exception: pass` 里，可能删不掉，例如 Windows 文件被占用） | 不碰 | `True` | **不会**——真正的保证不是“`.tmp` 一定被删掉了”，而是 `finished = True` 使这个 `state` 不会被“继续”复用；即便 `.tmp` 残留在磁盘上，也没有代码路径会再拿它和这个已经终结的 `state` 配对 |
  | 响应不可信（`open_mode is None`） | 同上，尽力删除 | 不碰 | `True` | **不会**——理由同上一行 |
  | 加书签阶段重写 `.tmp`（P1-1，本轮新补的行——这条路径不在“下载循环”之内） | `add_bookmarks` 把 `.tmp` 整份重写，字节内容、长度都变了，不再是服务端正文的前缀 | 不碰 | 视 `os.replace` 是否成功而定：成功则整个 `.tmp` 都不存在了（见下一行）；失败且此时命中暂停，本轮已改为不再落成“暂停”，而是判定失败并 `discard_temp_and_zero_counters()`，`finished = True` | 成功：不适用（`.tmp` 已经变成 `save_path`）；失败：**不会**——`finalizing` 标记堵住了“暂停”这条出路，不会再产生“`.tmp` 是书签版、但 `validator`/`finished=False` 仍描述服务端原始正文”这种活的、会被读回的不一致状态 |
  | `os.replace` 成功（收尾） | `.tmp` 不再存在，已改名为 `save_path` | 已在 `open()` 时覆盖/保留成正确值 | `True` | **不会**——`.tmp` 本身已经不存在，`offset` 下一次会算成 0 |
  | 批次层面兜底清理（`handle_batch_outcome` 对非“暂停”结局遍历未完成任务；(d) 里暂停期间点取消的收尾） | 尽力删除（同上，可能删不掉） | 不碰 | `True` | **不会**——理由同“中途被取消”那一行 |

  表里真正“活着”、会被下一轮实际读回去拼 Range 请求的只有前三行（`finished` 仍是 `False` 的那些），而这三行里 `.tmp` 的字节与 `validator`/`downloaded_size` 全部同源；`finished = True` 的那些行里，`.tmp` 是否被成功删除只是"尽力而为"，从不是正确性的保证来源，因为不管有没有删成，这个已经终结的 `state` 都不会再被拿来发起下一次请求——它也不会被将来某次全新的下载复用：全新下载走 `create_download_state`，`validator` 永远从 `None` 起算，与这个终结掉的旧 `state` 毫无关系。
- 镜像轮换（坑 8）：`request_download` 的镜像轮换逻辑不改，续传只是多带了 `Range`/`If-Range`/`Accept-Encoding`，这些头在每一个候选镜像上都会原样发送；是否可追加完全由 `plan_download_write` 里那组条件判定，不依赖“猜哪个镜像会命中”。

`download_file` 用 `plan_download_write` 的结果替换现有的“直接取 `Content-Length` 再以 `wb` 打开”那几行，其余（分块写入、chunk 大小按 `total_size` 分档、`refresh_download_progress`）不变。

### (c) 批次的三种终态（完成/暂停/取消）由谁判定、在哪个线程判定

批次一共有两处可能需要判定“到这里就算终结了”：解析阶段和下载阶段，两处都只在各自唯一的判定点上处理一次，不存在竞争。

**解析阶段**：`collect_parsed_resources` 的循环体每次迭代前检查 `control.cancel_event.is_set()`，命中就提前结束。解析线程结束后把结果通过 `ui_call` 交给主线程的 `start_downloads` 回调；这个回调**首先**检查 `control.cancel_event.is_set()`——命中就直接按“取消”收尾（不弹任何对话框、不打开目录/保存对话框、不创建 `download_states`），调用 `set_ui_phase("idle")` 并把 `_batch_control` 置回 `None`。这就是“取消覆盖解析阶段”这条硬要求的落点：`control` 从 `download()` 一开始就存在，解析阶段自然拿得到它。

**下载阶段**：由**批次工作线程自己**（`start_download_batch`/续传复用的 `_run_batch_worker` 里，`with ThreadPoolExecutor(...) as executor: ...` 代码块**退出之后**）判定，判定时机是所有提交的 `future.result()` 都已经返回（即所有工作线程函数调用都已经返回，不代表文件本身处理完）：

```python
def _run_batch_worker(states_to_run, control):
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(download_file, state["download_url"], state["save_path"], state["chapters"], state) for state in states_to_run]
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

def handle_batch_outcome(outcome, control):
    if control.cancel_event.is_set(): # outcome 算出之后到这次回调真正执行之前，取消随时可能追上来；
        outcome = "cancelled"         # 取消一旦置位，不允许再落成 paused/completed，入口重判一次优先级
    ...
```

`states_to_run` 只是这一轮（初次或“继续”）实际提交给线程池的子集；批次真正的全量任务列表跟着模块级 `download_states` 走（下载面板本来就用它汇总/展示整批进度），`handle_batch_outcome` 直接读这个模块级变量，不需要单独传一份 `all_states` 进来。

`handle_batch_outcome`（主线程）**入口先按 `control.cancel_event` 重判一次 `outcome`**，再按结局收尾：`_run_batch_worker` 算出 `outcome` 到这次回调真正在主线程执行之间隔着一次 `ui_call` 调度，这段间隙里取消随时可能追上来；一旦追上，不管 `_run_batch_worker` 当初算出的是 `"paused"` 还是 `"completed"`，都要改判为 `"cancelled"`，不允许把一次实际上已经被取消的批次收尾成暂停或完成。

- `"completed"`：现有 `finish_download_batch` 的行为原样保留（弹“下载完成”，展示失败清单），额外调用 `set_ui_phase("idle")`，把 `_batch_control` 置回 `None`。
- `"cancelled"`：不弹“下载完成”弹窗；对 `download_states` 里仍是 `finished == False` 的任务做兜底清理（正常情况下这些任务在 `download_file` 内部已经各自处理过，这里只是批次生命周期边界上的最后一道保险，不是给理论上不会发生的情况加复杂逻辑）；调用 `set_ui_phase("idle")`，把 `_batch_control` 置回 `None`。
- `"paused"`：把 `control.paused_settled` 置为 `True`，更新界面到“已暂停”对应的按钮/文案（见 (e)），**不**清空 `_batch_control`、**不**清空 `download_states`——两者都要留给“继续”使用。

`download_file` 内部对“单个文件为什么停下来”的分类，判据同样是读 `control.cancel_event`/`control.pause_event`（取消优先），并且明确规定每种情况下 `finished` 字段的终值：

- 成功 → `finished = True`（不变）。
- 失败 → `finished = True`（不变）。
- 排队中被取消 → 不发起网络请求，`finished = True`；如果磁盘上留着上一轮暂停时写下的 `.tmp`（这一轮还没来得及发出请求就直接被取消判定截住），也一并删除、计数器一并归零——不能因为“这一轮没写过东西”就跳过清理，`.tmp` 是不是这一轮建的不影响它该不该被清掉。
- 在飞中被取消 → 中止写入，删除 `.tmp`，`downloaded_size`/`total_size` 清零，`finished = True`（取消不算失败，不写 `failed_reason`）。
- 排队中/在飞中被暂停 → 保留已写的 `.tmp`，不清零 `downloaded_size`，`finished` 保持 `False`——这是批次里唯一不终结的情况，也是让 `downloads_active()`（定义 `not all(finished)`，完全不用改）继续把“已暂停”识别为“批次仍然活跃”的关键。**例外一**：如果暂停恰好落在最后一块写完之后（`total_size > 0` 且 `downloaded_size == total_size`），说明文件其实已经下完，只是还没来得及走到成功分支，这种情况按“完成”处理（改名、`finished = True`），不留着一个内容已经齐全的 `.tmp` 装作还在暂停。**例外二（P1-1）**：一旦确认传输已经完整、进入“加书签（`add_bookmarks`）+ 改名（`os.replace`）”这个收尾阶段（用一个局部的 `finalizing` 标记表示），这个阶段发生的暂停不再落成“暂停”——`add_bookmarks` 会把 `.tmp` 整份重写（字节内容、长度都变了，不再是服务端正文的前缀），如果这时 `os.replace` 恰好失败（例如 Windows 上目标文件被阅读器/杀软占用）而用户又恰好在加书签的这几秒里点了暂停，若仍按“暂停”处理，就会把这份已经不是服务端正文前缀的 `.tmp` 留在磁盘上、却让 `validator`/`downloaded_size`/`total_size` 继续描述服务端原始正文——下一轮“继续”会用书签版 `.tmp` 的实际长度当偏移、配上原始正文的校验子发起 Range 请求，服务端一旦认可就会把原始正文的尾巴接到书签版前缀后面。`finalizing` 期间命中暂停一律判定为失败并清理 `.tmp`，逼下一次发起一次全新的下载，不允许"续传"这条路。

判定“为什么停下来”还有两条容易漏掉的时序规则，必须显式遵守：

- **P0-2（先判 `stop_reason()`，再判响应可不可用）**：请求返回之后，必须先检查 `stop_reason()`，命中暂停/取消就直接按暂停/取消收尾，根本不去看这次响应是不是可信——网络请求在飞的这段时间里随时可能被暂停/取消，此时响应内容是什么已经不重要，把它当失败处理（写 `failed_reason`）是错的，会把一次正常的暂停/取消误报成下载失败。
- **P0-3（循环退出后重新读一次 `stop_reason()`，不沿用循环内最后一次的值）**：分块写入循环里每写完一块都会检查一次 `stop_reason()`，命中就 `break`；但循环退出后判定“这个文件最终算什么结局”时，必须**重新调用一次** `stop_reason()`，不能直接复用循环内触发 `break` 那次的返回值——两者之间可能存在流干净结束（EOF）与暂停/取消几乎同时发生的窗口，只有重新读一次才能配合上面“暂停恰好落在最后一块之后”的例外做出正确判断。

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
