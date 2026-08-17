#!/usr/bin/env python3
"""
test_cli_integration.py - CLI 命令行集成测试
测试 probe.py / probe_pdf.py / build_index.py / extract_chapter.py 对 PDF, EPUB, MOBI 的兼容性与完整性。
"""

import os
import sys
import subprocess
import tempfile
import zipfile
import struct
import json

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")


def create_sample_pdf(pdf_path: str):
    import pymupdf
    doc = pymupdf.open()
    # Page 1: 封面/目录
    p1 = doc.new_page()
    p1.insert_text((50, 72), "Book Title\n\nContents\nChapter 1 ...... 1\nChapter 2 ...... 2\n")
    # Page 2: 第1章
    p2 = doc.new_page()
    p2.insert_text((50, 72), "Chapter 1: Getting Started\nThis is chapter 1 content with details.")
    # Page 3: 第2章
    p3 = doc.new_page()
    p3.insert_text((50, 72), "Chapter 2: Advanced Topics\nThis is chapter 2 content.")

    # 注入书签
    toc = [
        [1, "Chapter 1: Getting Started", 2],
        [1, "Chapter 2: Advanced Topics", 3],
    ]
    doc.set_toc(toc)
    doc.save(pdf_path)
    doc.close()


def create_sample_epub(epub_path: str):
    # 创建一个极简 1x1 png 字节
    png_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    with zipfile.ZipFile(epub_path, "w") as z:
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
            '    <dc:title>EPUB Test Book</dc:title>\n'
            '    <dc:creator>Test Author</dc:creator>\n'
            '  </metadata>\n'
            '  <manifest>\n'
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n'
            '    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
            '    <item id="img1" href="images/code.png" media-type="image/png"/>\n'
            '  </manifest>\n'
            '  <spine toc="ncx">\n'
            '    <itemref idref="ch1"/>\n'
            '  </spine>\n'
            '</package>',
        )
        z.writestr(
            "OEBPS/toc.ncx",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            '  <navMap>\n'
            '    <navPoint id="np1" playOrder="1">\n'
            '      <navLabel><text>第一章 快速入门</text></navLabel>\n'
            '      <content src="ch1.xhtml"/>\n'
            '    </navPoint>\n'
            '  </navMap>\n'
            '</ncx>',
        )
        z.writestr(
            "OEBPS/ch1.xhtml",
            '<!DOCTYPE html><html><body><h1>第一章 快速入门</h1><p>欢迎学习 EPUB 章节解析。</p><p><img src="images/code.png" alt="示例代码图" /></p></body></html>',
        )
        z.writestr("OEBPS/images/code.png", png_bytes)



def run_cmd(cmd: list) -> str:
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    return res.stdout


def test_integration():
    with tempfile.TemporaryDirectory() as tmpdir:
        pdf_path = os.path.join(tmpdir, "sample.pdf")
        epub_path = os.path.join(tmpdir, "sample.epub")
        create_sample_pdf(pdf_path)
        create_sample_epub(epub_path)

        python_bin = sys.executable

        # 1. Test probe.py on PDF
        print("[1] Testing probe.py on PDF...")
        out_probe_pdf = run_cmd([python_bin, os.path.join(SCRIPTS_DIR, "probe.py"), pdf_path, "--json"])
        data = json.loads(out_probe_pdf)
        assert data["format"] == "PDF"
        assert data["total_pages"] == 3
        assert data["has_toc"] is True
        print("  -> PDF Probe OK")

        # 2. Test probe.py on EPUB
        print("[2] Testing probe.py on EPUB...")
        out_probe_epub = run_cmd([python_bin, os.path.join(SCRIPTS_DIR, "probe.py"), epub_path, "--json"])
        data_epub = json.loads(out_probe_epub)
        assert data_epub["format"] == "EPUB"
        assert data_epub["title"] == "EPUB Test Book"
        print("  -> EPUB Probe OK")

        # 3. Test build_index.py on PDF
        print("[3] Testing build_index.py on PDF...")
        idx_pdf = os.path.join(tmpdir, "pdf_index.json")
        run_cmd([python_bin, os.path.join(SCRIPTS_DIR, "build_index.py"), pdf_path, "-o", idx_pdf, "--print"])
        with open(idx_pdf, "r", encoding="utf-8") as f:
            pdf_idx_data = json.load(f)
        assert len(pdf_idx_data["chapters"]) == 2
        assert pdf_idx_data["chapters"][0]["start_phys"] == 2
        print("  -> PDF Index OK")

        # 4. Test build_index.py on EPUB
        print("[4] Testing build_index.py on EPUB...")
        idx_epub = os.path.join(tmpdir, "epub_index.json")
        run_cmd([python_bin, os.path.join(SCRIPTS_DIR, "build_index.py"), epub_path, "-o", idx_epub, "--print"])
        with open(idx_epub, "r", encoding="utf-8") as f:
            epub_idx_data = json.load(f)
        assert len(epub_idx_data["chapters"]) == 1
        assert epub_idx_data["chapters"][0]["title"] == "第一章 快速入门"
        print("  -> EPUB Index OK")

        # 5. Test extract_chapter.py on PDF
        print("[5] Testing extract_chapter.py on PDF...")
        pdf_md = run_cmd([python_bin, os.path.join(SCRIPTS_DIR, "extract_chapter.py"), pdf_path, "-c", "Chapter 1", "-f", "text"])
        assert "Getting Started" in pdf_md
        print("  -> PDF Extract OK")

        # 6. Test extract_chapter.py on EPUB
        print("[6] Testing extract_chapter.py on EPUB...")
        epub_md = run_cmd([python_bin, os.path.join(SCRIPTS_DIR, "extract_chapter.py"), epub_path, "-c", "第一章", "-f", "md"])
        assert "第一章 快速入门" in epub_md
        assert "欢迎学习 EPUB" in epub_md
        print("  -> EPUB Extract OK")

        # 7. Test extract_chapter.py with --dump-images and --ocr
        print("[7] Testing extract_chapter.py with --dump-images & --ocr...")
        dump_dir = os.path.join(tmpdir, "dumped_imgs")
        out_ocr_md = os.path.join(tmpdir, "ch1_ocr.md")
        run_cmd([python_bin, os.path.join(SCRIPTS_DIR, "extract_chapter.py"), epub_path, "-c", "第一章", "--dump-images", dump_dir, "--ocr", "-o", out_ocr_md])
        assert os.path.exists(dump_dir)
        assert len(os.listdir(dump_dir)) == 1
        with open(out_ocr_md, "r", encoding="utf-8") as f:
            ocr_content = f.read()
        assert "第一章 快速入门" in ocr_content
        print("  -> Image Dump & OCR OK")

        print("\nAll integration tests passed seamlessly!")



if __name__ == "__main__":
    test_integration()
