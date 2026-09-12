#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可复现的取消/暂停续传实测记录（issue 要求的"实测证据"，不只是单元测试）。

真实的 tchMaterial-parser 图形界面没法自动化，这里直接调用 download_panel 的公开函数，
对着本地起的真实 HTTP 服务（真的实现 Range/If-Range/ETag/416 语义，绑 port 0，纯
loopback，不需要联网）跑两轮：

    第一轮：开始下载 → 暂停（记录已下字节数、.tmp 大小）→ 继续（记录续传请求带的
            Range 头、实际衔接位置是否对得上）→ 完成（记录最终字节数、与原文件
            是否逐字节一致）
    第二轮：下载到一半 → 取消（记录 .tmp 是否被清理、目标文件是否被生成）

不是 pytest 用例（文件名不叫 test_*.py，不会被自动收集），直接运行：

    python3 tests/manual_resume_demo.py

任何一步不符合预期都会用 AssertionError 中止并给出非零退出码，而不是打印一句
"看起来不对"就算了——这份记录是给人核对的，但核对的标准是写死在脚本里的断言。
"""

from pathlib import Path
import hashlib
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _range_server import start_range_server, stop_range_server # noqa: E402

from src.tchmaterial_parser.ui import download_panel as panel # noqa: E402


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wait_until(predicate, timeout: float = 5.0, message: str = "等待超时") -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), message


class _StubWidget:
    def config(self, **kwargs: object) -> None:
        pass


def main() -> None:
    # download_panel 的进度反馈会去碰真实 Tk 控件，这里用最简单的桩顶替，
    # 只是为了能在没有窗口的环境下跑通，不影响下载/续传逻辑本身。
    panel.progress_label = _StubWidget()
    panel.download_progress_bar = _StubWidget()
    panel.ui_call = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    panel._MIN_REQUEST_INTERVAL = 0
    panel.download_states = []

    content = os.urandom(2 * 1024 * 1024) # 2 MB 随机内容，逐字节比对才有意义
    etag = f'"{sha256(content)}"'
    server, server_thread, url = start_range_server(content, etag)
    log(f"起本地服务：{url}（真实 Range / If-Range / ETag / 416 语义，纯 loopback，未联网）")

    recorded_requests: list[tuple[int | None, str | None]] = []
    original_request_download = panel.request_download

    def recording_request_download(url_arg: str, range_from: int | None = None, validator: str | None = None):
        recorded_requests.append((range_from, validator))
        return original_request_download(url_arg, range_from=range_from, validator=validator)

    panel.request_download = recording_request_download

    tmp_root = Path(__file__).resolve().parents[1] / ".tmp"
    tmp_root.mkdir(exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp_dir:
            log("=== 第一轮：开始下载 → 暂停 → 继续 → 完成 ===")
            save_path = str(Path(tmp_dir) / "round1.pdf")
            state = panel.create_download_state(url, save_path)
            control = panel.BatchControl()
            state["control"] = control

            log(f"开始下载：{url} → {save_path}（原文件 {len(content)} 字节，sha256={sha256(content)[:16]}...）")
            worker = threading.Thread(target=panel.download_file, args=(url, save_path, None, state))
            worker.start()

            target = len(content) // 3
            wait_until(lambda: state["downloaded_size"] >= target, message="没能等到下载进度，前置条件不成立")

            paused_downloaded = state["downloaded_size"]
            paused_tmp_size = os.path.getsize(f"{save_path}.tmp")
            log(f"暂停：此刻已下 {paused_downloaded} 字节，.tmp 大小 = {paused_tmp_size} 字节")
            assert paused_downloaded == paused_tmp_size, ".tmp 大小与计数器不一致"
            assert paused_downloaded < len(content), "文件已经下完了，没能制造出真正的中途暂停"

            control.pause_event.set()
            panel.close_active_responses(control)
            worker.join(timeout=5)
            assert not worker.is_alive(), "暂停没有让工作线程退出"
            assert not state["finished"], "暂停后 finished 应为 False，留给继续"
            log(f"批次线程已退出：finished={state['finished']}（预期 False）")

            recorded_requests.clear()
            control.pause_event.clear()
            resume_offset_before = os.path.getsize(f"{save_path}.tmp")
            log("继续：重新调用 download_file（等价于点击“继续”按钮）")
            panel.download_file(url, save_path, None, state)

            assert len(recorded_requests) >= 1, "没有发出续传请求"
            range_from, validator = recorded_requests[0]
            log(f"续传请求头：Range: bytes={range_from}-，If-Range: {validator}")
            assert range_from == resume_offset_before, f"续传偏移 {range_from} 与暂停时 .tmp 大小 {resume_offset_before} 不一致，接错位置了"
            log(f"实际衔接位置核对：请求偏移 {range_from} == 暂停时 .tmp 大小 {resume_offset_before} → 一致")

            assert state["finished"], "续传后应该完成"
            assert state["failed_reason"] is None, f"续传失败：{state['failed_reason']}"
            final_bytes = Path(save_path).read_bytes()
            identical = final_bytes == content
            log(f"完成：最终 {len(final_bytes)} 字节，sha256={sha256(final_bytes)[:16]}...，与原文件逐字节完全一致：{identical}")
            assert identical, "续传后的文件与原文件不是逐字节一致"
            assert not os.path.exists(f"{save_path}.tmp"), "完成后 .tmp 应该已经被重命名掉"

            log("=== 第二轮：下载到一半 → 取消 ===")
            save_path2 = str(Path(tmp_dir) / "round2.pdf")
            state2 = panel.create_download_state(url, save_path2)
            control2 = panel.BatchControl()
            state2["control"] = control2

            worker2 = threading.Thread(target=panel.download_file, args=(url, save_path2, None, state2))
            worker2.start()
            half = len(content) // 2
            wait_until(lambda: state2["downloaded_size"] >= half, message="没能等到下载进度，前置条件不成立")
            log(f"下载到一半：已下 {state2['downloaded_size']} 字节（.tmp 存在：{os.path.exists(f'{save_path2}.tmp')}）")

            control2.cancel_event.set()
            panel.close_active_responses(control2)
            worker2.join(timeout=5)
            assert not worker2.is_alive(), "取消没有让工作线程退出"

            tmp_exists = os.path.exists(f"{save_path2}.tmp")
            target_exists = os.path.exists(save_path2)
            log(f"取消完成：finished={state2['finished']}，.tmp 是否还在：{tmp_exists}，目标文件是否生成：{target_exists}")
            assert state2["finished"], "取消后 finished 应为 True"
            assert not tmp_exists, ".tmp 没有被清理"
            assert not target_exists, "取消后不应该生成目标文件"
            log("结论：.tmp 已被清理，目标文件未生成，取消生效")

        log("全部通过：暂停/继续字节级精确衔接，取消正确清理半截文件。")
    finally:
        panel.request_download = original_request_download
        stop_range_server(server, server_thread)
        log("本地服务已停止")


if __name__ == "__main__":
    main()
