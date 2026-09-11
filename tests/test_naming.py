# -*- coding: utf-8 -*-
"""文件名清洗与重名去重（B4）。"""

import os
import threading

import pytest

from tchmaterial_parser.core import naming


@pytest.fixture(autouse=True)
def _clear_reservations():
    naming.clear_reservations()
    yield
    naming.clear_reservations()


@pytest.mark.parametrize("raw, want", [
    ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
    ("义务教育教科书/英语", "义务教育教科书_英语"),
])
def test_invalid_characters_replaced(raw, want):
    assert naming.sanitize_filename(raw) == want


def test_control_characters_replaced():
    got = naming.sanitize_filename("a\x00b\tc\x1fd\ne")
    assert got == "a_b_c_d_e"
    assert not any(ord(c) < 32 for c in got)


@pytest.mark.parametrize("raw", ["../../etc/passwd", "..\\..\\windows\\system32", "/etc/shadow"])
def test_path_traversal_neutralised(raw, tmp_path):
    name = naming.sanitize_filename(raw)
    assert "/" not in name and "\\" not in name and os.sep not in name
    naming.assert_within(str(tmp_path), os.path.join(str(tmp_path), name + ".pdf"))


def test_assert_within_rejects_escape(tmp_path):
    with pytest.raises(ValueError):
        naming.assert_within(str(tmp_path), os.path.join(str(tmp_path), "..", "escaped.pdf"))


@pytest.mark.parametrize("raw, want", [
    ("CON", "_CON"), ("con", "_con"), ("NUL.pdf", "_NUL.pdf"),
    ("COM1", "_COM1"), ("LPT9", "_LPT9"), ("CONSOLE", "CONSOLE"),
])
def test_windows_reserved_names(raw, want):
    assert naming.sanitize_filename(raw) == want


@pytest.mark.parametrize("raw", ["", "   ", ".", "..", "...", None, "  ..  "])
def test_empty_titles_fall_back(raw):
    assert naming.sanitize_filename(raw) == "download"


@pytest.mark.parametrize("raw, want", [
    ("  名字  ", "名字"), ("名字...", "名字"), ("  .名字.  ", "名字"),
])
def test_surrounding_space_and_dots_stripped(raw, want):
    assert naming.sanitize_filename(raw) == want


def test_long_chinese_title_truncated_by_bytes():
    """200 个汉字 = 600 字节；限额是字节数，且不许切断多字节字符。"""
    got = naming.sanitize_filename("课" * 200)
    assert len(got.encode("utf-8")) <= naming.MAX_FILENAME_BYTES
    assert got.encode("utf-8").decode("utf-8") == got
    assert got == "课" * (naming.MAX_FILENAME_BYTES // 3)


@pytest.mark.parametrize("raw", [
    "a" * 199 + "课", "a" * 198 + "课", "ab" + "课" * 100, "课" * 66 + "ab",
])
def test_truncation_lands_on_character_boundary(raw):
    got = naming.sanitize_filename(raw)
    assert len(got.encode("utf-8")) <= naming.MAX_FILENAME_BYTES
    assert got.encode("utf-8").decode("utf-8") == got


def test_unique_path_three_times(tmp_path):
    got = [naming.unique_path(str(tmp_path), "英语三年级下册", ".pdf") for _ in range(3)]
    assert [os.path.basename(p) for p in got] == [
        "英语三年级下册.pdf", "英语三年级下册 (2).pdf", "英语三年级下册 (3).pdf"]


def test_existing_file_counts_as_taken(tmp_path):
    (tmp_path / "数学.pdf").write_text("x", encoding="utf-8")
    got = naming.unique_path(str(tmp_path), "数学", ".pdf")
    assert os.path.basename(got) == "数学 (2).pdf"


def test_concurrent_reservations_are_distinct(tmp_path):
    out, lock, barrier = [], threading.Lock(), threading.Barrier(8)

    def worker():
        barrier.wait()
        p = naming.unique_path(str(tmp_path), "同名教材", ".pdf")
        with lock:
            out.append(p)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(out)) == 8


def test_nineteen_identical_titles_do_not_collide(tmp_path):
    """实测单个列表文件内同一书名出现 19 次。"""
    title = "（根据2022年版课程标准修订）义务教育教科书·英语三年级下册"
    paths = [naming.unique_path(str(tmp_path), naming.sanitize_filename(title), ".pdf")
             for _ in range(19)]
    assert len(set(paths)) == 19


@pytest.mark.parametrize("raw, label", [
    ("课" * 66 + " 尾", "截断处露出空格"),
    ("a" * 199 + ".b", "截断处露出点"),
    ("课" * 66 + ".", "截断处露出点（中文）"),
])
def test_truncation_does_not_reexpose_trailing_space_or_dot(raw, label):
    """先 strip 再截断的话，截断会把末尾的空格或点重新露出来。

    Windows 会把它们静默吃掉，预留的名字和实际落盘的名字就对不上了——
    正是 B4 那一类问题。
    """
    got = naming.sanitize_filename(raw)
    assert not got.endswith(" "), "%s：%r" % (label, got[-5:])
    assert not got.endswith("."), "%s：%r" % (label, got[-5:])
    assert len(got.encode("utf-8")) <= naming.MAX_FILENAME_BYTES
    assert got == got.strip()


def test_string_that_becomes_empty_after_trimming_falls_back():
    assert naming.sanitize_filename("." * 300) == "download"
    assert naming.sanitize_filename(" " * 300) == "download"


@pytest.mark.parametrize("bad", [2024, True, 3.14, b"bytes", ["a"], {"x": 1}, object()])
def test_non_string_titles_do_not_raise(bad):
    """上游偶尔把 title 写成数字：for ch in 2024 会直接抛 TypeError。"""
    assert naming.sanitize_filename(bad) == "download"


# ---- PR 评审第 2 轮：大小写不敏感文件系统上的预留 ----

def test_reservations_collide_on_a_case_insensitive_filesystem(tmp_path, monkeypatch):
    """Windows 上 Teacher Guide.pdf 与 teacher guide.pdf 是同一个文件。

    预留集合按原样字符串去重的话，两个 worker 会各自以为独占，最终对着写
    同一个 .part。用 normcase 打桩模拟 Windows 语义，不依赖真实平台。
    """
    monkeypatch.setattr(naming.os.path, "normcase", str.lower)

    first = naming.unique_path(str(tmp_path), "Teacher Guide", ".pdf")
    second = naming.unique_path(str(tmp_path), "teacher guide", ".pdf")

    assert first.lower() != second.lower(), \
        "两个任务拿到了同一个文件（只差大小写）：%r / %r" % (
            os.path.basename(first), os.path.basename(second))
    assert os.path.basename(first) == "Teacher Guide.pdf", "对外返回的路径被改了大小写"
    assert os.path.basename(second) == "teacher guide (2).pdf"


def test_releasing_a_path_uses_the_same_key(tmp_path, monkeypatch):
    """归还也要按同一个键，否则预留永远清不掉。"""
    monkeypatch.setattr(naming.os.path, "normcase", str.lower)

    first = naming.unique_path(str(tmp_path), "Teacher Guide", ".pdf")
    naming.release_path(first)

    again = naming.unique_path(str(tmp_path), "Teacher Guide", ".pdf")
    assert again == first, "归还之后没能拿回原名：%r" % (os.path.basename(again),)


def test_reservation_key_is_identity_on_posix(tmp_path):
    """POSIX 上 normcase 是恒等：大小写不同就是不同的文件，不该被合并。"""
    if os.path.normcase("A") != "A":
        pytest.skip("这台机器上 normcase 不是恒等（Windows）")

    first = naming.unique_path(str(tmp_path), "Teacher Guide", ".pdf")
    second = naming.unique_path(str(tmp_path), "teacher guide", ".pdf")

    assert os.path.basename(first) == "Teacher Guide.pdf"
    assert os.path.basename(second) == "teacher guide.pdf", \
        "POSIX 上这是两个不同的文件，不该让号"
