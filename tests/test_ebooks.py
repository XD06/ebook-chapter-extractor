#!/usr/bin/env python3
"""
test_ebooks.py - 测试 EPUB 和 MOBI/AZW3 解析引擎
"""

import os
import sys
import struct
import zipfile
import tempfile
import unittest

# 添加 scripts 目录到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from bs4 import BeautifulSoup
from epub_parser import EpubBook
from mobi_parser import MobiBook, decompress_palmdoc, _get_trailing_data_size
from mathml_helper import convert_soup_mathml_to_latex


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
            assert "\n```c\n" in md2, f"Expected clean fenced block without indent: {md2!r}"
            assert "    ```c" not in md2, f"Unexpected 4-space code block indent: {md2!r}"
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


def _build_pdb(records: list, name: bytes = b"TestBook") -> bytes:
    """构造通用 PalmDB 镜像"""
    num_records = len(records)
    pdb_name = (name + b"\x00" * 32)[:32]
    pdb_hdr = pdb_name + struct.pack(
        ">HHIIIIII4s4sIIH",
        0, 0, 0, 0, 0, 0, 0, 0,
        b"BOOK", b"MOBI", 0, 0, num_records,
    )
    first_off = 78 + num_records * 8 + 2
    rec_entries = []
    cur = first_off
    for idx, rec in enumerate(records):
        rec_entries.append(struct.pack(">II", cur, idx))
        cur += len(rec)
    return pdb_hdr + b"".join(rec_entries) + b"\x00\x00" + b"".join(records)


def _build_mobi_hdr_record(
    text_len: int,
    text_rec_count: int,
    compression: int = 1,
    file_version: int = 6,
    encryption_type: int = 0,
    first_img_idx: int = 0xFFFFFFFF,
    huff_offset: int = 0xFFFFFFFF,
    huff_count: int = 0,
    rec192: int = 0xFFFFFFFF,
    extra_data_flags: int = 0,
    title: str = "Synthetic MOBI",
    author: str = "",
    publisher: str = "",
) -> bytes:
    """构造标准 244B PalmDOC(16B) + MOBI(228B) Header 及可选 EXTH 块"""
    rec = bytearray(244)
    struct.pack_into(">HHIHHHH", rec, 0, compression, 0, text_len, text_rec_count, 4096, encryption_type, 0)
    rec[16:20] = b"MOBI"
    struct.pack_into(">IIIII", rec, 20, 228, 2, 65001, 999, file_version)
    struct.pack_into(">IIII", rec, 108, first_img_idx, huff_offset, huff_count, 0)
    struct.pack_into(">I", rec, 192, rec192)
    struct.pack_into(">H", rec, 242, extra_data_flags)

    exth_entries = []
    if author:
        ab = author.encode("utf-8")
        exth_entries.append(struct.pack(">II", 100, 8 + len(ab)) + ab)
    if publisher:
        pb = publisher.encode("utf-8")
        exth_entries.append(struct.pack(">II", 101, 8 + len(pb)) + pb)
    tb = title.encode("utf-8")
    exth_entries.append(struct.pack(">II", 503, 8 + len(tb)) + tb)

    exth_flags = 0x40 if exth_entries else 0
    struct.pack_into(">I", rec, 128, exth_flags)

    exth_blob = b""
    if exth_entries:
        body = b"".join(exth_entries)
        exth_len = 12 + len(body)
        pad = (-exth_len) % 4
        exth_blob = b"EXTH" + struct.pack(">II", exth_len + pad, len(exth_entries)) + body + b"\x00" * pad

    title_offset = len(rec) + len(exth_blob)
    struct.pack_into(">II", rec, 84, title_offset, len(tb))
    return bytes(rec) + exth_blob + tb


def test_epub_anchor_slice_multispine_header_drm_and_mathml():
    """测试 EPUB 同文件锚点精切、跨 Spine 章节合并、<header> 保留、UTF-16 解码、DRM 拦截与扩展 MathML"""
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"

    # 1. 多章合一 + 跨 Spine 章节 + 字体混淆白名单 + UTF-16
    tmp_epub = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp_epub.close()
    try:
        with zipfile.ZipFile(tmp_epub.name, "w") as z:
            z.writestr("mimetype", "application/epub+zip")
            z.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            # IDPF 字体混淆不得误报为 DRM
            z.writestr(
                "META-INF/encryption.xml",
                '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
                '<enc:CipherData><enc:CipherReference URI="OEBPS/Fonts/test.ttf"/></enc:CipherData></enc:EncryptedData></encryption>',
            )
            z.writestr("OEBPS/Fonts/test.ttf", b"DUMMYFONT")
            z.writestr(
                "OEBPS/content.opf",
                '<?xml version="1.0" encoding="utf-8"?>'
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>MultiSpine</dc:title><dc:publisher>Pub</dc:publisher></metadata>'
                '<manifest>'
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                '<item id="s1" href="Text/sec1.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="s2" href="Part2/sec2.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="s3" href="Part2/sec3.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="img2" href="Part2/figs/diagram.png" media-type="image/png"/>'
                '</manifest>'
                '<spine><itemref idref="s1"/><itemref idref="s2"/><itemref idref="s3"/></spine>'
                '</package>',
            )
            z.writestr(
                "OEBPS/nav.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
                '<li><a href="Text/sec1.xhtml#ch1">Ch1 Mono Split</a></li>'
                '<li><a href="Text/sec1.xhtml#ch2">Ch2 MultiSpine</a></li>'
                '<li><a href="Part2/sec3.xhtml">Ch3 Final</a></li>'
                '</ol></nav></body></html>',
            )
            # UTF-16LE 编码且含运行页脚 pagination、语义 header 以及两个锚点章 Ch1/Ch2
            sec1_html = (
                '<!DOCTYPE html><html><body>'
                '<footer class="pagination">Page 1</footer>'
                '<header><a id="ch1"></a><h1>Ch1 Mono Split</h1></header>'
                '<p>Alpha ONLY body.</p>'
                '<pre class="python">def foo():\n    return 42</pre>'
                '<h2><a id="ch2"></a>Ch2 MultiSpine</h2>'
                '<p>Beta part one.</p>'
                '</body></html>'
            ).encode("utf-16")
            z.writestr("OEBPS/Text/sec1.xhtml", sec1_html)
            z.writestr(
                "OEBPS/Part2/sec2.xhtml",
                '<html><body><p>Beta continuation in spine 2.</p><img src="figs/diagram.png" alt="diag"/></body></html>',
            )
            z.writestr("OEBPS/Part2/sec3.xhtml", "<html><body><h1>Ch3 Final</h1><p>Gamma end.</p></body></html>")
            z.writestr("OEBPS/Part2/figs/diagram.png", png_bytes)

        with EpubBook(tmp_epub.name) as book:
            info = book.get_probe_info()
            assert info["is_drm"] is False, "Font obfuscation falsely flagged as DRM"
            assert info["publisher"] == "Pub"

            ch1 = book.find_chapter("Ch1")
            md1 = book.extract_chapter_markdown(ch1)
            assert "Ch1 Mono Split" in md1, "Semantic <header><h1> stripped"
            assert "Alpha ONLY body." in md1
            assert "Page 1" not in md1, "Pagination footer leaked"
            assert "Beta part one." not in md1, "Same-file Ch1 slice leaked into Ch2"
            assert "```python\ndef foo():\n    return 42\n```" in md1

            ch2 = book.find_chapter("Ch2")
            with tempfile.TemporaryDirectory() as img_out:
                md2 = book.extract_chapter_markdown(ch2, dump_images_dir=img_out)
                assert "Beta part one." in md2
                assert "Beta continuation in spine 2." in md2, "Multi-spine continuation dropped"
                assert "Gamma end." not in md2
                assert len(os.listdir(img_out)) == 1, "Relative image in spine 2 not resolved"
    finally:
        if os.path.exists(tmp_epub.name):
            os.remove(tmp_epub.name)

    # 2. 真实内容加密 DRM 拦截测试
    tmp_drm = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    tmp_drm.close()
    try:
        with zipfile.ZipFile(tmp_drm.name, "w") as z:
            z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
            z.writestr(
                "META-INF/encryption.xml",
                '<encryption xmlns="http://www.w3.org/2001/04/xmlenc#"><EncryptedData>'
                '<EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
                '<CipherData><CipherReference URI="ch1.xhtml"/></CipherData></EncryptedData></encryption>',
            )
            z.writestr("content.opf", '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Locked</dc:title></metadata><manifest><item id="c1" href="ch1.xhtml"/></manifest><spine><itemref idref="c1"/></spine></package>')
            z.writestr("ch1.xhtml", b"\x00\xffEncryptedCiphertext")
        with EpubBook(tmp_drm.name) as drm_book:
            assert drm_book.get_probe_info()["is_drm"] is True
            try:
                drm_book.extract_chapter_markdown(drm_book.toc[0])
                raise AssertionError("Expected RuntimeError for DRM chapter extraction")
            except RuntimeError as exc:
                assert "DRM" in str(exc)
    finally:
        if os.path.exists(tmp_drm.name):
            os.remove(tmp_drm.name)

    # 3. 扩展 MathML 测试 (munderover, mtable/mtr/mtd, mover, munder)
    soup = BeautifulSoup(
        '<math display="block">'
        '<munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></munderover>'
        '<mfrac><msub><mi>x</mi><mi>i</mi></msub><mi>n</mi></mfrac>'
        '<mo>=</mo>'
        '<mover><mi>x</mi><mo>¯</mo></mover>'
        '</math>'
        '<math><mtable><mtr><mtd><mn>1</mn></mtd><mtd><mn>2</mn></mtd></mtr><mtr><mtd><mn>3</mn></mtd><mtd><mn>4</mn></mtd></mtr></mtable></math>',
        "html.parser",
    )
    convert_soup_mathml_to_latex(soup)
    rendered = soup.get_text()
    assert "\\sum" in rendered and "_{i" in rendered and "^{n}" in rendered, rendered
    assert "\\frac{x_{i}}{n}" in rendered, rendered
    assert "\\bar{x}" in rendered or "\\overline{x}" in rendered, rendered
    assert "\\begin{matrix}1 & 2 \\\\ 3 & 4\\end{matrix}" in rendered, rendered
    print("[OK] EPUB advanced & MathML tests passed successfully!")


def test_mobi_huff_cdic_trailing_and_kf8():
    """测试 MOBI trailing extra_data_flags 剥离、HUFF/CDIC 解压、原生 AZW3(KF8) FDST+EXTH 及代码块提取"""
    # 1. trailing extra_data_flags: bit0=1 (overlap 2B = trailing byte 0x01 => total 2B) + bit1=1 (varint len 3B: b'XX\x83')
    raw_payload = b"<h1>H</h1><p>UTF8</p>"
    record_with_trail = raw_payload + b"\xaa\x01" + b"XX\x83"
    trail_sz = _get_trailing_data_size(record_with_trail, 0b11)
    assert trail_sz == 5, f"Expected 5 trailing bytes, got {trail_sz}"

    # 2. HUFF / CDIC 压缩 (compression=17480) + 递归 CDIC 字典项
    # 编码 scheme: bit '1' -> idx 0; '01' -> idx 1; '00' -> idx 2 (idx 2 含 flag=0 压缩串 0xAD = 1|01|01|1|01)
    dict1_vals = [((2 << 8) | 0 | 2) for _ in range(128)] + [((1 << 8) | 0x80 | 1) for _ in range(128)]
    dict2_pairs = [(0, 1)] * 32
    dict2_pairs[0] = (1, 1)  # codelen=1
    dict2_pairs[1] = (0, 2)  # codelen=2
    dict2_flat = [x for pair in dict2_pairs for x in pair]
    huff_rec = (
        b"HUFF\x00\x00\x00\x18"
        + struct.pack(">II", 24, 24 + 256 * 4)
        + b"\x00\x00\x00\x00\x00\x00\x00\x00"
        + struct.pack(">256I", *dict1_vals)
        + struct.pack(">64I", *dict2_flat)
    )
    entries = [
        (0x8000, b"<h1>HUFF</h1>"),
        (0x8000, b"<p>OK</p>"),
        (0x0000, bytes([0b10101101])),  # 递归解压为 <h1>HUFF</h1><p>OK</p><p>OK</p><h1>HUFF</h1><p>OK</p>
    ]
    blob = bytearray()
    offsets = []
    for flag, data in entries:
        offsets.append(len(blob) + 6)  # 3 offsets * 2B = 6B before entries
        blob.extend(struct.pack(">H", flag | len(data)) + data)
    cdic_rec = b"CDIC\x00\x00\x00\x10" + struct.pack(">II", 3, 4) + struct.pack(">3H", *offsets) + bytes(blob)
    # 文本比特流 0b10001101 -> bits: '1'(idx0) + '00'(idx2) + '01'(idx1) + '1'(idx0) + '01'(idx1)
    comp_text = bytes([0b10001101]) + b"\x00\x01"  # 附带 2B bit0 overlap 尾缀
    rec0_huff = _build_mobi_hdr_record(
        text_len=200,
        text_rec_count=1,
        compression=17480,
        huff_offset=2,
        huff_count=2,
        extra_data_flags=1,
        title="Huffman Test Book",
        author="Donald Knuth",
    )
    huff_mobi_bytes = _build_pdb([rec0_huff, comp_text, huff_rec, cdic_rec], name=b"HuffBook")
    tmp_huff = tempfile.NamedTemporaryFile(suffix=".mobi", delete=False)
    tmp_huff.write(huff_mobi_bytes)
    tmp_huff.close()
    try:
        with MobiBook(tmp_huff.name) as hbook:
            probe = hbook.get_probe_info()
            assert probe["author"] == "Donald Knuth"
            assert hbook.full_html.startswith("<h1>HUFF</h1><h1>HUFF</h1><p>OK</p>")
    finally:
        if os.path.exists(tmp_huff.name):
            os.remove(tmp_huff.name)

    # 3. 原生 AZW3 (Standalone KF8, file_version=8 + FDST Flow0 隔离 CSS + <pre> 无缩进 + kindle:embed 图片 + EXTH + 跨级图片)
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    main_html = (
        '<html><body>'
        '<h1>第1章 KF8架构</h1><p>正文导论</p><pre class="python">print("azw3")</pre>'
        '<h2>1.1 子节附图</h2><img src="kindle:embed:0001?mime=image/png" alt="kf8img"/>'
        '<h1>第2章 结束语</h1><p>收尾内容</p>'
        '</body></html>'
    ).encode("utf-8")
    css_tail = b"body { color: #111; } .ignored_css_flow { display: none; }"
    full_flow = main_html + css_tail
    fdst_rec = b"FDST" + struct.pack(">II", 12, 2) + struct.pack(">IIII", 0, len(main_html), len(main_html), len(full_flow))
    # Records: 0=header, 1=text, 2=fdst, 3=png
    rec0_kf8 = _build_mobi_hdr_record(
        text_len=len(full_flow),
        text_rec_count=1,
        compression=1,
        file_version=8,
        first_img_idx=3,
        rec192=2,
        title="Kindle Format 8 Guide",
        author="Alice",
        publisher="KindlePress",
    )
    azw3_bytes = _build_pdb([rec0_kf8, full_flow, fdst_rec, png_bytes], name=b"KF8Guide")
    tmp_azw3 = tempfile.NamedTemporaryFile(suffix=".azw3", delete=False)
    tmp_azw3.write(azw3_bytes)
    tmp_azw3.close()
    try:
        with MobiBook(tmp_azw3.name) as kbook:
            pinfo = kbook.get_probe_info()
            assert "KF8" in pinfo["format"]
            assert pinfo["author"] == "Alice"
            assert pinfo["publisher"] == "KindlePress"
            assert ".ignored_css_flow" not in kbook.full_html, "FDST flow 1 CSS leaked into HTML"

            ch1 = kbook.find_chapter("第1章")
            md1 = kbook.extract_chapter_markdown(ch1)
            assert "```python\nprint(\"azw3\")\n```" in md1
            assert "    ```python" not in md1
            imgs = kbook.extract_chapter_images(ch1)
            assert len(imgs) == 1, f"Level-1 chapter should include level-2 subsection image, got {len(imgs)}"
    finally:
        if os.path.exists(tmp_azw3.name):
            os.remove(tmp_azw3.name)

    print("[OK] MOBI HUFF/CDIC, trailing flags, and native AZW3/KF8 tests passed!")


class TestEbooksSuite(unittest.TestCase):
    def test_01_epub_basic(self):
        test_epub()

    def test_02_mobi_basic(self):
        test_mobi()

    def test_03_epub_advanced_and_mathml(self):
        test_epub_anchor_slice_multispine_header_drm_and_mathml()

    def test_04_mobi_huff_and_kf8(self):
        test_mobi_huff_cdic_trailing_and_kf8()


if __name__ == "__main__":
    unittest.main(verbosity=2)
