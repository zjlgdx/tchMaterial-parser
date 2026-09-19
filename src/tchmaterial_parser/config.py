# -*- coding: utf-8 -*-
# 本地配置的读写（Windows 用注册表，其余平台用 JSON 文件）与登录凭据的维护
#
# 鉴权相关三项：
# - access_token：X-ND-AUTH 的 MAC id，也用于 Authorization: Bearer
# - mac_key：官网 HMAC 密钥；没有它就只能生成占位头
# - token_diff：官网 Fe(diff) 的时钟差（毫秒），只影响 nonce 时间戳
# 不要把 refresh_token 写入配置。旧用户可能只有 AccessToken 注册表值，加载时 mac_key 为空是正常的。

import json, os
from pathlib import Path

from .auth import TokenCredentials, parse_token_input
from .network import headers
from .platform_utils import os_name, print_error, winreg

access_token: str | None = None
mac_key: str | None = None
token_diff: int = 0 # 与 UC Token JSON 的 diff 对应，单位毫秒

REGISTRY_PATH = "Software\\tchMaterial-parser" # Windows 下存放配置的注册表键
CONFIG_FILE_MODE = 0o600 # 配置文件保存着 Access Token，只允许文件所有者读写
CONFIG_KEYS = { # 配置项名称到注册表值名称的映射（JSON 文件直接使用配置项名称）
    "access_token": "AccessToken",
    "mac_key": "MacKey",
    "token_diff": "TokenDiff",
    "theme": "Theme",
}

def config_file_path() -> Path | None: # 获取配置文件路径
    if os_name == "Windows": # 在 Windows 上，配置存放于 %LOCALAPPDATA%\tchMaterial-parser\data.json（此处为备用）
        return Path(
            os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local",
            "tchMaterial-parser",
            "data.json",
        )
    elif os_name in ("Linux", "Android"): # 在 Linux 上，配置存放于 ~/.config/tchMaterial-parser/data.json
        return Path.home() / ".config" / "tchMaterial-parser" / "data.json"
    elif os_name == "Darwin": # 在 macOS 上，配置存放于 ~/Library/Application Support/tchMaterial-parser/data.json
        return Path.home() / "Library" / "Application Support" / "tchMaterial-parser" / "data.json"

def catalog_cache_path() -> Path | None: # 获取资源目录缓存的文件路径，与配置文件放在同一目录下
    config_path = config_file_path()
    return config_path.with_name("catalog-cache.json.gz") if config_path else None

def log_dir_path() -> Path | None: # 获取日志目录，与配置文件放在同一目录下（Windows 的配置在注册表，此处仍用那个备用目录）
    config_path = config_file_path()
    return config_path.with_name("logs") if config_path else None

def config_location() -> str: # 获取配置存放位置的描述文本，用于提示用户
    if os_name == "Windows":
        return f"已写入注册表：HKEY_CURRENT_USER\\{REGISTRY_PATH}"
    elif os_name in ("Linux", "Android"):
        return "已保存至文件：~/.config/tchMaterial-parser/data.json"
    elif os_name == "Darwin":
        return "已保存至文件：~/Library/Application Support/tchMaterial-parser/data.json"
    else:
        return "本工具尚未支持该操作系统下 Access Token 的持久化，下次启动时仍需手动输入 Access Token。"

def restrict_config_file(target_file: Path) -> None: # 尽力把配置文件权限收紧到 0600，失败时静默忽略
    if os_name == "Windows": # Windows 的配置存放于注册表，且 chmod 只能改只读位
        return
    try:
        current = os.stat(target_file).st_mode & 0o777
        desired = current & CONFIG_FILE_MODE # 只去掉不允许的权限位，不添新位，例如 0444 收紧为 0400
        if desired != current:
            os.chmod(target_file, desired)
    except OSError: # 只读文件系统、文件不属于当前用户等情况下收紧会失败，不能影响读取配置
        pass

def load_config() -> dict[str, str]: # 读取本地存储的配置
    config: dict[str, str] = {}

    if os_name == "Windows": # 在 Windows 上，从注册表读取
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_READ) as key:
                for name, value_name in CONFIG_KEYS.items():
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except FileNotFoundError: # 该配置项尚未写入
                        continue
                    if not isinstance(value, str):
                        print_error(TypeError(f"配置项 {name} 必须是字符串"))
                        continue
                    config[name] = value
            return config
        except FileNotFoundError: # 注册表键不存在，即从未保存过配置
            return {}
        except Exception as e:
            print_error(e)
            return {}

    try:
        target_file = config_file_path() # 在其他平台上，从 JSON 文件读取
        if not target_file or not os.path.exists(target_file): # 文件不存在表示尚未保存过配置
            return {}
        restrict_config_file(target_file) # 旧版本创建的配置文件可能是 0644，读取时顺带收紧
        with open(target_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print_error(TypeError("配置文件的根节点必须是对象"))
            return {}
        for name in CONFIG_KEYS:
            if name not in data:
                continue
            value = data[name]
            if not isinstance(value, str):
                print_error(TypeError(f"配置项 {name} 必须是字符串"))
                continue
            config[name] = value
        return config
    except Exception as e:
        print_error(e)
        return {}

def save_config(**updates: str) -> None: # 保存配置，并与已有配置合并
    if os_name == "Windows": # 在 Windows 上，写入注册表
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            for name, value in updates.items():
                winreg.SetValueEx(key, CONFIG_KEYS[name], 0, winreg.REG_SZ, value)
        return

    target_file = config_file_path() # 在其他平台上，写入 JSON 文件
    data = load_config() # 先读取已有配置，避免覆盖其他配置项
    data.update(updates)
    os.makedirs(os.path.dirname(target_file), exist_ok=True)
    # 新建时就以 0600 落盘，避免写入 Access Token 后才收紧权限而留下窗口期
    fd = os.open(target_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CONFIG_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    restrict_config_file(target_file) # os.open 的权限参数只在创建文件时生效，已存在的旧文件需显式收紧

def apply_static_headers() -> None:
    """更新全局占位头。私有下载不要用这份 X-ND-AUTH，应走 network.request_headers。"""
    headers["Authorization"] = f"Bearer {access_token or '0'}"
    headers["X-ND-AUTH"] = f'MAC id="{access_token or "0"}",nonce="0",mac="0"'

def apply_credentials(credentials: TokenCredentials) -> None:
    """写入内存中的凭据并刷新占位头。空 access_token 视为未登录。"""
    global access_token, mac_key, token_diff
    access_token = credentials.access_token or None
    mac_key = credentials.mac_key
    token_diff = credentials.diff
    apply_static_headers()

def load_access_token(config: dict[str, str]) -> None: # 从已读取的配置中加载登录凭据
    token = config.get("access_token") or ""
    stored_mac = config.get("mac_key") or ""
    try:
        stored_diff = int(config.get("token_diff") or 0)
    except ValueError:
        stored_diff = 0
    apply_credentials(TokenCredentials(token, stored_mac or None, stored_diff))

def set_access_token(raw: str) -> str: # 校验三项 JSON 并保存；空内容则清除已保存的登录凭据
    credentials = parse_token_input(raw)
    apply_credentials(credentials)
    save_config(
        access_token=credentials.access_token,
        mac_key=credentials.mac_key or "",
        token_diff=str(credentials.diff),
    )
    if not credentials.access_token:
        return f"登录凭据已清除。\n{config_location()}"
    return f"登录凭据已保存！\n{config_location()}"
