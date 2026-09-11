# -*- coding: utf-8 -*-
"""Access Token 的持久化与文件权限（C5）。"""

import json
import os
import stat
import tempfile

import pytest

from tchmaterial_parser.core import tokens

WINDOWS = tokens.config.os_name == "Windows"
posix_only = pytest.mark.skipif(WINDOWS, reason="文件权限只在 POSIX 上有意义")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """把配置目录指到临时路径，绝不碰用户真实的 Token。"""
    cfg = tmp_path / "config"
    monkeypatch.setattr(tokens.config, "config_dir", lambda: str(cfg))
    monkeypatch.setattr(tokens.config, "legacy_linux_config_file",
                        lambda: str(tmp_path / "legacy" / "data.json"))
    return tmp_path


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@posix_only
def test_round_trip(home):
    message = tokens.save_token("tok-abc")
    assert "已保存" in message
    assert tokens.load_token() == "tok-abc"


@posix_only
def test_new_file_is_private(home):
    tokens.save_token("tok-abc")
    assert oct(mode_of(tokens.data_file())) == "0o600"


@posix_only
def test_directory_is_private(home):
    tokens.save_token("tok-abc")
    assert oct(mode_of(os.path.dirname(tokens.data_file()))) == "0o700"


@posix_only
def test_existing_world_readable_file_is_tightened(home):
    """面板采纳项：预置一个 0o644 的旧文件，写入后必须变成 0o600。"""
    target = tokens.data_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump({"access_token": "old"}, f)
    os.chmod(target, 0o644)
    assert oct(mode_of(target)) == "0o644"

    tokens.save_token("tok-new")

    assert oct(mode_of(target)) == "0o600"
    assert tokens.load_token() == "tok-new"


@posix_only
def test_target_never_holds_the_token_with_loose_permissions(home, monkeypatch):
    """写入过程中目标文件不得以宽松权限承载新 Token。

    在 os.replace 发生前拦一刀：此刻目标文件要么还是旧内容，要么不存在——
    新 Token 只存在于那个 0600 的临时文件里。
    """
    target = tokens.data_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump({"access_token": "old"}, f)
    os.chmod(target, 0o644)

    observed = {}
    real_replace = os.replace

    def spy_replace(src, dst):
        observed["tmp_mode"] = oct(mode_of(src))
        observed["tmp_holds_new"] = "tok-new" in open(src, encoding="utf-8").read()
        observed["target_content"] = open(dst, encoding="utf-8").read() if os.path.exists(dst) else ""
        observed["target_mode"] = oct(mode_of(dst)) if os.path.exists(dst) else None
        return real_replace(src, dst)

    monkeypatch.setattr(tokens.os, "replace", spy_replace)
    tokens.save_token("tok-new")

    assert observed["tmp_mode"] == "0o600"          # 临时文件一建出来就是私有的
    assert observed["tmp_holds_new"] is True        # 新 Token 只在临时文件里
    assert "tok-new" not in observed["target_content"]  # replace 之前目标里没有它
    assert oct(mode_of(target)) == "0o600"          # replace 之后目标是私有的


@posix_only
def test_legacy_path_is_still_readable(home, tmp_path):
    """v3.1 及以前写在 ~/.config 下的 Token 仍要读得出来。"""
    legacy = tmp_path / "legacy" / "data.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"access_token": "legacy-tok"}), encoding="utf-8")
    assert tokens.load_token() == "legacy-tok"


@posix_only
def test_new_path_wins_over_legacy(home, tmp_path):
    legacy = tmp_path / "legacy" / "data.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"access_token": "legacy-tok"}), encoding="utf-8")
    tokens.save_token("new-tok")
    assert tokens.load_token() == "new-tok"


def test_missing_token_returns_none(home):
    assert tokens.load_token() is None


@posix_only
def test_corrupted_file_is_ignored(home):
    target = tokens.data_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert tokens.load_token() is None


@posix_only
def test_write_failure_reports_the_truth(home, monkeypatch):
    """C5：写不进去就必须说写不进去，不许谎报成功。"""
    def boom(*args, **kwargs):
        raise OSError("只读文件系统")

    monkeypatch.setattr(tokens, "write_private_json", boom)
    message = tokens.save_token("tok-abc")

    assert "保存失败" in message
    assert "已保存" not in message
    assert "只读文件系统" in message


@posix_only
def test_no_temp_file_left_behind_on_failure(home, monkeypatch):
    target = tokens.data_file()
    real_replace = os.replace

    def boom_replace(src, dst):
        raise OSError("改名失败")

    monkeypatch.setattr(tokens.os, "replace", boom_replace)
    message = tokens.save_token("tok-abc")
    monkeypatch.setattr(tokens.os, "replace", real_replace)

    assert "保存失败" in message
    directory = os.path.dirname(target)
    assert [n for n in os.listdir(directory) if n.endswith(".tmp")] == []


@posix_only
def test_failure_prefix_is_a_shared_constant(home, monkeypatch):
    """界面靠这个前缀区分成功与失败，两处不能各写一份文案。"""
    def boom(*args, **kwargs):
        raise OSError("只读文件系统")

    monkeypatch.setattr(tokens, "write_private_json", boom)
    assert tokens.save_token("tok-abc").startswith(tokens.SAVE_FAILED_PREFIX)

    monkeypatch.undo()
    assert not tokens.save_token("tok-abc").startswith(tokens.SAVE_FAILED_PREFIX)


@posix_only
@pytest.mark.parametrize("content, label", [
    ("[]", "顶层是列表"),
    ('"just a string"', "顶层是字符串"),
    ("null", "顶层是 null"),
    ("123", "顶层是数字"),
    ('{"access_token": 123}', "token 字段不是字符串"),
    ('{"access_token": null}', "token 字段是 null"),
])
def test_structurally_broken_file_is_treated_as_unset(home, content, label):
    """这段跑在 tk.Tk() 之前：放任何异常出去，双击运行的用户连错误都看不到。"""
    target = tokens.data_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)

    assert tokens.load_token() is None, label


@posix_only
def test_broken_file_does_not_block_a_later_save(home):
    target = tokens.data_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write("[]")

    assert tokens.load_token() is None
    assert "已保存" in tokens.save_token("tok-new")
    assert tokens.load_token() == "tok-new"


# ---- PR 评审：残留的临时文件不许挡住此后每一次保存 ----

def record_temp_names(monkeypatch) -> list:
    """记下每次保存实际用的临时文件名。"""
    names = []
    original = tempfile.mkstemp

    def spy(*args, **kwargs):
        fd, name = original(*args, **kwargs)
        names.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    return names


def test_a_leftover_temp_file_does_not_block_saving(tmp_path):
    """进程在改名前崩过一次，之后每一次保存都不该失败。

    固定名 + O_EXCL 会让用户被钉死在旧 Token 上，只能自己去删那个文件。
    """
    target = str(tmp_path / "cfg" / "data.json")
    tokens.write_private_json(target, {"token": "tok-0"})

    leftover = target + ".tmp" # 上一次崩溃留下的
    with open(leftover, "w", encoding="utf-8") as f:
        f.write("半截内容")

    tokens.write_private_json(target, {"token": "tok-1"})

    with open(target, encoding="utf-8") as f:
        assert json.load(f)["token"] == "tok-1", "新 Token 没存进去"


def test_two_saves_do_not_use_the_same_temp_name(tmp_path, monkeypatch):
    """两个实例同时保存：固定名会撞，唯一名不会。"""
    target = str(tmp_path / "cfg" / "data.json")
    names = record_temp_names(monkeypatch)

    tokens.write_private_json(target, {"token": "tok-0"})
    tokens.write_private_json(target, {"token": "tok-1"})

    assert len(set(names)) == 2, "两次保存用了同一个临时文件名：%s" % names


@posix_only
def test_the_temp_file_is_private_and_gets_renamed_away(tmp_path, monkeypatch):
    """临时文件一建出来就得是 0600——Token 在改名之前就已经写进去了。"""
    target = str(tmp_path / "cfg" / "data.json")
    names = record_temp_names(monkeypatch)

    tokens.write_private_json(target, {"token": "tok-0"})

    assert oct(mode_of(target)) == "0o600"
    assert not os.path.exists(names[0]), "临时文件没被改名走"
