# 全量加固与结构重整（Hardening and Restructure）

**Date:** 2026-09-11

## Context

本仓库是一个 Tkinter 单文件桌面程序 `src/tchMaterial-parser.pyw`（735 行），用于从「国家中小学智慧教育平台」下载电子课本 PDF。一次完整代码审查列出了 A（致命）/ B（并发与数据正确性）/ C（健壮性与可维护性）三类共 17 条缺陷，对应 21 项交付任务，要求在同一分支 `fix/hardening-and-restructure` 上分三个阶段交付。

基线提交：`f3002017fc54018ffc91903b608caa4ef6084c11`。

本文档取代仓库根目录的 `重构设计方案.md`（该文档提出 Pydantic v1 写法、asyncio + Tkinter 混用、插件化架构、云端同步，且完全没有覆盖 A1/A2/A3 三条致命问题；实施时将其删除，不保留两份并存的设计稿）。

设计的三条底线：

1. **不要过度设计**。明确排除：`aiohttp` / `asyncio` 重写、CustomTkinter / PyQt、Pydantic、Poetry、loguru、插件化架构、云端同步、多设备同步。并发用标准库 `threading` / `concurrent.futures`，UI 用 `tkinter` / `ttk`，配置用 `dataclass`，日志用标准库 `logging`。
2. **运行时依赖白名单**：`requests`、`pyperclip`，以及仅 Windows 的 `pywin32`。本设计**没有引入任何白名单之外的运行时依赖**；测试期依赖只有 `pytest` 与 `flake8`。
3. **功能不回退**：URL 批量解析、解析并复制链接、层级选择插入 URL、Token 设置与帮助、高 DPI 适配、窗口图标、关闭时的未完成下载确认，全部保留。

## Discussion

### 逐条核实结果

对照当前工作树（基线提交，文件 735 行）逐条核实了 issue 中的每个 `file:line`。**除非另行说明，行号均指 `src/tchMaterial-parser.pyw`。**结论先行：每条缺陷本身都成立，没有误报；但相当一部分行号有漂移，下面的实施以本表的行号为准。

| 编号 | issue 标注 | 核实结论 | 漂移 |
| --- | --- | --- | --- |
| A1 | `:13` 顶层 `import pyperclip` | 成立。`:13` 是合并导入行 `import base64, tempfile, pyperclip`。`requirements.txt` 只有 `psutil==6.1.0` / `pywin32==308` / `requests==2.32.3`，既缺 `pyperclip`，`pywin32` 也无 environment marker | 无 |
| A2 | `:149`、`:390`、`:423` | 成立，且**已穷尽**。用 3.13 的 `ast` 遍历全部 `JoinedStr` 节点做了扫描，PEP 701 依赖恰好只有这 3 行：`:149`（既有嵌套同类引号，又有表达式内反斜杠 `"\n".join`）、`:390`、`:423`。本机 3.11.2 实测报错点确为 `:149` | 补充：`:22` 的返回注解 `tuple[str, str, str] \| tuple[None, None, None]` 在运行时求值，要求 3.10+；这会约束版本下限的选择 |
| A3 | `:517` 早于 `:523`；`:366-375` 串行下载 | 成立。`:517` `fetch_resource_list()`、`:523` `tk.Tk()` | 串行拉取四个列表文件实际在 `:370-372`（`for url in list_data: book_resp = session.get(url)`）；`:366` 只是取 `data_version.json`，区间标注偏大 |
| A4 | `:520` `messagebox` 早于 `:523` `tk.Tk()` | 成立 | 无 |
| B1 | `:128-131`、`:141-150` 子线程碰 Tk | 成立 | `progress_label.config()` 在 `:133`，不在 `:128-131` 内；完成块实际是 `:142-151`（`all(finished)` 判断 `:142`，两个 `messagebox` 在 `:149` / `:151`），不是 `:141-150` |
| B2 | `:102-104` append、`:141` 遍历 | 成立，`download_states` 全程无锁 | `all(finished)` 在 `:142` 而非 `:141` |
| B3 | `:218-219` 提前解禁、`:182` 清空状态 | 成立 | 无 |
| B4 | `:203-205` 文件名未清洗 | 成立。`title` 原样拼进 `os.path.join` | 无 |
| B5 | `:645`、`:686-690` 线性反查 | 成立 | 同一模式共 **6 处**：`:638`、`:644`、`:660`、`:684`、`:687`、`:690`。`:645` 本身是 `current_hier = current_hier[current_id]["children"]`，不是反查 |
| B6 | `:65-70`、`:82`、`:106`、`:361-408` 无 timeout | 成立。全文件 11 处 `session.get`（`:65`、`:68`、`:70`、`:82`、`:106`、`:361`、`:366`、`:371`、`:398`、`:403`、`:408`）**无一**带 `timeout` | 无 |
| B7 | `:215` 每链接一线程；`:434-438` daemon 被注释；`:561-568` psutil；`:571` `sys.exit(0)` | 全部成立。`# t.daemon = True` 确在 `:437` | 无 |
| C1 | `:23` try、`:97-98` 裸 except | 成立 | `except Exception:` 在 `:98`，`return None, None, None` 在 `:99`，区间应为 `:98-99` |
| C2 | `:381`、`:516`、`:661`、`:691` | 全部成立。`:516-519` 外层 try 把任何异常吞成 `{}` | `parsed_hier[book["tag_paths"][0].split("/")[1]]` 实际在 `:379`（`:381` 是 `continue`） |
| C3 | `:118-139` 直接写目标文件 | 成立。`:119` `open(save_path, "wb")`，失败分支 `:137-140` 不删除残件 | 无 |
| C4 | `:65-70` 对比 `:106` | 成立。全文件只在 `:447` 设过 `session.proxies`，**从未**设置 `session.headers`，`headers` 仅在 `:106` 显式传参 | 无 |
| C5 | `:507-508` macOS 撒谎 | 成立。`:507` `else:` / `:508` 直接返回「已保存！」。另外 `:509-510` 的 `except` 也返回「已保存！」，写失败同样撒谎；Linux 写入在 `:503`，无 `chmod` | issue 只点了 `:507-508`，`:509-510` 是同一性质的第二处，一并修 |
| C6 | `selection_handler :632-700`；硬编码 8 在 `:625`、`:712`；字体 `:230`、`:236`；图标 `:527`、`:547` | 缺陷全部成立，行号多处漂移 | `selection_handler` 实际是 `:617-695`，`trace_add` 绑定在 `:697-698`；硬编码 8 的位置是 `:611`、`:613`、`:697`、`:707`，而 `:625` / `:712` 用的是 `len(drops)` 和循环变量；「微软雅黑」共 **6 处**：`:238`、`:242`、`:319`、`:579`、`:589`、`:592`，不是 `:230` / `:236`；base64 图标在 `:546`（`:527` 是 `win32print` 那行），固定临时文件名在 `:547` 与 `:550`。「URL 拼接复制 4 遍」属实（`:663`、`:665`、`:693`、`:695`）；无 `if __name__ == "__main__"` 属实 |
| C7 | `fetch_lesson_list :396-430`、自注 `:433`、标题 `:541`、版本号 4 处 | 缺陷成立 | `fetch_lesson_list` 实际是 `:396-427`，作者自注在 `:431`；窗口标题在 `:542`；**`tchMaterial-parser.spec` 并未硬编码 `3.1`**，它写的是 `version='version.txt'`，真正的字面量在源码注释 `:2`、标题 `:542`、`version.txt`（`filevers` / `prodvers` / `FileVersion` / `ProductVersion` 共 4 处）以及 `README.md`（3 处 v3.1 文案）。README 的「支持暂停/恢复操作」与「发布 Windows 与 Linux 产物」承诺属实，CI 只在 `windows-latest` 上跑 flake8 属实 |

### 由核实引出的额外结论

- **A2 的版本下限选 3.10，不抬到 3.12。** 三处 PEP 701 写法改写起来是纯机械替换（把内层双引号换成单引号、把 `"\n".join(...)` 提到 f-string 外），代价几乎为零；而抬到 3.12 会把 AUR / 旧发行版用户挡在门外，也会让本机 3.11.2 无法跑测试（与验收标准冲突）。下限定为 **3.10**，因为 `:22` 的 `X | Y` 注解在运行时求值，需要 3.10；用 `from __future__ import annotations` 下探到 3.9 属于为了一个数字增加心智负担，不做。CI 矩阵覆盖 3.10 / 3.11 / 3.12 / 3.13。
- **B1 的最终形态不是 `root.after(0, ...)` 而是 UI 轮询。** 当前代码每下载一个 128 KB 分块就更新一次界面；照搬成 `root.after(0, ...)` 会在大文件批量下载时向 Tk 事件队列灌入每秒上千个回调，把主线程压垮。最终形态是：工作线程只在锁内更新纯数据的下载状态，UI 用一个 200 ms 的 `root.after` 轮询器读取快照并刷新进度条/标签/完成提示。这同样满足「子线程绝不碰 Tkinter」，且开销恒定。阶段一的止血版本仍按 issue 任务 5 用 `root.after(0, ...)`，阶段二替换为轮询器。
- **按钮的禁用与恢复由不同角色负责。** 轮询有 200 ms 的延迟，若按钮状态完全由轮询器派生，用户在一个周期内双击会让同一批链接被解析两遍、投递两遍。因此职责这样切：**点击处理函数在主线程内同步置灰**（早于任何解析和提交动作），**轮询器只负责在 `in_flight` 归零后恢复**。B3 要根治的是「还有任务在飞时被提前解禁」，恢复权收归轮询器一处正是它的解法，同步禁用不与之冲突。
- **B7 的守护线程在阶段三会失效，需要显式取消机制，且退出延迟有一个可计算的上界。** `concurrent.futures.ThreadPoolExecutor` 的工作线程在 3.9+ 是非守护线程，并由解释器退出钩子 join，单靠 `daemon = True` 已经不适用。阶段三改用「`threading.Event` 取消标志 + `executor.shutdown(wait=False, cancel_futures=True)`」，**目录线程池与下载线程池各持一个标志，关窗时一并置位**。需要说清楚这套机制的真实边界：`shutdown(cancel_futures=True)` 只能撤掉尚未开始的任务，已在执行的任务撤不掉；取消标志也只有在当前阻塞调用返回之后才会被检查到。所以对一个正卡在连接或读取上的线程，退出延迟的上界是**读取超时，即 30 s**（连接阶段则是 10 s），而不是一个分块的时间。这个上界可以接受：进程不会永久残留（旧代码的非守护线程是无上界的），用户已经在关窗确认框里确认过要中止，且 30 s 之后进程自行结束不需要任何用户干预；而更激进的手段在 Python 里没有安全做法——没有中断任意线程的公开 API，`ctypes` 强制抛异常会让线程停在任意位置，`.part` 文件和锁的状态都无法保证一致。阶段一仍按任务 8 打开 `t.daemon = True`（那时还是裸 `threading.Thread`）。
- **C5 的第二处谎言。** `set_access_token` 的 `except Exception` 分支（`:509-510`）也返回「已保存！」，写盘失败时用户同样被骗。新实现里失败必须返回真实错误文案。
- **`fetch_lesson_list` 选择删除而非修好**（任务 17 的二选一）。理由：(a) 它从未被启用，无用户依赖；(b) 它的 `list_resp.json()["urls"]` 当作 list 直接迭代，而课本那条同名字段是逗号分隔字符串（`:367` 有 `.split(",")`），两个接口契约不一致且无法在不打真实网络的前提下验证；(c) UI 只会拼 `basic.smartedu.cn/tchMaterial/detail` 形式的 URL，课件资源不适用该形式，修好了也接不上下游；(d) 启用它会让启动负载再翻一倍，与 A3 的目标直接冲突。删除是可从 git 历史恢复的无损操作。

## Approach

### 阶段划分

沿用 issue 的三阶段，每个任务单独 commit。阶段一**不动架构**，直接在 `src/tchMaterial-parser.pyw` 上打补丁，保证该 commit 单独 checkout 出来就是可用的；阶段二做拆包，阶段一的补丁随之迁入对应模块（`git diff` 的归因仍然成立，因为每个文件都能指回任务表里的某一项）。

任务编号沿用 issue 的 1–21，下面的每一条都对应至少一个 commit（任务 5 覆盖三条缺陷，拆成两个 commit）。

**阶段一：止血**

1. `requirements.txt`：补 `pyperclip>=1.8`；`pywin32==308; sys_platform == "win32"`；删 `psutil`。新增 `requirements-dev.txt`（`pytest>=7.0`、`flake8`）与 `pytest.ini`（`pythonpath = src`、`testpaths = tests`）。→ A1、B7
2. 改写 `:149` / `:390` / `:423` 三处 f-string 为 3.10 兼容写法；`.github/workflows/python-app.yml` 改成 3.10–3.13 矩阵。→ A2
3. 新增 `DEFAULT_TIMEOUT = (10, 30)`（连接 10 s / 读取 30 s），11 处 `session.get` 全部带上；启动时 `session.headers.update(...)`，`set_access_token` / `load_access_token` 同步更新 `session.headers`，`download_file` 不再单独传 `headers`。→ B6、C4
4. 把 `:516-520` 的资源列表获取与警告弹窗整体移到 `tk.Tk()` 之后。→ A4
5. （5a）`download_states` 用模块级 `threading.Lock` 保护读写，工作线程内所有 Tk 调用改为 `root.after(0, ...)`；（5b）`download()` 只在「没有任何线程在飞」时才解禁按钮，`download_states = []` 同样移入锁内并加同一前置条件。→ B1、B2、B3
6. 文件名清洗 + 重名自动加后缀（先落在单文件里的两个纯函数 `sanitize_filename()` / `unique_path()`，阶段二原样迁入 `core/naming.py`）。→ B4
7. 下载写 `<目标名>.pdf.part`，成功后 `os.replace` 原子改名，失败删残件。→ C3
8. `thread_it` 打开 `t.daemon = True`；删除 `on_closing` 里的 psutil 段落。→ B7

**阶段二：启动体验与结构**

9. 启动不阻塞：先建窗口显示「正在加载资源目录…」，后台线程加载；四个列表文件用 `ThreadPoolExecutor(max_workers=4)` 并行传输、串行解析。→ A3
10. 把版本探测拆成独立的廉价一步 `fetch_version()`，以它返回的版本标识为缓存键落盘，命中则秒开且完全不拉那 40 MB；探测失败时回退到离线缓存。→ A3
11. 建树时逐条目就地裁剪，只保留白名单字段，丢弃 `global_description` 等大字段。→ A3
12. 8 个 `OptionMenu` 换成 `ttk.Treeview` + 搜索框，节点直接携带 ID，按需填充子节点。→ B5、C6
13. 拆包（见 Architecture）；核心模块不得 import tkinter。→ C6
14. 引入 `logging`，所有 `except Exception: pass` 换成记日志 + 向用户展示具体原因；建树按条目容错。→ C1、C2

**阶段三：完善**

15. macOS Token 持久化；Linux / macOS 文件 `0600`；写失败不再谎报成功。→ C5
16. 下载并发上限（默认 4）+ 失败重试 + 断点续传。→ B7、C3
17. 删除 `fetch_lesson_list` 及其相关常量。→ C7
18. 版本号单一来源 `tchmaterial_parser.__version__`，`version.txt` 由脚本生成、CI 校验不过期。→ C7
19. CI 改为多版本 lint + test，新增 Windows / Linux 打包与发布 job。→ C7
20. 补单元测试（见 Testing）。
21. README 与代码对齐（删除「暂停/恢复」等未实现表述、更新安装与平台说明）；删除 `重构设计方案.md`。→ C7

### 缺陷编号 → 改动落点

| 缺陷 | 任务 | 改动落点（阶段二之后的最终位置） |
| --- | --- | --- |
| A1 | 1 | `requirements.txt`、`requirements-dev.txt` |
| A2 | 2 | 三处 f-string 改写后位于 `core/downloader.py`（原 `:149`）与 `core/catalog.py`（原 `:390`）；原 `:423` 随 `fetch_lesson_list` 删除。`.github/workflows/python-app.yml`、`README.md` |
| A3 | 9、10、11 | `ui/app.py`（先建窗、加载态、四步加载流程）、`core/catalog.py`（`fetch_version` / `fetch_tree` 两步拆分、并行传输串行解析、逐条目裁剪）、`core/cache.py`（版本键 + 离线回退）、`ui/catalog_tree.py`（按需填充） |
| A4 | 4 | `ui/app.py`（所有对话框只在 `Tk()` 之后出现） |
| B1 | 5 | `core/downloader.py`（只写状态，不碰 Tk）、`ui/app.py`（200 ms 轮询器） |
| B2 | 5 | `core/downloader.py` 的 `DownloadManager`（`threading.Lock` + 快照） |
| B3 | 5 | `ui/app.py`（点击时同步置灰，恢复权归轮询器，由 `DownloadManager` 的在飞计数派生） |
| B4 | 6 | `core/naming.py`（按字节截断 + 预留/归还） |
| B5 | 12 | `ui/catalog_tree.py`（Treeview 节点 ↔ 节点对象直接映射，不再按名字反查） |
| B6 | 3 | `core/http.py`（唯一出口强制注入 timeout） |
| B7 | 8、16 | `core/downloader.py`（线程池 + 取消 Event）、`core/catalog.py`（目录线程池的取消 Event）、`ui/app.py`（关窗时置位两个标志）、`requirements.txt`（删 psutil） |
| C1 | 14 | `core/errors.py`、`core/parser.py`、`core/logging_setup.py`、`ui/app.py`（展示具体原因） |
| C2 | 14 | `core/catalog.py`（逐条目 try/except + 跳过计数） |
| C3 | 7、16 | `core/downloader.py`（`.part` + `os.replace` + 重试 + `Range` / `If-Range` 校验续传） |
| C4 | 3 | `core/http.py`（`session.headers` 集中管理）、`core/tokens.py` |
| C5 | 15 | `core/tokens.py` |
| C6 | 12、13、14 | 整个 `src/tchmaterial_parser/` 包；`ui/platform_ui.py`（字体 / DPI / 图标） |
| C7 | 17、18、19、21 | 删 `fetch_lesson_list`；`__init__.py` 的 `__version__`；`scripts/gen_version_file.py`；`.github/workflows/python-app.yml`；`README.md`；删 `重构设计方案.md` |

### 文件 → 任务归因（对应验收标准第 5 条）

| 文件 | 任务 |
| --- | --- |
| `requirements.txt` / `requirements-dev.txt` / `pytest.ini` | 1 |
| `.github/workflows/python-app.yml` | 2、19 |
| `src/tchMaterial-parser.pyw` | 阶段一任务 1–8；阶段二收敛为入口 shim（任务 13） |
| `src/tchmaterial_parser/**` | 9–18 |
| `src/tchmaterial_parser/assets/**` | 13（图标从 base64 改为资源文件） |
| `tests/**` | 20 |
| `scripts/gen_version_file.py`、`version.txt` | 18 |
| `tchMaterial-parser.spec` | 13、18、19 |
| `README.md` | 2、15、21 |
| `重构设计方案.md`（删除） | 21 |
| `docs/designs/2026-09-11-hardening-and-restructure.md` | 本设计文档 |

## Architecture

### 目录结构

```
tchMaterial-parser/
├── requirements.txt                 # 运行时依赖（白名单内）
├── requirements-dev.txt             # pytest>=7.0 / flake8
├── pytest.ini                       # pythonpath = src；testpaths = tests
├── version.txt                      # PyInstaller 版本资源，由脚本生成
├── tchMaterial-parser.spec
├── README.md
├── docs/designs/2026-09-11-hardening-and-restructure.md
├── scripts/
│   └── gen_version_file.py          # 从 __version__ 渲染 version.txt，支持 --check
├── src/
│   ├── tchMaterial-parser.pyw       # 入口 shim：把 src 加进 sys.path 后调用 main()
│   └── tchmaterial_parser/
│       ├── __init__.py              # __version__ = "3.2.0"（版本号唯一来源）
│       ├── __main__.py              # python -m tchmaterial_parser
│       ├── config.py                # AppConfig(dataclass) + 缓存/配置/日志目录解析
│       ├── logging_setup.py         # setup_logging()：RotatingFileHandler + stderr
│       ├── core/
│       │   ├── __init__.py
│       │   ├── errors.py            # 领域异常
│       │   ├── http.py              # HttpClient：Session、超时、鉴权头、忽略代理
│       │   ├── parser.py            # 资源页 URL -> (pdf_url, content_id, title)
│       │   ├── catalog.py           # 资源树抓取 / 建树 / 字段裁剪
│       │   ├── cache.py             # 版本号为键的资源树磁盘缓存
│       │   ├── startup.py           # 启动时的目录加载编排（探版本 / 读缓存 / 拉取）
│       │   ├── naming.py            # 文件名清洗 + 去重
│       │   ├── downloader.py        # DownloadManager：线程池、断点续传、重试、状态
│       │   └── tokens.py            # Access Token 跨平台持久化
│       ├── ui/
│       │   ├── __init__.py
│       │   ├── app.py               # 主窗口、布局、生命周期、轮询器
│       │   ├── catalog_tree.py      # Treeview + 搜索框
│       │   ├── token_dialog.py      # Token 设置 / 帮助窗口
│       │   └── platform_ui.py       # 高 DPI、字体族选择、图标定位
│       └── assets/
│           ├── favicon_223x223.png  # 由 src/ 移入（原 base64 内嵌）
│           └── favicon_48x48.ico    # PyInstaller icon= 使用
└── tests/
    ├── conftest.py                  # FakeSession / FakeResponse 等测试替身
    ├── fixtures/*.json
    ├── test_parser.py
    ├── test_naming.py
    ├── test_catalog.py
    ├── test_cache.py
    ├── test_downloader.py
    ├── test_tokens.py
    └── test_core_is_headless.py
```

模块数量控制在 13 个源文件（原 735 行单文件），每个模块只承担一件事；不设 `models/`、`utils/`、`components/` 这类为将来准备的空壳目录——这正是旧方案的主要负担来源。

### 模块职责边界

**`config.py`** — 纯数据。`AppConfig` 是一个 `dataclass`，字段包括 `connect_timeout=10.0`、`read_timeout=30.0`、`max_download_workers=4`、`max_catalog_workers=4`、`max_retries=3`、`chunk_size=131072`、`progress_poll_ms=200`。另提供 `cache_dir()` / `config_dir()` / `log_dir()`：Windows 走 `%LOCALAPPDATA%` / `%APPDATA%`，macOS 走 `~/Library/Caches` / `~/Library/Application Support` / `~/Library/Logs`，Linux 走 `$XDG_CACHE_HOME` / `$XDG_CONFIG_HOME`（缺省回退 `~/.cache` / `~/.config`）。不读环境以外的任何配置文件——本程序没有需要用户手改的配置。

**`logging_setup.py`** — `setup_logging(level)`：一个 `RotatingFileHandler`（`log_dir()/tchMaterial-parser.log`，1 MB × 3）加一个 `StreamHandler`。仅由 `__main__.py` 调用一次；库模块一律只用 `logging.getLogger(__name__)`，绝不配置 root logger。

**`core/errors.py`** — `ParserError` 基类，派生 `InvalidUrlError`（URL 里没有 contentId）、`ResourceNotFoundError`（响应里找不到 PDF 项）、`AuthError`（401/403）、`NetworkError`（超时 / 连接失败）、`UpstreamFormatError`（JSON 结构与预期不符）。每个异常带一句可直接展示给用户的中文 `message`。这是 C1 的核心：`(None, None, None)` 这种「全部失败长一个样」的返回被彻底替换。

**`core/http.py`** — 唯一的网络出口。`HttpClient` 持有 `requests.Session`，构造时设置 `session.proxies = {"http": None, "https": None}` 与默认 `X-ND-AUTH` 头；`set_access_token(token)` 原地更新 `session.headers`（C4：详情接口从此也带鉴权）。对外只暴露 `get_json(url)` 与 `stream(url, extra_headers)`，两者**在方法内部注入 `timeout=(connect, read)`，调用方无法遗漏**（B6）。`requests` 的异常在此转换成 `NetworkError` / `AuthError`。

**`core/parser.py`** — 纯函数 + 一次网络调用。`parse(client, url) -> (pdf_url, content_id, title)`。保留现有三条分支（基础性作业 / 专题课程 / 普通电子课本）与未登录时的 URL 改写正则。失败抛 `core/errors.py` 里的具体异常，不再返回 `(None, None, None)`——C1 要的是「每种失败都可辨」，成功路径返回什么形状与此无关。

返回元组而不是 `dataclass`：下游只消费 `pdf_url` 与 `title`（界面拼下载链接用的是目录节点自带的 id，不是这里的 `content_id`）。为两个没人读的字段引入一个类型，属于为将来准备的结构；真有第三个消费者出现时再引入也不迟。

**`core/catalog.py`** — 资源树，分成**廉价的版本探测**与**昂贵的整树拉取**两步，这是缓存能真正生效的前提（A3、任务 10）：

- `fetch_version(client) -> CatalogVersion`：只取 `data_version.json` 这一个小文件，返回 `dataclass(version, urls)`——`version` 是缓存键，`urls` 是四个列表文件的地址。这一步的代价是一次小请求，**任何启动路径都付得起**。
- `fetch_tree(client, version, urls, progress_cb=None) -> CatalogNode`：取标签层级，拉取四个列表文件并建树。只有缓存未命中时才会被调用。
- 建树时**逐条目 try/except**，坏条目 `logger.debug` 记录并跳过，最后汇总一条 WARNING「跳过 N 条无法解析的条目」，绝不让一条坏数据毁掉整棵树（C2）。
- 容错分**条目**与**整页**两级，两级都计数并在末尾各汇总一条 WARNING。「整页不可用」包含连不上、HTTP 错、正文不是 JSON、不是数组这几种形态——对用户来说结论相同：这一页没有课本可用。`AuthError` 例外，它不降级：Token 失效四页都会中，而「请重新设置 Token」是用户唯一能据以行动的信息。但容错有下界：四个列表文件出自同一个接口，真实的格式变更是四页同时变形，此时「每页都跳过」会退化成一棵只有分类、没有课本的树。**一本课本都没挂上就抛 `UpstreamFormatError`**——否则 `load_catalog` 会把这棵空树当成好数据写进缓存，覆盖掉上一份能用的离线缓存，用户逐层展开全是空的且重启不自愈。响亮地失败，任务 10 的缓存回退才接得住。
- `CatalogNode` 是 `dataclass(node_id, display_name, resource_type_code, children)`，**只保留这四个字段**，`global_description` 等大字段在建树时丢弃（A3、任务 11）。`resource_type_code` 用 `.get(...) or "assets_document"` 取，缺字段不再 KeyError（C2 的 `:661` / `:691`）。
- `progress_cb` 是一个接受 `(done, total)` 的纯回调，由 UI 侧包装成线程安全的状态写入——`catalog.py` 本身对 UI 一无所知。
- 拉取与解析的并发形态见下文「数据规模与响应性」，那里的四条约束对本模块是硬性的。
- 模块持有自己的 `threading.Event` 取消标志，关窗时由 `ui/app.py` 置位；每拉完一个列表文件、每建完一批条目检查一次，避免关窗后目录线程还在闷头解析 40 MB（B7）。

**`core/cache.py`** — `load(cache_key) -> CatalogNode | None` / `load_any() -> tuple[str, CatalogNode] | None` / `store(cache_key, node)`。缓存文件是 `cache_dir()/catalog.json`，内容为 `{"version": <cache_key>, "tree": {...}}`，存的是裁剪后的树，约 1 MB。

- `load(cache_key)`：版本一致才返回树。JSON 损坏、版本不符、字段缺失一律当作未命中（记 WARNING 后重新拉取），永不因缓存崩溃。
- `load_any()`：不校验版本，连同它自己的版本号一起返回。**只在网络路径走不通时使用**——版本探测失败（离线），或版本探测成功、拉取那几个列表文件时中断。两种情形下旧树都比空面板有用，但它确实可能已经过时，所以这条路径一律把 `is_stale` 置为 `True`，UI 据此打上「离线缓存」标注。缓存里也没有东西时才向用户报告具体失败原因。
- `store()`：先写同目录 `.tmp` 再 `os.replace`，避免半截缓存。

**`core/startup.py`** — `load_catalog(client, progress_cb=None) -> (tree, is_stale, failure)`，把「探版本 → 查缓存 → 必要时拉取」这条启动路径编排成一个函数，供 UI 在后台线程里调用。

它独立成一个模块而不是并进相邻的两个：`cache` 需要 `catalog` 的 `CatalogNode` 才能反序列化，`catalog` 再反过来依赖 `cache` 就成环了，编排只能落在两者之上的一层。它也不属于 `ui/`——这段逻辑没有任何界面依赖，放进 `ui/` 会让它在无图形界面的环境里连导入都做不到，与「核心逻辑可在无 Tk 环境导入并测试」这条约束直接冲突。因此它和 `core/` 下其余模块受同一条纪律约束：不得 import `tkinter`。

**`core/naming.py`** — 纯函数，零 I/O 依赖以外的东西，是测试密度最高的模块（B4）。
- `sanitize_filename(title) -> str`：替换 `/ \ : * ? " < > |` 与所有 `ord(c) < 32` 的控制字符为 `_`；去掉首尾空白与点；`.` / `..` 视同空；Windows 保留名（`CON` `PRN` `AUX` `NUL` `COM1-9` `LPT1-9`，不分大小写、含扩展名形式）前缀 `_`；**按 UTF-8 编码后的字节数截断到 200 字节**；结果为空则用 `download`。
  按字节而非字符截断是因为文件系统的限制本来就是字节数（主流文件系统单个文件名上限 255 字节），而教材标题全是中文——一个汉字 UTF-8 占 3 字节，按字符数算的限额会让长标题照样写入失败，那正是 B4 要消灭的场景。200 字节这个数给 ` (99)` 这样的去重后缀与 `.pdf.part` 扩展名留了余量。截断**必须落在字符边界上**：先编码再截到 200 字节，若末尾切断了多字节序列就逐字节回退，直到 `decode("utf-8")` 成功。
- `unique_path(dir_path, base_name, ext) -> str` / `release_path(path)`：在一个模块级 `Lock` 保护的「已占用集合」里为并发任务分配互不冲突的路径，磁盘已存在的文件也算占用，冲突时依次尝试 ` (2)`、` (3)`……（实测同名教材多达 19 本，这是必须的）。
  **预留是有生命周期的**：`DownloadManager` 在任务终结时（无论成功还是失败）调用 `release_path()` 把它从集合里摘掉。成功的任务摘掉后磁盘上已有真实文件，下次申请照样会因「磁盘已存在」而让号，语义不变；失败的任务按 C3 已经删掉 `.part`，磁盘上并不存在该文件，归还号段才能让用户修好网络后重下同一本教材时仍拿到原本的名字，而不是一路涨到 ` (2)` ` (3)` ` (4)` 的幽灵序号。
- `assert_within(dir_path, final_path)`：对 `os.path.realpath` 的结果做前缀校验，堵死路径穿越；不通过直接抛异常。

**`core/downloader.py`** — `DownloadManager`，B1/B2/B3/B7/C3 的汇合点。
- 持有 `ThreadPoolExecutor(max_workers=config.max_download_workers)`、一个 `threading.Lock`、一个 `threading.Event` 取消标志，以及任务状态列表。
- `submit(resource_ref, save_path)` 提交任务；`snapshot() -> DownloadSnapshot` 在锁内返回一个**不可变的**聚合快照（已下载字节 / 总字节 / 完成数 / 总数 / 在飞数 / 新完成的失败原因列表）。UI 只读快照（B2）。
- 工作线程**不 import tkinter，也不持有任何 Tk 对象**（B1）。
- 下载流程：写同目录 `<name>.pdf.part` → 成功后 `os.replace` 到目标名（C3）；网络类错误按 `max_retries` 退避重试（1 s / 2 s / 4 s）；4xx（尤其 401/403）不重试，直接判失败；**最终失败删除 `.part`**，不留残件。
- **续传必须先确认续的是同一份文件。** 首次响应记下一个校验子：优先 `ETag`，没有则 `Last-Modified`，再没有则 `Content-Length`。重试时若 `.part` 已有 n 字节，请求带 `Range: bytes=n-` 与 `If-Range: <校验子>`，并在收到 206 后核对响应里的校验子是否与首次一致。校验子不一致、服务端回 200 而非 206、或三者都拿不到，就丢弃 `.part` 从零重下。三者都拿不到不算错误，只是降级为不续传。没有这层核对，同一 URL 的内容在两次请求之间变化时会拼出「旧文件前缀 + 新文件后缀」，再被 `os.replace` 改名成一个看起来成功的 PDF——那正是 C3 要消灭的损坏文件，而且比半截文件更难发现。
- 任务终结时（成功或失败）调用 `naming.release_path()` 归还预留的文件名。
- 分块循环每轮检查取消标志；`cancel_all()` 置位标志并 `shutdown(wait=False, cancel_futures=True)`，关窗时调用（B7）。已在执行的任务撤不掉，正阻塞在读取上的线程要等到读取超时才会看到标志，退出延迟的上界因此是 30 s，理由见 Discussion。
- 「是否还有在飞任务」由 `snapshot().in_flight` 唯一判定，UI 的下载按钮**恢复**为可用完全由它派生，不再有手工解禁的分支（B3）。

**`core/tokens.py`** — `load_token() -> str | None` 与 `save_token(token) -> str`（返回可展示的真实结果文案，C5）。
- Windows：注册表 `HKEY_CURRENT_USER\Software\tchMaterial-parser\AccessToken`，行为与现状一致。
- macOS：`~/Library/Application Support/tchMaterial-parser/data.json`。
- Linux：`$XDG_CONFIG_HOME/tchMaterial-parser/data.json`，并继续读取旧路径 `~/.config/tchMaterial-parser/data.json` 以兼容存量用户。
- 文件分支统一走「写临时文件 → `os.replace` 覆盖目标」：目录以 `0o700` 创建，同目录临时文件用 `os.open(tmp, O_CREAT|O_EXCL|O_WRONLY, 0o600)` 新建，内容写完再改名。`os.replace` 保留的是临时文件的权限位，**目标文件从不以宽松权限承载 Token**。直接对目标路径 `O_CREAT|O_TRUNC` 再补 `chmod` 是不够的：`os.open` 的 mode 只对新建文件生效，而 C5 点名的存量 Linux `data.json` 很可能已经是 `0644`，那条路径会在旧权限下写入新 Token，写完才收紧——中间的可读窗口正是要消除的那个，且进程若在此期间中断，宽松权限会长期留着。
- **写失败必须返回失败文案并记 ERROR**，绝不返回「已保存！」。
- 不引入 Keychain：stdlib 没有对应 API，走 `security` 命令行子进程既脆弱又会弹系统授权框，对一个可随时重取的 7 天期 Token 不划算。

**`ui/platform_ui.py`** — 三件跨平台杂活，全部收口于此：
- `apply_dpi_scaling(root) -> float`：Windows 走 `win32print` / `shcore.SetProcessDpiAwareness`，其他平台按 `root.winfo_fpixels("1i") / 96.0` 估算，逻辑与现状等价。
- `ui_font(size, bold=False) -> tuple`：按 `tkinter.font.families()` 的实际可用字体，从每个平台的偏好序列里挑第一个命中的（Windows：`Microsoft YaHei UI` / `微软雅黑`；macOS：`PingFang SC` / `Heiti SC`；Linux：`Noto Sans CJK SC` / `Source Han Sans SC` / `WenQuanYi Micro Hei`），全不命中则回落到 `TkDefaultFont` 的族名。取代散在 6 处的硬编码「微软雅黑」（C6）。
- `set_window_icon(root)`：用 `importlib.resources.files("tchmaterial_parser.assets")` 定位 `favicon_223x223.png`，删除 base64 内嵌与 `tempfile.gettempdir() + "/icon.png"` 固定文件名写盘（C6：消除符号链接攻击面）。PyInstaller 侧通过 spec 的 `datas` 把 assets 放进 `tchmaterial_parser/assets`，冻结与源码两种形态用同一段代码。

**`ui/catalog_tree.py`** — `ttk.Treeview` 取代 8 个 `OptionMenu`（B5、C6）。
- 节点按需展开：每个 Treeview item 的 `iid` 是自增字符串，旁边维护 `iid -> CatalogNode` 字典，**双击叶子节点时直接读 `node.node_id`**，彻底消灭「按 `display_name` 线性反查取第一个」的 6 处调用；19 本同名教材从此各选各的。
- 层级深度由数据决定，不再受「8 个下拉框」限制（C6）。
- **按需填充，绝不一次性插入全部节点**：首次只插入根层级的子节点；`<<TreeviewOpen>>` 事件触发时才填充被展开节点的直接子节点，并给该节点打一个「已填充」标记防止重复插入。详见下文「数据规模与响应性」(c)。
- 顶部一个搜索框：输入 2 个字符以上时做子串匹配，结果以扁平列表呈现，每行显示「完整路径 / 教材名」，用路径消除同名歧义；清空搜索框恢复树视图。结果**最多显示 200 条**，超出时在列表末尾提示「结果过多，请细化关键词」。
- URL 拼接只有**一处** `build_detail_url(node) -> str`（原来复制了 4 遍）。
- 资源树尚未就绪时显示「正在加载资源目录…」占位行；加载失败显示「资源目录加载失败：<原因>，可手动粘贴链接」，程序其余功能照常可用（A3、C2）。目录来自 `cache.load_any()` 的离线回退时，在树的顶部标注「离线缓存」，让用户知道内容可能不是最新的。

**`ui/token_dialog.py`** — Token 设置窗口与帮助窗口，含右键菜单、Esc 关闭、Enter 保存、居中，行为与现状一致，只是文案改为 `core/tokens.save_token()` 的真实返回值。

**`ui/app.py`** — 唯一知道「窗口生命周期」的地方。跨线程的界面更新一律排进一个 `queue.Queue`，由主线程的轮询器取出执行：`root.after` 只能由主线程、或在主线程已进入 `mainloop` 之后调用，而命中缓存时目录加载可能比 `mainloop` 起得还早，直接投递会失败，界面就永远停在加载占位上。
- `main()`：`setup_logging()` → `AppConfig()` → `HttpClient` → `tk.Tk()` → 建全部控件 → 启动后台线程加载资源树 → 启动 200 ms 轮询器 → `mainloop()`。**任何 `messagebox` 都在 `tk.Tk()` 之后**（A4）。
- 后台资源树线程调用 `core.startup.load_catalog()`（A3、任务 9/10），该函数内部依次是：

  1. `catalog.fetch_version()` 取版本键——只有一个小文件的代价。
  2. `cache.load(version)` 命中 → 直接用，**整个热启动路径不碰那 40 MB**。
  3. 未命中 → `catalog.fetch_tree()` 拉取建树 → `cache.store(version, tree)`。
  4. 第 1 步或第 3 步失败 → `cache.load_any()`，有货就用并标注「离线缓存」；没货才显示加载失败提示。

  `ui/app.py` 只负责把返回的 `(tree, is_stale, failure)` 排进界面队列，由主线程的轮询器取出并据此更新界面。
- 轮询器 `drain_ui_queue()` 每 200 ms 跑一次：先把下一次 tick 排上（这样中间任何一步抛异常都不会让轮询器整个死掉），再取空跨线程回调队列，最后 `poll_downloads()` 读一次 `DownloadManager.snapshot()` 刷新进度条与标签，并在「全部完成」时弹一次完成/失败汇总对话框、把下载按钮恢复为可用。完成对话框「弹两次或一次都不弹」的竞态（B2）在这里结构性消失——判定只发生在主线程的一个地方。
- 下载按钮的点击处理函数**先同步置灰再做任何事**（解析、选目录、提交都在置灰之后），恢复权归轮询器。两者的分工见 Discussion；这样 200 ms 窗口内的双击只会有第一次生效。
- `on_closing()`：若 `snapshot().in_flight > 0` 则询问；确认后置位**两个线程池的取消标志**（目录加载与下载）并各自 `cancel_all()` → `root.destroy()`，让 `main()` 正常返回，不再 `sys.exit(0)`，也不再有 psutil 杀子进程的无效代码（B7）。阻塞在网络读取上的线程最多 30 s 后结束，进程随之退出。

### 数据规模与响应性

A3 的本质是数据量问题，所以约束要写死成数字而不是原则。实测单个列表文件：原始 **9.92 MB**；`json.loads` 本身只要 **0.10 s**（解析速度从来不是瓶颈），但解析后的对象常驻 **19.7 MB**、过程峰值 **39.2 MB**；裁剪到必要字段后只剩 **0.43 MB**，缩减 **23 倍**。四个文件合计，裁剪后的整棵树约 **1.7 MB**。下面四条是硬约束：

**(a) 并行传输，串行解析。** 四个列表文件的**网络传输**并行（瓶颈在网络），但「`json.loads` + 裁剪」在一把锁里串行，同一时刻只存在一份中间对象。

以下为实测：按上游规模合成 4 个各约 9.5 MB 的列表文件，传输用一次真实的字节拷贝并叠加 0.15 s 传输耗时来模拟网络节奏，每个变体在独立子进程里跑 5 次，取中位数。内存以 `tracemalloc` 的 Python 分配峰值为主指标，`ru_maxrss` 增量为辅（后者含分配器高水位，抖动较大）。

| 变体 | Python 峰值 | RSS 增量 | 耗时 |
| --- | --- | --- | --- |
| 四路各自 `response.json()` | 105 MB | 125 MB | 0.33 s |
| 并行传输 + 串行解析，传输并发 4 | **67 MB** | 96 MB | 0.32 s |
| 并行传输 + 串行解析，传输并发 2 | 48 MB | 77 MB | 0.45 s |
| 并行传输 + 串行解析，传输并发 1 | 39 MB | 60 MB | 0.86 s |

两条结论：

- **串行化解析省下约 38 MB（105 → 67），而且几乎不花时间**——解析本身只占 0.10 s，耗时与朴素做法持平。它消掉的是「四份解析对象并存」这一项；剩下的约 39 MB 底噪来自同时在途的原始响应体与分配器高水位，串行化解析省不掉，也不该指望它省。
- **传输并发取 4**：比串行只多约 27 MB 峰值，墙钟却快 2.7 倍（0.86 s → 0.32 s）。并发度的代价是「同时在途的响应体份数」，上界约为 `(并发数 - 1) × 单文件大小`，这是一条可预测的线性代价，取 4 是在这条线上的合理位置。

冷启动的真实代价因此是**一次性约 67 MB 的峰值**；之后常驻的只有裁剪后的树，约 **1.4 MB**，落盘的缓存文件约 **0.98 MB**（4000 本课本）。

**(b) 裁剪在建树之前、逐条目就地完成。** 每读出一个条目立刻抽取 `CatalogNode` 需要的四个字段，原始 `dict` 用完即弃，绝不把整份原始列表留在内存里等建完树再筛。这是 23 倍缩减能兑现的唯一形态。

**(c) `ttk.Treeview` 按需填充。** 约 4000 条记录一次性灌进 Treeview，插入期间界面完全无响应。只插入当前展开层级的子节点：首次只填根层级，`<<TreeviewOpen>>` 时才填该节点的直接子节点，用「已填充」标记防重复。搜索结果的扁平列表同理设 200 条上限，超出提示细化关键词。

**(d) 缓存命中时完全不触碰那 40 MB。** 热启动只读约 1 MB 的裁剪后缓存文件。这正是把 `fetch_version()` 从 `fetch_tree()` 里拆出来的目的——版本探测必须廉价到可以无条件执行，缓存才有机会在付出 40 MB 之前拦下这次加载。

### 「核心模块不得 import tkinter」如何落实

四道闸，从约定到自动化逐层收紧：

1. **目录约定**：`core/` 下所有模块不得 import `tkinter`、不得 import `ui.*`。依赖方向单向：`ui -> core -> config/errors`，`core` 永不反向引用。
2. **接口形状**：`core` 向外传递进度与状态的方式只有两种——普通回调函数和不可变快照 `dataclass`（下载侧就是 `DownloadSnapshot`）。任何一端都不需要 Tk 对象，这让「顺手 import 一下 tkinter」在写代码时根本没有动机。
3. **自动化断言**：`tests/test_core_is_headless.py` 起一个子进程执行
   `import importlib, sys; [importlib.import_module(m) for m in CORE_MODULES]; assert "tkinter" not in sys.modules`，
   逐模块导入 `core/` 与 `config.py`、`logging_setup.py` 后断言 `sys.modules` 里没有 `tkinter`。用子进程是为了不被同一进程内其他测试导入的 UI 模块污染；子进程的 `env` 里**显式传 `PYTHONPATH=src`**，不依赖从父进程继承（pytest 的 `pythonpath` 配置只作用于 pytest 自己的进程）。这条断言一旦被破坏，CI 立刻变红。
4. **CI 环境**：lint + test job 在 `ubuntu-latest` 上不安装 `python3-tk`，`core` 的测试在无 Tk 的解释器上照样通过；只有打包 job 才装 `python3-tk`。环境本身就是第二道防线。

`ui/` 的测试用 `pytest.importorskip("tkinter")` 保护，缺 Tk 时跳过而非失败。

### 打包与版本号

- 版本号唯一来源：`src/tchmaterial_parser/__init__.py` 的 `__version__ = "3.2.0"`。
- 窗口标题 `f"国家中小学智慧教育平台 资源下载工具 v{__version__}"`；源码顶部注释不再写版本号。
- `scripts/gen_version_file.py` 从 `__version__` 渲染 `version.txt`（`filevers` / `prodvers` / `FileVersion` / `ProductVersion` 四处）。`version.txt` 仍然入库（PyInstaller 直接读它），但 CI 跑 `python scripts/gen_version_file.py --check`，内容与 `__version__` 不一致就失败——单一来源靠校验而不是靠自觉（C7）。
- `tchMaterial-parser.spec`：入口保持 `src/tchMaterial-parser.pyw`（shim），加 `pathex=['src']`、`datas=[('src/tchmaterial_parser/assets', 'tchmaterial_parser/assets')]`、`hiddenimports=['tchmaterial_parser']`。`icon=['src/tchmaterial_parser/assets/favicon_48x48.ico']`。已有的非 Windows 平台兼容处理（提交 `01babb2`）保留。
- `src/tchMaterial-parser.pyw` 保留为 shim（而非删除）的理由：Windows 上 `.pyw` 后缀决定了双击运行不弹控制台窗口，README 与 AUR 包都依赖这个路径；shim 只有 5 行——把 `src` 加进 `sys.path` 后调用 `tchmaterial_parser.__main__.main()`。

### CI

`.github/workflows/python-app.yml` 改为三个 job：

1. `lint-and-test`：`ubuntu-latest`，matrix `python-version: ["3.10", "3.11", "3.12", "3.13"]`。装 `requirements.txt` + `requirements-dev.txt`（`pywin32` 的 marker 保证 Linux 上不会尝试安装），跑
   `flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics`，
   再跑 `flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics`（保持现有的非阻塞风格检查），最后 `pytest -q` 与 `python scripts/gen_version_file.py --check`。
2. `build-windows`：`windows-latest` + Python 3.12，装 `requirements.txt` 与 `pip install pyinstaller==6.11.1`，跑 `pyinstaller tchMaterial-parser.spec`，上传 artifact。
3. `build-linux`：`ubuntu-latest` + Python 3.12，先 `sudo apt-get install -y python3-tk`（PyInstaller 需要真实的 Tk 才能打进产物），同样装 `requirements.txt` 与 `pyinstaller==6.11.1`，再打包并上传 artifact。

PyInstaller 钉版本并且**只装在这两个打包 job 里**，不进 `requirements-dev.txt`：它是构建工具而非开发测试依赖，`requirements-dev.txt` 保持「只有 pytest 与 flake8」，让本地开发者 `pip install -r requirements-dev.txt` 不必为跑一次单测拖进一整套打包链。

推 tag 时追加一个 `release` job，把两个 artifact 附到 Release 上——这样 README 里「发布 Windows 与 Linux 产物」的承诺才第一次成立（C7）。

## Testing

Phase 3 的把关方按本节执行。

### 命令

```sh
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
python3 -m pytest -q
python3 scripts/gen_version_file.py --check
```

四条命令都在仓库根目录执行，本机 `python3`（3.11.2）与 `/opt/homebrew/bin/python3.13` 上都必须通过。**跑完整套不应产生任何网络请求**——若测试需要联网才能过，就是测试写错了。

`pytest` 能找到被测包靠仓库根目录的 `pytest.ini`：

```ini
[pytest]
pythonpath = src
testpaths = tests
```

`pythonpath` 是 pytest 7 内置的配置项，因此 `requirements-dev.txt` 里把下限钉为 `pytest>=7.0`。这样既不需要 `pyproject.toml` / `setup.py` 把包装成可安装工程，也不需要 `conftest.py` 手工改 `sys.path`，更不引入任何新依赖——`src/` 布局下 pytest 默认不会把 `src` 加进 `sys.path`，缺了这三行 `tests/` 在收集阶段就会 `ModuleNotFoundError`。

额外的两条冒烟检查：

```sh
python3 -c "import ast; ast.parse(open('src/tchMaterial-parser.pyw',encoding='utf-8').read())"   # A2 回归
PYTHONPATH=src python3 -m tchmaterial_parser                                                     # 仅在有图形环境时手动验证建窗
```

### 测什么

| 测试文件 | 覆盖的缺陷 | 要点 |
| --- | --- | --- |
| `test_parser.py` | C1、C4 | contentId / contentType 的提取（缺失、乱序、多余参数、contentType 缺省为 `assets_document`）；未设 Token 时的 URL 改写正则（含不匹配时原样返回）；专题课程回退到 `thematic_course/.../resources/list.json` 的分支；基础性作业分支；401/403 抛 `AuthError`、超时抛 `NetworkError`、结构异常抛 `UpstreamFormatError`——每种失败各自可辨，而不是同一个 `None` |
| `test_naming.py` | B4 | `/ \ : * ? " < > \|` 与控制字符被替换；`../../etc/passwd` 这类标题清洗后 `assert_within` 通过；Windows 保留名加前缀；纯空白 / 空标题回落 `download`；**由 200 个汉字构成的标题，清洗后 `len(name.encode("utf-8")) <= 200` 且 `name` 可正常 `encode`/`decode`（没有在多字节字符中间切断）**；同名连续申请得到 ` (2)` ` (3)`；两个线程并发申请同一名字拿到不同路径；**`release_path()` 归还后再申请同一名字，拿回的是不带序号的原名** |
| `test_catalog.py` | A3、C2 | `fetch_version()` 只发一个请求就拿到版本与 `urls`（用 FakeSession 的调用计数断言，这是缓存能生效的前提）；用 fixture 建树，断言层级结构正确；一条缺 `tag_paths` 映射的坏数据只被跳过、其余节点完整保留；**递归遍历结果树的每一个节点，断言都不含 `global_description` 等被裁剪的大字段**（防止字段裁剪被后续改动悄悄破坏）；两本同名教材以不同 `node_id` 同时存在；`resource_type_code` 缺失时取到默认值而不抛 KeyError |
| `test_cache.py` | A3 | 写入后 `load(同版本)` 读回等价；`load(不同版本)` → 未命中；`load_any()` 在版本不同时仍返回树与它自己的版本号；文件内容被截断 / 写成非 JSON → 两个 load 都未命中且不抛异常；写入过程中目标目录已有旧缓存时 `os.replace` 覆盖成功 |
| `test_downloader.py` | B1、B2、B3、B7、C3 | 成功路径：过程中只存在 `.part`，结束后只存在 `.pdf`；4xx 失败后目录里既无 `.part` 也无 `.pdf`；瞬时网络错误重试 3 次后成功，且第 2 次起请求同时带 `Range: bytes=` 与 `If-Range`；**续传响应的 `ETag` 与首次不一致时 `.part` 被丢弃、文件从零重下，最终内容等于新文件而不是新旧拼接**；服务端对 Range 回 200 时从头重写；响应没有 `ETag` / `Last-Modified` / `Content-Length` 时降级为不续传且任务仍能成功；取消 Event 置位后分块循环在下一次读取返回时退出；任务终结后预留的文件名已被 `release_path()` 归还；多线程并发提交后 `snapshot()` 的计数自洽（这是 B2 的回归测试）；断言下载线程从不接触任何 Tk 对象（用一个会在被调用时失败的哨兵回调） |
| `test_tokens.py` | C5 | 写入后 `os.stat(path).st_mode & 0o777 == 0o600`（Windows 上 skip）；**预置一个 `0o644` 的旧 `data.json`，写入后权限变为 `0o600`，且写入全程目标路径从未以 `0o644` 承载新 Token**（校验落到临时文件 + `os.replace` 的实现上）；round trip 一致；目标目录不可写时返回的文案含失败字样且**不含**「已保存」；Linux 上旧路径 `~/.config/...` 仍可读出 |
| `test_core_is_headless.py` | C6 | 子进程导入全部 `core` 模块后断言 `"tkinter" not in sys.modules` |

UI 模块不写自动化测试（无头环境下不保证有 Tk，且 Tkinter 的事件驱动交互用自动化测试验证的性价比很低）；`ui/` 的正确性靠 `pytest.importorskip("tkinter")` 保护的导入冒烟 + 人工验证 README 里的使用流程。

### Fixture 从哪来

`tests/fixtures/` 下全部是**手工裁剪的 JSON**，每个文件控制在几 KB，来源是源码 `:41-62` 注释里记录的真实响应结构，以及现有代码对字段的实际使用（`ti_items` / `lc_ti_format` / `ti_storages` / `tag_paths` / `hierarchies` / `tag_id` / `tag_name` / `urls` / `title` / `name` / `id` / `resource_type_code`）。**不在测试期访问网络，也不把 40 MB 的真实列表文件入库。**

| Fixture | 内容 |
| --- | --- |
| `details_tch_material.json` | 一条普通电子课本详情：`ti_items` 含一个 `lc_ti_format == "pdf"` 的项，`ti_storages` 为三个 `*-private.ykt.cbern.com.cn` 地址 |
| `details_no_pdf.json` | `ti_items` 里没有 pdf 项，用于 `ResourceNotFoundError` |
| `details_thematic_course.json` + `thematic_course_resources.json` | 专题课程的两段式解析路径 |
| `tch_material_tag.json` | 三层标签树，每层 2–3 个节点 |
| `data_version.json` | 含 `urls` 字段（逗号分隔的两个假地址）与版本标识 |
| `book_list_page.json` | 6 条课本记录：2 条 `title` 完全相同（复现实测的重名场景）、1 条 `tag_paths` 为空数组、1 条 `tag_paths` 指向标签树里不存在的分支（触发跳过）、1 条无 `title` 只有 `name`、1 条 `title` 含 `/` 与 `:` 且带前后空格（喂给命名测试） |

网络替身放在 `conftest.py`：一个 `FakeResponse`（`status_code` / `json()` / `headers` / `iter_content()` / `raise_for_status()`）和一个按 URL 返回预置响应的 `FakeSession`，可配置抛 `requests.Timeout` 或返回指定状态码。因为 `HttpClient` 在构造时接受一个可选的 session 参数，注入替身不需要 monkeypatch，也不需要 `responses` / `requests-mock` 这类额外依赖——符合依赖白名单。

## Assumptions & Open Questions

- Python 版本下限定 3.10 还是 3.12？→ 默认 **3.10**：三处 PEP 701 f-string 是纯机械改写，代价接近零；抬到 3.12 会挡住 AUR 与旧发行版用户，也让本机 3.11.2 无法跑测试（与验收标准直接冲突）。3.10 而不是 3.9，是因为 `:22` 的 `X | Y` 注解在运行时求值。
- `data_version.json` 的版本字段确切叫什么？现有代码只读了 `urls`，环境事实一节也没有给出字段名。→ `fetch_version()` 默认**优先读 `version` / `module_version` 等常见键，都不存在则用整个响应体的 SHA-256 前 16 位作为缓存键**。后者在任何字段命名下都正确（内容变则键变），代价仅是服务端偶发的无意义字节抖动会导致一次多余的重新拉取。
- 缓存要不要设过期时间？→ 默认**不设**。缓存键已经绑定了上游版本，加 TTL 只会在上游没变时制造无谓的 40 MB 流量。另在「加载失败」提示里提供一个「强制刷新」入口，用户可手动废弃缓存。
- `fetch_version()` 失败时该拿旧缓存顶上，还是让目录彻底不可用？→ 默认**拿旧缓存顶上并在界面标注「离线缓存」**。教材目录的变动频率以周计，一棵可能过时几天的树对离线用户远胜于一个空白面板；标注让用户自己判断要不要联网重试。旧缓存也没有的场合才显示加载失败，此时手动粘贴链接的通路仍然可用。
- 断点续传的范围？→ 默认**只覆盖同一次运行内的重试**：重试时带 `Range` + `If-Range` 续传并核对校验子，最终失败则删除 `.part`。跨进程续传要在下次启动时判断残留的 `.part` 是否还对应同一份远端文件，而上游未承诺稳定的 `ETag`，判据不可靠；这与 C3「失败清理残件」的要求也一致。
- 上游不返回 `ETag` / `Last-Modified` / `Content-Length` 中的任何一个时，续传该怎么办？→ 默认**降级为不续传**（丢弃 `.part` 从零重下），而不是报错。拿不到校验子只是慢一点；无校验子的续传可能产出损坏的 PDF，而损坏的 PDF 比重下一次贵得多。
- 文件名的字节上限取多少？→ 默认 **200 字节**。主流文件系统的单文件名上限是 255 字节，留 55 字节给 ` (99)` 这类去重后缀、`.pdf.part` 扩展名以及个别文件系统更严的余量；再小会让完整教材名被切掉有意义的部分（实测教材标题最长约 40 个汉字，合 120 字节，200 字节不会误伤正常标题）。
- 搜索结果的显示上限取多少？→ 默认 **200 条**。超过 200 条说明关键词本身没有区分度，继续往下翻不如改关键词；上限同时兜住了「搜一个单字就往 Treeview 里灌几千行」导致的界面卡顿。
- PyInstaller 该不该钉版本？→ 默认**钉 `6.11.1` 且只装在打包 job 里**。打包器的行为变化（隐式导入收集、bootloader）是产物层面的回归，浮动版本会让某次与代码无关的上游更新把发布流水线弄红；只装在打包 job 里则保证「开发依赖只有 pytest 与 flake8」这句话为真。
- 下载并发上限取多少？→ 默认 **4**。上游是 CDN，4 路足以跑满家用带宽；再高只会在弱网下同时拖慢所有任务，且线程数需要有个确定上限来治 B7（粘贴 100 个链接 = 100 个线程）。
- 超时取多少？→ 默认 **(连接 10 s, 读取 30 s)**。读取超时是「两次收到数据之间」的间隔而非总时长，30 s 对大 PDF 足够宽松，又能让「服务端不响应」在半分钟内变成可见的错误而不是永久挂起。
- 关窗后最多 30 s 才退出进程，能接受吗？→ 默认**接受**，不为此缩短读取超时，也不追求「立即退出」。这个上界只在「关窗的同时恰好有线程卡在网络读取上」时才会摸到，且期间窗口已经消失、用户无需等待；把读取超时调小以换取更快退出，会让弱网下的正常下载频繁误判失败，代价明显更大。
- macOS 的 Token 该存 Keychain 还是文件？→ 默认**存文件**（`~/Library/Application Support/tchMaterial-parser/data.json`，`0600`）。stdlib 没有 Keychain 绑定，调 `security` 命令行会弹系统授权框且难以测试；Token 是 7 天有效、可随时重取的凭据，文件权限已是相称的保护。README 的 FAQ 需同步更新——它目前说 macOS 不保存 Token。
- `fetch_lesson_list` 删还是修？→ 默认**删**。理由见 Discussion（从未启用、接口契约与课本列表不一致且无法离线验证、UI 的 URL 形式接不上、启用会让启动负载翻倍）。删除可从 git 历史恢复。
- 版本号定多少？→ 默认 **3.2.0**。用户可见的功能集没有增减，本轮是加固与内部重整，次版本号 +1 足以表达；留 4.0.0 给真正的功能变更。
- 阶段一是否值得在旧单文件上打补丁（反正阶段二要重写）？→ 默认**值得**。issue 明确要求三阶段「独立可用」，阶段一的每个 commit 单独 checkout 出来都必须是能跑的程序；这也让 `git bisect` 在将来定位回归时有意义。代价是部分改动会在阶段二被搬家，但内容（清洗函数、超时常量、`.part` 逻辑）是原样迁移而非重做。
- `src/tchMaterial-parser.pyw` 保留还是删除？→ 默认**保留为 5 行 shim**。Windows 上 `.pyw` 后缀决定双击不弹控制台，README 与 AUR 打包都引用这个路径；删掉属于对外破坏性变更，而收益只是少一个文件。
- 进度更新用 `root.after(0, ...)` 逐次推送还是主线程轮询？→ 最终形态默认**轮询（200 ms）**。逐次推送在批量下载时每秒会向 Tk 事件队列灌入上千个回调；轮询开销恒定，且「全部完成」的判定收敛到主线程唯一一处，顺带结构性地消除 B2 的重复弹窗竞态。阶段一的止血版本仍按 issue 任务 5 的字面要求用 `root.after(0, ...)`。
- 资源树搜索的触发阈值？→ 默认**输入满 2 个字符后即时过滤**，不加防抖。本地内存里几千条记录的子串匹配在毫秒级完成，防抖属于没有测量支撑的优化。
- UI 要不要写自动化测试？→ 默认**不写**，只做 `importorskip` 保护的导入冒烟。无头 CI 不保证有 Tk，Tkinter 事件驱动交互的自动化成本远高于收益；核心逻辑已经全部被挤到无 Tk 依赖的 `core/` 里，这正是拆包的目的。
- `重构设计方案.md` 删除还是原地替换？→ 默认**删除**（`git rm`），本文档是唯一设计稿，并在 README 里加一行指向 `docs/designs/`。issue 要求「不要留两份并存」，保留一份已被推翻的方案只会误导后来者。
