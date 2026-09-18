# -*- coding: utf-8 -*-
# 运行日志：滚动文件与控制台输出、敏感数据脱敏，以及启动环境与关键操作耗时的记录
#
# 脱敏放在格式化的最末端：异常堆栈与消息参数都是 Formatter 此刻才拼成文本的，
# 更早的环节（如 Filter）看不到它们，requests 异常里带鉴权参数的完整 URL 就会原样落盘。

import importlib, logging, os, platform, re, sys, time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import metadata
from logging.handlers import RotatingFileHandler

from . import __version__, config
from .platform_utils import os_name

PACKAGE_LOGGER = "tchmaterial_parser"
LOG_FILE_NAME = "tchMaterial-parser.log"
LOG_MAX_BYTES = 1024 * 1024 # 单个日志文件的上限
LOG_BACKUP_COUNT = 3 # 另外保留的历史份数
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_LEVEL_ENV = "TCHMATERIAL_LOG_LEVEL" # 设为 DEBUG 可让文件日志记下耗时等细节
REDACTED = "<已隐藏>"
UNKNOWN_VERSION = "未知"

# 依赖的分发名与导入名；分发名用于 importlib.metadata，导入名用于读模块自身的 __version__
DEPENDENCIES = (("pillow", "PIL"), ("psutil", "psutil"), ("pypdf", "pypdf"), ("requests", "requests"), ("sv-ttk", "sv_ttk"))
WINDOWS_DEPENDENCIES = (("pywin32", "win32api"),)

# Token 可能以查询串、表单、URL 编码、HTML 转义或 JSON 字段等形态出现，统一锚定在键名上，
# 键名之外一概不动，免得把正常文本也打成马赛克
_TOKEN_KEY = r"access[_-]?token"
# 值一直取到分隔符为止：除了 & 与各类括号、引号、逗号分号，URL 编码的 &（%26）同样算分隔符。
# 不能整个排除 %，被编码的 Token 自身可能含 %2B、%2F 这类转义
_TOKEN_VALUE = r"(?:(?!%26)[^&\s'\",;<>)\]}])+"
_TOKEN_ASSIGNMENT = re.compile(rf"({_TOKEN_KEY}\s*(?:=|%3D)){_TOKEN_VALUE}", re.IGNORECASE)
_TOKEN_JSON = re.compile(rf"([\"']{_TOKEN_KEY}[\"']\s*:\s*[\"'])[^\"']*", re.IGNORECASE)
_BEARER = re.compile(r"(Bearer\s+)[^\s'\",]+", re.IGNORECASE)
_MAC_ID = re.compile(r"(MAC\s+id=\")[^\"]*", re.IGNORECASE)
_MAC_SIGNATURE = re.compile(r"(\bmac=\")[^\"]*", re.IGNORECASE) # X-ND-AUTH 里的签名与 id 同样敏感
_COOKIE = re.compile(r"((?:Cookie|Set-Cookie)\s*[:=]\s*)[^\r\n]+", re.IGNORECASE)

logger = logging.getLogger(PACKAGE_LOGGER)
_configured = False

def redact_access_token(text: str) -> str:
    """隐藏文本里可能残留的 accessToken。本工具不再主动拼接该参数，但异常或用户粘贴的 URL 仍可能带上。"""
    text = _TOKEN_ASSIGNMENT.sub(rf"\1{REDACTED}", text)
    return _TOKEN_JSON.sub(rf"\1{REDACTED}", text)

def redact_sensitive(text: str) -> str: # 在 accessToken 之外，再遮蔽鉴权头与内存中的凭据原文
    text = redact_access_token(text)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    text = _MAC_ID.sub(rf"\1{REDACTED}", text)
    text = _MAC_SIGNATURE.sub(rf"\1{REDACTED}", text)
    text = _COOKIE.sub(rf"\1{REDACTED}", text)
    for secret in (config.access_token, config.mac_key): # 凭据也可能以裸串形式出现在日志里
        if secret and len(secret) >= 8: # 过短的值多半不是真凭据，全局替换反而会把正常文本打成马赛克
            text = text.replace(secret, REDACTED)
    return text

class RedactingFormatter(logging.Formatter): # 在格式化的最末端统一脱敏，一次覆盖正文、参数、异常堆栈与调用栈
    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive(super().format(record))

def file_log_level() -> int: # 文件日志级别，默认 INFO，可由环境变量提到 DEBUG
    level = logging.getLevelName(os.environ.get(LOG_LEVEL_ENV, "").strip().upper() or "INFO")
    return level if isinstance(level, int) else logging.INFO

def setup_logging() -> None: # 配置本程序的日志输出，重复调用不会叠加 handler
    global _configured
    if _configured:
        return
    _configured = True

    level = file_log_level()
    logger.setLevel(min(level, logging.WARNING))
    logger.propagate = False
    formatter = RedactingFormatter(LOG_FORMAT)

    if sys.stderr: # 打包后的 GUI 程序没有控制台，此时 sys.stderr 为 None
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    log_dir = None
    try:
        # 定位目录本身也可能失败：缺少 HOME 时 Path.home() 会抛 RuntimeError。
        # 这是 main() 的第一步，无论如何都不能把程序拦在启动阶段，取不到就只留控制台输出。
        log_dir = config.log_dir_path()
        if not log_dir: # 本工具尚未支持该系统的持久化，没有确定的日志目录可用
            return
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / LOG_FILE_NAME, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8", delay=True,
        )
    except (OSError, RuntimeError) as e: # 只读文件系统、权限不足、定位不到用户目录等
        logger.warning("无法启用日志文件（%s）：%s", log_dir or "目录未知", e)
        return
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

def dependency_version(dist_name: str, module_name: str) -> str: # 逐个依赖单独取版本，取不到记为未知
    try:
        module = importlib.import_module(module_name)
    except Exception:
        module = None
    version = getattr(module, "__version__", None)
    if isinstance(version, str) and version:
        return version
    try: # 打包产物里没有依赖的 dist-info，这一步会抛 PackageNotFoundError
        return metadata.version(dist_name)
    except Exception:
        return UNKNOWN_VERSION

def log_environment(root: object) -> None: # 启动时记录一次运行环境，供排查版本相关的问题
    logger.info("应用版本 %s | Python %s | 系统 %s", __version__, sys.version.split()[0], platform.platform())

    try:
        tk_patch = root.getvar("tk_patchLevel")
        tcl_patch = root.tk.call("info", "patchlevel")
        windowing_system = root.tk.call("tk", "windowingsystem")
    except Exception as e:
        logger.warning("无法读取 Tcl/Tk 版本信息：%s", e)
    else:
        logger.info("Tk %s | Tcl %s | windowingsystem %s", tk_patch, tcl_patch, windowing_system)

    dependencies = DEPENDENCIES + (WINDOWS_DEPENDENCIES if os_name == "Windows" else ())
    logger.info("依赖版本 %s", "、".join(f"{dist_name} {dependency_version(dist_name, module_name)}" for dist_name, module_name in dependencies))

@contextmanager
def log_duration(action_logger: logging.Logger, action: str, warn_ms: float = 0.0) -> Iterator[None]: # 记录一段操作的耗时，超过阈值按 WARNING 记
    started_at = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        if warn_ms and elapsed_ms >= warn_ms:
            action_logger.warning("%s 耗时 %.0f ms，超过 %.0f ms", action, elapsed_ms, warn_ms)
        else:
            action_logger.debug("%s 耗时 %.0f ms", action, elapsed_ms)
