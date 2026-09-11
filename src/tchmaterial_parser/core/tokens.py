# -*- coding: utf-8 -*-
"""Access Token 的本地持久化。"""

import json
import logging
import os

from .. import config

logger = logging.getLogger(__name__)

REGISTRY_KEY = "Software\\tchMaterial-parser"
REGISTRY_VALUE = "AccessToken"
DATA_FILENAME = "data.json"
SAVE_FAILED_PREFIX = "Access Token 保存失败："
FILE_MODE = 0o600 # Token 是凭据，同机其他用户不该读得到
DIR_MODE = 0o700

if config.os_name == "Windows":
    import winreg


def data_file() -> str:
    return os.path.join(config.config_dir(), DATA_FILENAME)


def candidate_files() -> list:
    """按优先级列出可能存有 Token 的文件。"""
    paths = [data_file()]
    legacy = config.legacy_linux_config_file() # 旧版本固定写在这里
    if legacy not in paths:
        paths.append(legacy)
    return paths


def load_token() -> str:
    """读取本地存储的 Access Token；没有或读不出来时返回 None。"""
    try:
        if config.os_name == "Windows": # 在 Windows 上，从注册表读取
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ) as key:
                token, _ = winreg.QueryValueEx(key, REGISTRY_VALUE)
                return token or None
    except FileNotFoundError:
        return None # 注册表项不存在，属于「还没设置过」
    except OSError:
        logger.warning("从注册表读取 Access Token 失败", exc_info=True)
        return None

    for path in candidate_files():
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 「JSON 合法但结构不对」和「文件读不出来」是同一档：都按没有 Token 处理。
            # 这段跑在 tk.Tk() 之前，放任何异常出去，双击运行的用户连错误都看不到
            if not isinstance(data, dict):
                raise ValueError("配置文件的顶层不是对象")
            token = data.get("access_token")
            if isinstance(token, str) and token:
                return token
        except (OSError, ValueError, TypeError):
            logger.warning("读取 %s 里的 Access Token 失败，按未设置处理", path, exc_info=True)

    return None


def write_private_json(path: str, payload: dict) -> None:
    """把 JSON 写进一个只有本人可读的文件。

    先写同目录的临时文件再改名：os.open 的 mode 只对新建文件生效，直接覆盖一个
    已存在的 0644 文件会让 Token 先以宽松权限落盘，进程若在收紧权限前中断，
    那个宽松权限还会一直留着。os.replace 保留的是临时文件的权限位，
    目标文件因此从不以宽松权限承载 Token。
    """
    os.makedirs(os.path.dirname(path), mode=DIR_MODE, exist_ok=True)

    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        raise


def save_token(token: str) -> str:
    """保存 Access Token，返回可直接展示给用户的真实结果。"""
    try:
        if config.os_name == "Windows": # 在 Windows 上，将 Access Token 写入注册表
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
                winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, token)
            return f"Access Token 已保存！\n已写入注册表：HKEY_CURRENT_USER\\{REGISTRY_KEY}\\{REGISTRY_VALUE}"

        target = data_file()
        write_private_json(target, { "access_token": token })
        return f"Access Token 已保存！\n已写入文件：{target}"
    except Exception as e:
        logger.error("保存 Access Token 失败", exc_info=True)
        return f"{SAVE_FAILED_PREFIX}{e}\n本次运行仍可使用，重启后需要重新输入。"
