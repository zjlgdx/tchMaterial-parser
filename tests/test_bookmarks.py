# -*- coding: utf-8 -*-

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch
import builtins
import io
import os
import stat
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter

from src.tchmaterial_parser import bookmarks


def make_pdf(path: str, pages: int = 5) -> bytes: # 现造一份真 PDF（若干空白页），不引入测试固件文件
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as file:
        writer.write(file)
    return Path(path).read_bytes()


def outline_titles(path: str) -> list: # 把 PdfReader 读回的大纲整理成 [标题, [子标题, ...], ...] 形式，便于断言层级
    def walk(items: list) -> list:
        result = []
        for item in items:
            if isinstance(item, list):
                result.append(walk(item))
            else:
                result.append(str(item.title))
        return result

    with open(path, "rb") as file:
        return walk(list(PdfReader(file).outline))


class AddBookmarksTest(unittest.TestCase):
    """add_bookmarks 的核心契约：写成功才替换原文件，失败时原 PDF 逐字节不变、不向上抛、不留临时文件。"""

    def setUp(self) -> None:
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        self.root_directory.mkdir(exist_ok=True)
        self.tmp_dir = self.context.enter_context(tempfile.TemporaryDirectory(dir=self.root_directory))
        # 真实调用方传进来的就是下载中的临时文件 xxx.pdf.tmp，这里照搬这个命名，
        # 确保本函数自己的临时文件不会和它撞名
        self.pdf_path = str(Path(self.tmp_dir) / "book.pdf.tmp")
        self.chapters = [
            {"title": "第一章", "page_index": 1, "children": [
                {"title": "第一节", "page_index": 2},
                {"title": "第二节", "page_index": 3},
            ]},
            {"title": "第二章", "page_index": 4},
        ]

    def directory_entries(self) -> set: # 目录里现有的文件名，用来断言没有残留临时文件
        return {entry.name for entry in Path(self.tmp_dir).iterdir()}

    def test_bookmarks_are_written_without_changing_the_pages(self) -> None:
        # 正常路径：页数不变、大纲能被 PdfReader 读回、父子层级正确，且不留下临时文件。
        original = make_pdf(self.pdf_path)

        bookmarks.add_bookmarks(self.pdf_path, self.chapters)

        with open(self.pdf_path, "rb") as file:
            self.assertEqual(len(PdfReader(file).pages), 5) # 页数不变
        self.assertEqual(outline_titles(self.pdf_path), ["第一章", ["第一节", "第二节"], "第二章"])
        self.assertNotEqual(Path(self.pdf_path).read_bytes(), original) # 确实写进去了，不是原地没动
        self.assertEqual(self.directory_entries(), {"book.pdf.tmp"}) # 没有残留临时文件

    def test_failure_midway_through_writing_leaves_the_original_pdf_untouched(self) -> None:
        # 本次修复的核心断言：写到一半失败（磁盘满、pypdf 内部报错等）时，原 PDF 必须逐字节不变。
        # 就地 open(pdf_path, "wb") 会在这一刻把原文件截断成半截，而外层又吞掉异常，
        # 调用方随后照常改名交付，于是一个损坏的 PDF 被当作“下载成功”送到用户手里。
        original = make_pdf(self.pdf_path)

        def failing_write(self_writer, stream) -> None: # 先写出若干字节，再抛异常
            stream.write(b"%PDF-1.7\n" + b"0" * 4096)
            stream.flush()
            raise OSError("No space left on device")

        with patch.object(bookmarks.PdfWriter, "write", failing_write):
            bookmarks.add_bookmarks(self.pdf_path, self.chapters) # 不得向上抛异常

        self.assertEqual(Path(self.pdf_path).read_bytes(), original) # 原 PDF 逐字节不变
        with open(self.pdf_path, "rb") as file:
            self.assertEqual(len(PdfReader(file).pages), 5) # 仍是一份能打开的完好 PDF
        self.assertEqual(self.directory_entries(), {"book.pdf.tmp"}) # 半成品临时文件必须被清掉

    def test_failure_while_reading_leaves_the_file_untouched(self) -> None:
        # 失败发生在写之前（文件根本不是 PDF，PdfReader 解析失败）：文件本就没被碰过，
        # 现有“吞掉异常、交付一份没有书签的文件”的行为保持不变。
        Path(self.pdf_path).write_bytes(b"this is definitely not a pdf")

        bookmarks.add_bookmarks(self.pdf_path, self.chapters) # 不得向上抛异常

        self.assertEqual(Path(self.pdf_path).read_bytes(), b"this is definitely not a pdf")
        self.assertEqual(self.directory_entries(), {"book.pdf.tmp"})

    def test_empty_chapters_returns_without_touching_the_file(self) -> None:
        # 没有章节时直接返回：不读、不写，文件一个字节都不碰。
        original = make_pdf(self.pdf_path)

        with patch.object(bookmarks, "PdfReader", Mock()) as reader, \
             patch.object(bookmarks.os, "replace", Mock()) as replace:
            bookmarks.add_bookmarks(self.pdf_path, [])

        reader.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(Path(self.pdf_path).read_bytes(), original)
        self.assertEqual(self.directory_entries(), {"book.pdf.tmp"})

    def test_source_handle_is_closed_before_replacing_the_original(self) -> None:
        # 跨平台约束：Windows 上对一个仍有打开句柄的文件执行 os.replace 会抛 PermissionError，
        # 于是书签永远写不进去。macOS/Linux 不会报错，这条差异在本机跑不出来，只能直接把
        # “替换发生时源文件的读句柄必须已经关闭”这条规则本身钉住。
        make_pdf(self.pdf_path)
        real_open, real_replace = builtins.open, os.replace
        source_handles, closed_at_replace = [], []

        def recording_open(file, mode="r", *args, **kwargs): # 记下所有对 pdf_path 的读句柄
            handle = real_open(file, mode, *args, **kwargs)
            if isinstance(file, (str, os.PathLike)) and os.fspath(file) == self.pdf_path and "r" in mode:
                source_handles.append(handle)
            return handle

        def checking_replace(src, dst): # 替换发生的这一刻，逐个记下它们是否已经关闭
            closed_at_replace.extend(handle.closed for handle in source_handles)
            real_replace(src, dst)

        with patch.object(builtins, "open", recording_open), \
             patch.object(bookmarks.os, "replace", checking_replace):
            bookmarks.add_bookmarks(self.pdf_path, self.chapters)

        self.assertTrue(source_handles, "根本没读过源文件，测试前置条件不成立")
        self.assertTrue(closed_at_replace, "os.replace 没被调用，说明根本没走原子替换")
        self.assertTrue(all(closed_at_replace), "os.replace 时源文件的读句柄还开着，Windows 上会抛 PermissionError")
        self.assertEqual(outline_titles(self.pdf_path), ["第一章", ["第一节", "第二节"], "第二章"])

    def temp_names_next_to(self, pdf_path: str, chapters: list) -> list:
        # 写出的那一刻，目标目录里多出来的文件名。不去打桩 mkstemp 之类的具体实现，只看
        # 文件系统上真实发生了什么：临时文件必须就落在目标旁边，成功后又必须消失。
        directory = os.path.dirname(pdf_path)
        before = set(os.listdir(directory))
        seen = []
        real_write = bookmarks.PdfWriter.write

        def snapshotting_write(self_writer, stream):
            seen.extend(sorted(set(os.listdir(directory)) - before))
            return real_write(self_writer, stream)

        with patch.object(bookmarks.PdfWriter, "write", snapshotting_write):
            bookmarks.add_bookmarks(pdf_path, chapters)

        self.assertTrue(seen, "写出时目标目录里没有多出临时文件：要么就地覆盖了原文件，要么临时文件建到了别的目录")
        return seen

    def test_temporary_file_lives_next_to_the_target(self) -> None:
        # 临时文件必须与目标同目录：跨文件系统时 os.replace 会抛 OSError，原子替换无从谈起。
        make_pdf(self.pdf_path)

        for name in self.temp_names_next_to(self.pdf_path, self.chapters):
            self.assertNotEqual(name, os.path.basename(self.pdf_path), "仍在就地覆盖原文件")

    def test_temporary_file_name_length_is_bounded_regardless_of_the_target_name(self) -> None:
        # 临时文件名的长度必须与目标文件名无关。ext4 的 NAME_MAX 是 255 *字节*，一个中文占
        # 3 字节，教材名本就逼近这个上限；若临时名是“目标名再接一段后缀”，就会撞
        # ENAMETOOLONG，异常被吞掉，书签静默丢失，而下载本身看起来一切正常。
        # （macOS 的 APFS 按字符而非字节算上限，这个场景在本机复现不出来，所以这里不去等
        # 一个 OSError，而是直接断言“临时名的字节长度有上界”这条规则本身。）
        suffix = "第一册.pdf.tmp"
        long_name = ("人民教育出版社义务教育教科书" * 10)[:(245 - len(suffix.encode("utf-8"))) // 3] + suffix
        self.assertGreater(len(long_name.encode("utf-8")), 240, "名字不够长，测试前置条件不成立") # 逼近 NAME_MAX
        long_pdf_path = str(Path(self.tmp_dir) / long_name)
        make_pdf(long_pdf_path)

        for name in self.temp_names_next_to(long_pdf_path, self.chapters):
            # 不断言具体名字（那会把实现细节焊死），只断言它的字节数有个远低于 NAME_MAX 的上界
            self.assertLessEqual(len(name.encode("utf-8")), 64)

        self.assertEqual(outline_titles(long_pdf_path), ["第一章", ["第一节", "第二节"], "第二章"]) # 书签确实写进去了

    @unittest.skipIf(os.name == "nt", "Windows 的 os.chmod 只认只读位，POSIX 权限位在那里没有对应语义")
    def test_permission_bits_of_the_original_file_are_preserved(self) -> None:
        # mkstemp 建出来的文件是 0600，直接 os.replace 过去会让交付给用户的 PDF 权限从 0644
        # 悄悄收紧成 0600——共享下载目录里别的账号就读不到了。替换前必须把原文件的权限位复制过来。
        make_pdf(self.pdf_path)
        os.chmod(self.pdf_path, 0o644)

        bookmarks.add_bookmarks(self.pdf_path, self.chapters)

        self.assertEqual(stat.S_IMODE(os.stat(self.pdf_path).st_mode), 0o644)
        self.assertEqual(outline_titles(self.pdf_path), ["第一章", ["第一节", "第二节"], "第二章"]) # 确实写成功了才谈得上权限

    def test_invalid_chapters_are_skipped_without_failing_the_whole_file(self) -> None:
        # 逐个跳过无效章节的既有行为保持不变：页码缺失/非法/越界的条目被跳过，其余照常写入。
        make_pdf(self.pdf_path, pages=3)
        chapters = [
            {"title": "缺页码"},
            {"title": "页码非法", "page_index": "第三页"},
            {"title": "页码越界", "page_index": 99},
            {"title": "正常章节", "page_index": 2},
        ]

        with patch.object(bookmarks, "print_error"): # 跳过时会打印错误，这里不关心输出
            bookmarks.add_bookmarks(self.pdf_path, chapters)

        self.assertEqual(outline_titles(self.pdf_path), ["正常章节"])
        self.assertEqual(self.directory_entries(), {"book.pdf.tmp"})

    def test_cleanup_failure_does_not_escape(self) -> None:
        # 清理临时文件本身也可能失败（例如被杀软占用）：不能让清理的异常盖掉原始错误、抛给调用方。
        original = make_pdf(self.pdf_path)

        def failing_write(self_writer, stream) -> None:
            raise OSError("No space left on device")

        with patch.object(bookmarks.PdfWriter, "write", failing_write), \
             patch.object(bookmarks.os, "remove", side_effect=PermissionError("临时文件被占用")):
            bookmarks.add_bookmarks(self.pdf_path, self.chapters) # 不得向上抛异常

        self.assertEqual(Path(self.pdf_path).read_bytes(), original)


class AddBookmarksStreamTest(unittest.TestCase):
    """writer.write 拿到的必须是一个真正可写的流，而不是路径字符串等其他东西。"""

    def test_writer_receives_a_writable_binary_stream(self) -> None:
        root_directory = Path(__file__).resolve().parents[1] / ".tmp"
        root_directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root_directory) as tmp_dir:
            pdf_path = str(Path(tmp_dir) / "book.pdf.tmp")
            make_pdf(pdf_path)
            received = []

            real_write = bookmarks.PdfWriter.write

            def recording_write(self_writer, stream): # 流在函数返回后就会被关闭，只能当场记下它的状态
                received.append((isinstance(stream, io.IOBase), stream.writable()))
                return real_write(self_writer, stream)

            with patch.object(bookmarks.PdfWriter, "write", recording_write):
                bookmarks.add_bookmarks(pdf_path, [{"title": "第一章", "page_index": 1}])

            self.assertEqual(received, [(True, True)])


if __name__ == "__main__":
    unittest.main()
