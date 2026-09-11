# -*- coding: utf-8 -*-
"""README 与代码对齐（C7）。

README 里承诺的东西，代码里得真的有；代码里没有的，README 里不许写。
"""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")
SRC = os.path.join(REPO_ROOT, "src", "tchmaterial_parser")


def readme():
    return open(README, encoding="utf-8").read()


def package_sources():
    out = []
    for dirpath, _dirs, files in os.walk(SRC):
        for name in files:
            if name.endswith(".py"):
                out.append(open(os.path.join(dirpath, name), encoding="utf-8").read())
    return "\n".join(out)


def test_no_pause_resume_claim():
    """代码里从来没有暂停/恢复功能。"""
    text = readme()
    assert "暂停" not in text and "恢复操作" not in text


def test_python_version_badge_matches_the_floor():
    text = readme()
    assert "Python-3.10" in text, "徽章没有写明 3.10+"
    assert "Python-3.x" not in text


def test_source_run_instructions_point_at_the_entry():
    text = readme()
    assert "src/tchMaterial-parser.pyw" in text
    assert "pip install -r requirements.txt" in text
    assert os.path.exists(os.path.join(REPO_ROOT, "src", "tchMaterial-parser.pyw"))


def test_macos_token_persistence_is_documented():
    """任务 15 之后 macOS 也会持久化，README 不能再说只存在内存里。"""
    text = readme()
    assert "临时存储于内存" not in text
    assert "Library/Application Support/tchMaterial-parser" in text
    assert "0600" in text


def test_tree_and_search_are_documented():
    """任务 12 换掉了下拉框，README 要描述当前的界面。"""
    text = readme()
    assert "教材目录" in text and "搜索" in text
    assert "选项卡" not in text and "下拉框" not in text


def test_deduplication_is_documented():
    text = readme()
    assert "(2)" in text or "（2）" in text, "没有说明重名教材如何处理"


def test_design_docs_are_linked():
    assert "docs/designs" in readme()
    assert os.path.isdir(os.path.join(REPO_ROOT, "docs", "designs"))


def test_old_design_draft_is_gone():
    """设计稿只留一份，不许两份并存。"""
    assert not os.path.exists(os.path.join(REPO_ROOT, "重构设计方案.md"))
    designs = os.listdir(os.path.join(REPO_ROOT, "docs", "designs"))
    assert designs == ["2026-09-11-hardening-and-restructure.md"], designs


def test_no_stale_version_number_in_readme():
    assert not re.search(r"v3\.1\b", readme())


def test_documented_token_paths_exist_in_code():
    sources = package_sources()
    assert "Library" in sources and "Application Support" in sources
    assert "Software\\\\tchMaterial-parser" in sources or "Software\\tchMaterial-parser" in sources
