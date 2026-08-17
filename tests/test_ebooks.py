#!/usr/bin/env python3
"""
test_ebooks.py - 测试 EPUB 和 MOBI/AZW3 解析引擎
"""

import os
import sys
import zipfile
import tempfile
import struct

# 添加 scripts 目录到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from epub_parser import EpubBook
from mobi_parser import MobiBook, decompress_palmdoc


def test_epub():
    print("=== Testing EPUB Engine ===")
    tmp_epub = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    with zipfile.ZipFile(tmp_epub.name, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '   <rootfiles>\n'
            '      <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '   </rootfiles>\n'
            '</container>',
        )
        z.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="BookId" version="2.0">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            '    <dc:title>深入理解计算机系统 (CSAPP)</dc:title>\n'
            '    <dc:creator>Randal E. Bryant</dc:creator>\n'
            '    <dc:language>zh-CN</dc:language>\n'
            '  </metadata>\n'
            '  <manifest>\n'
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            '    <item id="ch1" href="Text/ch1.xhtml" media-type="application/xhtml+xml"/>\n'
            '    <item id="ch2" href="Text/ch2.xhtml" media-type="application/xhtml+xml"/>\n'
            '  </manifest>\n'
            '  <spine toc="ncx">\n'
            '    <itemref idref="ch1"/>\n'
            '    <itemref idref="ch2"/>\n'
            '  </spine>\n'
            '</package>',
        )
        z.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            '  <navMap>\n'
            '    <navPoint id="navPoint-1" playOrder="1">\n'
            '      <navLabel><text>第1章 计算机系统漫游</text></navLabel>\n'
            '      <content src="Text/ch1.xhtml"/>\n'
            '    </navPoint>\n'
            '    <navPoint id="navPoint-2" playOrder="2">\n'
            '      <navLabel><text>第2章 信息的表示和处理</text></navLabel>\n'
            '      <content src="Text/ch2.xhtml"/>\n'
            '    </navPoint>\n'
            '  </navMap>\n'
            '</ncx>',
        )
        z.writestr(
            "OEBPS/Text/ch1.xhtml",
            '<!DOCTYPE html>\n'
            '<html><head><title>第1章 计算机系统漫游</title></head><body>\n'
            '  <h1>第1章 计算机系统漫游</h1>\n'
            '  <p>计算机系统是由硬件和系统软件组成的。</p>\n'
            '  <table>\n'
            '    <tr><th>阶段</th><th>输出</th></tr>\n'
            '    <tr><td>预处理</td><td>hello.i</td></tr>\n'
            '    <tr><td>编译</td><td>hello.s</td></tr>\n'
            '  </table>\n'
            '</body></html>',
        )
        z.writestr(
            "OEBPS/Text/ch2.xhtml",
            '<!DOCTYPE html>\n'
            '<html><head><title>第2章 信息的表示和处理</title></head><body>\n'
            '  <h1>第2章 信息的表示和处理</h1>\n'
            '  <p>现代计算机存储和处理信息以二值信号表示。</p>\n'
            '  <pre><code>int main() {\n    printf("Hello, CSAPP!");\n    return 0;\n}</code></pre>\n'
            '</body></html>',
        )
    tmp_epub.close()

    try:
        with EpubBook(tmp_epub.name) as book:
            info = book.get_probe_info()
            assert info["title"] == "深入理解计算机系统 (CSAPP)", f"Title mismatch: {info['title']}"
            assert info["author"] == "Randal E. Bryant", f"Author mismatch: {info['author']}"
            assert len(book.toc) == 2, f"TOC mismatch: {len(book.toc)}"

            ch1 = book.find_chapter("第1章")
            assert ch1 is not None, "Chapter 1 not found"
            md1 = book.extract_chapter_markdown(ch1)
            print("Extracted Chapter 1 Markdown:")
            print(md1)
            assert "hello.i" in md1
            assert "hello.s" in md1

            ch2 = book.find_chapter("第2章")
            assert ch2 is not None, "Chapter 2 not found"
            md2 = book.extract_chapter_markdown(ch2)
            print("\nExtracted Chapter 2 Markdown:")
            print(md2)
            assert "Hello, CSAPP!" in md2
            print("\n[OK] EPUB tests passed successfully!")
    finally:
        if os.path.exists(tmp_epub.name):
            os.remove(tmp_epub.name)


def test_mobi():
    print("\n=== Testing MOBI Engine ===")
    # 构造一个极简标准 MOBI 文件 (Palm Database + Mobi Header + PalmDOC HTML)
    html_content = (
        '<html><body>\n'
        '<h1>第1章 Python编程基础</h1>\n'
        '<p>Python 是一种面向对象、解释型计算机程序设计语言。</p>\n'
        '<h1>第2章 数据结构与算法</h1>\n'
        '<p>列表、字典、集合是 Python 中常用的内置数据结构。</p>\n'
        '</body></html>'
    ).encode("utf-8")

    # 构建 record 0 (Mobi Header)
    # PalmDOC header: comp=1(none), pad=0, text_len=len(html), rec_count=1, rec_size=4096
    palmdoc_hdr = struct.pack(">HHIHH", 1, 0, len(html_content), 1, 4096)
    mobi_id = b"MOBI"
    mobi_hdr_len = 232
    mobi_type = 2
    codepage = 65001  # utf-8
    unique_id = 123456
    file_version = 6
    title_str = "Python编程实战指南".encode("utf-8")
    title_offset = 248  # rec0 offset where title is written
    title_len = len(title_str)

    mobi_hdr_rest = struct.pack(
        ">IIIIII",
        mobi_hdr_len,
        mobi_type,
        codepage,
        unique_id,
        file_version,
        0xFFFFFFFF,  # ortographic index
    )

    # 填充到 title_offset
    rec0 = palmdoc_hdr + mobi_id + mobi_hdr_rest
    # pad to 84 bytes for title offset/len
    if len(rec0) < 84:
        rec0 += b"\x00" * (84 - len(rec0))
    rec0 += struct.pack(">II", title_offset, title_len)
    if len(rec0) < title_offset:
        rec0 += b"\x00" * (title_offset - len(rec0))
    rec0 += title_str

    # 构建 PDB header + record offsets
    # total records = 2 (rec 0 + rec 1)
    rec0_offset = 78 + (2 * 8) + 2  # 78 + 16 + 2 gap = 96
    rec1_offset = rec0_offset + len(rec0)
    pdb_name = b"Python_Book\x00" + b"\x00" * (32 - 12)
    pdb_hdr = pdb_name + struct.pack(
        ">HHIIIIII4s4sIIH",
        0, 0, 0, 0, 0, 0, 0, 0,
        b"BOOK", b"MOBI",
        0, 0, 2  # num_records = 2
    )

    rec_list = struct.pack(">II", rec0_offset, 0) + struct.pack(">II", rec1_offset, 1) + b"\x00\x00"
    mobi_bytes = pdb_hdr + rec_list + rec0 + html_content

    tmp_mobi = tempfile.NamedTemporaryFile(suffix=".mobi", delete=False)
    tmp_mobi.write(mobi_bytes)
    tmp_mobi.close()

    try:
        with MobiBook(tmp_mobi.name) as book:
            info = book.get_probe_info()
            print("Probe MOBI title:", info["title"])
            print("MOBI TOC count:", info["toc_count"])
            assert "Python" in info["title"]
            assert len(book.toc) >= 2, f"MOBI TOC length: {len(book.toc)}"

            ch1 = book.find_chapter("第1章")
            assert ch1 is not None, "MOBI Chapter 1 not found"
            md1 = book.extract_chapter_markdown(ch1)
            print("Extracted MOBI Chapter 1 Markdown:")
            print(md1)
            assert "解释型" in md1

            ch2 = book.find_chapter("第2章")
            assert ch2 is not None, "MOBI Chapter 2 not found"
            md2 = book.extract_chapter_markdown(ch2)
            print("\nExtracted MOBI Chapter 2 Markdown:")
            print(md2)
            assert "数据结构" in md2

            print("\n[OK] MOBI tests passed successfully!")
    finally:
        if os.path.exists(tmp_mobi.name):
            os.remove(tmp_mobi.name)


if __name__ == "__main__":
    test_epub()
    test_mobi()
