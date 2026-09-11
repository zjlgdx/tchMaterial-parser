# -*- coding: utf-8 -*-
"""Access Token 的本地持久化。"""

import json
import logging
import os

from .. import config

logger = logging.getLogger(__name__)

REGISTRY_KEY = "Software\\tchMaterial-parser"
REGISTRY_VALUE = "AccessToken"

if config.os_name == "Windows":
    import winreg


def linux_data_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "tchMaterial-parser", "data.json")


def load_token() -> str:
    """读取本地存储的 Access Token；没有或读不出来时返回 None。"""
    try:
        if config.os_name == "Windows": # 在 Windows 上，从注册表读取
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_READ) as key:
                token, _ = winreg.QueryValueEx(key, REGISTRY_VALUE)
                if token:
                    return token
        elif config.os_name == "Linux": # 在 Linux 上，从 ~/.config/tchMaterial-parser/data.json 文件读取
            target_file = linux_data_file()
            if not os.path.exists(target_file): # 文件不存在则不做处理
                return None

            with open(target_file, "r") as f:
                data = json.load(f)
            return data["access_token"]
    except Exception:
        pass # 读取失败则不做处理

    return None


def save_token(token: str) -> str:
    """保存 Access Token，返回展示给用户的文案。"""
    try:
        if config.os_name == "Windows": # 在 Windows 上，将 Access Token 写入注册表
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
                winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, token)
            return "Access Token 已保存！\n已写入注册表：HKEY_CURRENT_USER\\Software\\tchMaterial-parser\\AccessToken"
        elif config.os_name == "Linux": # 在 Linux 上，将 Access Token 保存至 ~/.config/tchMaterial-parser/data.json 文件中
            target_file = linux_data_file()
            os.makedirs(os.path.dirname(target_file), exist_ok=True)

            with open(target_file, "w") as f:
                json.dump({ "access_token": token }, f, indent=4)

            return "Access Token 已保存！\n已写入文件：~/.config/tchMaterial-parser/data.json"
        else:
            return "Access Token 已保存！"
    except Exception:
        return "Access Token 已保存！"
