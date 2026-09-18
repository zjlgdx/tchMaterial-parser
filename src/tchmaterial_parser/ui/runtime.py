# -*- coding: utf-8 -*-
# 界面运行时状态：主窗口、缩放因子，以及跨线程的调度封装
#
# 工作线程不能直接调 root.after_idle：Tk 9 下这样排进去的回调确实拿到了编号，却没人唤醒主循环，
# 于是永远不会执行。Tk 也没有可移植的「从别的线程唤醒事件循环」接口，
# 因此线程只往队列里放任务，由主线程定时轮询取出执行。

import logging, queue, threading, time
import tkinter as tk
from collections.abc import Callable

logger = logging.getLogger(__name__)

ui_scale = 1.0 # 界面缩放因子，由 app.py 根据屏幕 DPI 写入
app_closing = False

UI_QUEUE_POLL_MS = 16 # 主线程轮询队列的间隔，与 60 Hz 同量级
UI_QUEUE_BUDGET_MS = 8 # 单次轮询执行回调的时间预算，超出即让出一帧给重绘

_ui_queue: queue.Queue[tuple[Callable[..., object], tuple, dict]] = queue.Queue()
_pump_after_id: str | None = None

def bind_root(window: tk.Tk) -> None: # 由 app.py 在创建主窗口后写入，供其余模块调度到主线程
    global root
    root = window
    start_ui_pump()

def scaled(size: float) -> int: # 按缩放因子换算界面元素的像素尺寸
    return round(size * ui_scale)

def thread_it(func: Callable[..., object], *args: tuple, **kwargs: dict) -> None: # 打包函数到线程
    t = threading.Thread(target=func, args=args, kwargs=kwargs)
    t.daemon = True
    t.start()

def ui_call(func: Callable[..., object], *args: tuple, **kwargs: dict) -> None: # 在主线程执行 Tkinter UI 更新
    if app_closing:
        return

    _ui_queue.put((func, args, kwargs))
    if threading.current_thread() is threading.main_thread():
        # 主线程投递时额外排一次空闲回调，保持「同一次 update() 内就执行」的既有时序，不必等到下一轮轮询
        try:
            root.after_idle(_pump)
        except Exception as e: # 主窗口尚未创建或已销毁
            logger.debug("排队界面更新失败：%s", e)

def report_callback_exception(error: Exception) -> None: # 回调异常交给 Tk 的统一出口，取不到就自己落日志
    try:
        root.report_callback_exception(type(error), error, error.__traceback__)
    except Exception:
        logger.error("界面回调出错", exc_info=error)

def _pump() -> None: # 取出队列中的回调并在主线程执行
    deadline = time.monotonic() + UI_QUEUE_BUDGET_MS / 1000
    while not app_closing:
        try:
            func, args, kwargs = _ui_queue.get_nowait()
        except queue.Empty:
            return

        try:
            func(*args, **kwargs)
        except Exception as e: # 一个坏回调不能让整个泵停摆，其余回调仍要送达
            report_callback_exception(e)

        if time.monotonic() >= deadline:
            return

def _tick() -> None: # 轮询一轮并重新排期
    global _pump_after_id
    _pump_after_id = None
    if app_closing:
        return

    _pump()
    try:
        _pump_after_id = root.after(UI_QUEUE_POLL_MS, _tick)
    except Exception as e: # 主窗口销毁后 after 会抛错，轮询到此为止
        logger.debug("界面更新轮询结束：%s", e)

def start_ui_pump() -> None: # 启动主线程的队列轮询；重复调用只保留一个定时器
    global _pump_after_id
    if _pump_after_id is not None:
        try:
            root.after_cancel(_pump_after_id)
        except Exception as e:
            logger.debug("取消旧的界面更新轮询失败：%s", e)
        _pump_after_id = None

    try:
        _pump_after_id = root.after(UI_QUEUE_POLL_MS, _tick)
    except Exception as e:
        logger.debug("启动界面更新轮询失败：%s", e)
