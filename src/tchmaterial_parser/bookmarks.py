# -*- coding: utf-8 -*-
# 为下载好的 PDF 写入章节书签

import os, shutil, tempfile

from pypdf import PdfReader, PdfWriter

from .platform_utils import print_error

def add_bookmarks(pdf_path: str, chapters: list[dict]) -> None: # 给 PDF 添加书签
    if not chapters:
        return

    # 写到同目录的临时文件，整份写成功之后再原子替换掉原文件：写到一半失败（磁盘满、pypdf 报错等）
    # 时只损失这个临时文件，原 PDF 逐字节不变，交付出去的仍是一份完好的、只是没有书签的 PDF。
    # 同目录是为了与原文件处在同一文件系统，os.replace 才是原子的。
    directory = os.path.dirname(pdf_path) or "." # pdf_path 不含目录时（相对路径）退回当前目录
    temp_path = None
    try:
        with open(pdf_path, "rb") as source: # 由本函数持有并关闭读句柄：Windows 上对仍有打开句柄的文件执行 os.replace 会抛 PermissionError
            reader = PdfReader(source)
            writer = PdfWriter()
            writer.append_pages_from_reader(reader)

            def add_chapter(chapter_list: list[dict], parent=None): # 递归添加书签的内部函数
                for chapter in chapter_list:
                    title: str = chapter.get("title", "未知章节")
                    p_index: int | None = chapter.get("page_index")
                    if p_index is None: # 如果值为 None 或者不存在，跳过这个书签
                        print_error(ValueError(f"章节 “{title}” 的页码索引无效，已跳过此处书签添加"))
                        continue

                    try: # 尝试将其转为整数并减 1（pypdf 页码从 0 开始)
                        page_num: int = int(p_index) - 1
                    except (ValueError, TypeError) as e: # 如果转换失败，跳过这个书签
                        print_error(e)
                        continue

                    if page_num < 0 or page_num >= len(writer.pages):
                        continue

                    # 添加书签，其中 parent 是父级书签对象，用于处理多级目录
                    bookmark = writer.add_outline_item(title, page_num, parent=parent)

                    # 如果有子章节（children），递归添加
                    if chapter.get("children"):
                        add_chapter(chapter["children"], parent=bookmark)

            # 开始处理章节数据
            add_chapter(chapters)

            # 保存到临时文件；writer 写出时仍会按需读取 reader 的数据流，因此不能提前关闭源文件。
            # 临时名交给 mkstemp 生成：长度有界且唯一。不能用「pdf_path 再接一段后缀」——教材名
            # 本就长（ext4 的 NAME_MAX 是 255 *字节*，一个中文占 3 字节），额外的后缀会撞
            # ENAMETOOLONG，异常被下面吞掉，书签就这么静默丢了；也不能用「截断原名再加后缀」——
            # 教材常是只在结尾不同的系列名（第一册/第二册），截断切掉的正是结尾，并发下载会撞名互相覆盖
            fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".bookmark-", suffix=".tmp")
            with os.fdopen(fd, "wb") as f: # 接管 mkstemp 的文件描述符，由 with 负责关闭，不泄漏
                writer.write(f)

        # mkstemp 建出来的文件是 0600，而原文件通常是 0644：不复制权限位的话，交付给用户的 PDF
        # 权限会悄悄收紧，共享下载目录里别的账号就读不到了（Windows 上这一步等价于无操作）
        shutil.copymode(pdf_path, temp_path)
        os.replace(temp_path, pdf_path) # 源文件的读句柄已随上面的 with 关闭，这里才能安全替换

    except Exception as e:
        # 书签是尽力而为的附加物：失败时原 PDF 完好无损，照常交付一份没有书签的 PDF，不向上抛，
        # 以免调用方把一份已经完整下载好的文件当成下载失败而整个丢弃
        print_error(e)
        if temp_path is not None:
            try: # 尽力清掉半成品；清理本身失败也不能抛出来盖掉上面的真实错误
                os.remove(temp_path)
            except Exception:
                pass
