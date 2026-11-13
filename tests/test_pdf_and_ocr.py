#!/usr/bin/env python3
"""
test_pdf_and_ocr.py - 测试 pdf_helper 与 ocr_helper 的核心稳定性与精度增强
覆盖：魔数嗅探、Windows 文件名清洗、印刷目录提取与 Offset 校准、非单调断链书签自愈、
稀疏书签标题扫描、混合扫描页探测、过期缓存版本隔离、OCR 同行聚类与代码块误判防护。
"""

import json
import os
import sys
import tempfile
import unittest

import pymupdf

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from ocr_helper import _format_rapidocr_boxes, clean_ocr_text, format_ocr_markdown
from pdf_helper import (
    INDEX_SCHEMA_VERSION,
    _load_or_build_pdf_chapters,
    build_index_from_headings,
    build_index_from_text_toc,
    build_pdf_index_data,
    detect_ebook_format,
    extract_pdf_as_markdown_fast,
    heal_toc_entries,
    is_degenerate_toc,
    probe_pdf_document,
    sanitize_filename,
)


def _rect_png_bytes(width: int = 400, height: int = 600) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.clear_with(245)
    return pix.tobytes("png")


class TestPdfHelper(unittest.TestCase):
    def test_detect_ebook_format_magic_and_ext(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_file = os.path.join(tmp, "fake_ext.bin")
            with open(pdf_file, "wb") as f:
                f.write(b"%PDF-1.7\n...")
            self.assertEqual(detect_ebook_format(pdf_file), "pdf")

            mobi_file = os.path.join(tmp, "book.dat")
            with open(mobi_file, "wb") as f:
                f.write(b"\x00" * 60 + b"BOOKMOBI" + b"\x00" * 20)
            self.assertEqual(detect_ebook_format(mobi_file), "mobi")

            azw3_file = os.path.join(tmp, "kindle.azw3")
            with open(azw3_file, "wb") as f:
                f.write(b"\x00" * 16)
            self.assertEqual(detect_ebook_format(azw3_file), "mobi")

            epub_file = os.path.join(tmp, "book.epub")
            with open(epub_file, "wb") as f:
                f.write(b"PK\x03\x04\x00\x00")
            self.assertEqual(detect_ebook_format(epub_file), "epub")

    def test_sanitize_filename_windows_chars(self):
        raw = '  Ch 1: What? *Why* <A/B> "Test"| \t  '
        cleaned = sanitize_filename(raw)
        for ch in '\\/:*?"<>|\r\n\t':
            self.assertNotIn(ch, cleaned)
        self.assertTrue(cleaned.startswith("Ch 1"))
        self.assertEqual(sanitize_filename("..."), "slice")

    def test_build_index_from_text_toc_no_nameerror_and_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "printed_toc.pdf")
            doc = pymupdf.open()
            # P1: Cover, P2: Printed TOC (logical p1 -> phys p3, so offset = 2)
            doc.new_page().insert_text((72, 100), "Cover Title\nAuthor")
            doc.new_page().insert_text(
                (72, 100),
                "目录\n第1章 基础理论 ........ 1\n第2章 实践方法 ........ 3\n",
                fontname="china-s",
            )
            doc.new_page().insert_text((72, 100), "第1章 基础理论\n正文第一页内容。", fontname="china-s")
            doc.new_page().insert_text((72, 100), "第1章续页。", fontname="china-s")
            doc.new_page().insert_text((72, 100), "第2章 实践方法\n正文第二章内容。", fontname="china-s")
            doc.save(pdf_path)
            doc.close()

            data = build_pdf_index_data(pdf_path, allow_ocr_toc=False)
            self.assertGreaterEqual(len(data["chapters"]), 2)
            self.assertEqual(data["chapters"][0]["start_phys"], 3)
            self.assertEqual(data["chapters"][0]["end_phys"], 4)
            self.assertEqual(data["chapters"][1]["start_phys"], 5)
            self.assertEqual(data["chapters"][1]["end_phys"], 5)

            doc2 = pymupdf.open(pdf_path)
            try:
                chapters, source = build_index_from_text_toc(doc2, allow_ocr=False)
                self.assertEqual(len(chapters), 2)
                self.assertIn("text TOC", source)
            finally:
                doc2.close()

    def test_heal_toc_broken_page1_and_frontmatter_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "broken_toc.pdf")
            doc = pymupdf.open()
            for i in range(1, 13):
                p = doc.new_page()
                if i == 1:
                    p.insert_text((72, 72), "Table of Contents")
                elif i == 2:
                    p.insert_text((72, 72), "Author Bio Details")
                elif i == 5:
                    p.insert_text((72, 72), "什么是他人意见恐惧FOPO\n正文开始", fontname="china-s")
                elif i == 9:
                    p.insert_text((72, 72), "被认同的渴望\n第二部分内容", fontname="china-s")
                else:
                    p.insert_text((72, 72), f"Filler page {i}")

            # 模拟扉页 L1 与中部断链回跳到 p=1 的书签
            raw_toc = [
                [1, "Table of Contents", 1],
                [1, "Author Bio", 2],
                [2, "第一章 序言", 3],
                [2, "什么是他人意见恐惧FOPO", 1],  # 断链回跳到 1！
                [3, "FOPO特点", 6],
                [2, "被认同的渴望", 9],
                [2, "越界结语", 99],  # 越界页码！
            ]
            doc.set_toc(raw_toc)
            doc.save(pdf_path)
            doc.close()

            data = build_pdf_index_data(pdf_path, allow_ocr_toc=False)
            chapters = data["chapters"]
            fopo = next(c for c in chapters if "FOPO" in c["title"] and "特点" not in c["title"])
            self.assertEqual(fopo["start_phys"], 5, f"Expected FOPO healed to p5 via body search, got {fopo}")
            self.assertEqual(fopo["end_phys"], 8, f"FOPO range should stop before 被认同的渴望(p9), got {fopo}")

            # 扉页误标 L1 应降级为 L2（或与正文 L2 同级），防止 Table of Contents 吞并全书
            toc_entry = chapters[0]
            self.assertEqual(toc_entry["level"], 2)
            self.assertLessEqual(toc_entry["end_phys"], 2)

            end_entry = chapters[-1]
            self.assertLessEqual(end_entry["start_phys"], 12)

    def test_fast_pdf_markdown_mixed_font_and_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "mixed_table.pdf")
            doc = pymupdf.open()
            # Page 1: 中英混排正文（即使英文 span 字体不同也不得碎成假表格）
            p1 = doc.new_page()
            p1.insert_text((72, 100), "我们讨论的是", fontname="china-s", fontsize=11)
            p1.insert_text((150, 100), "FOPO", fontname="helv", fontsize=11)
            p1.insert_text((185, 100), "概念及其影响。", fontname="china-s", fontsize=11)

            # Page 2: 带清晰矢量框线的真实 2x2 表格
            p2 = doc.new_page()
            x0, y0, cw, rh = 60, 80, 140, 30
            for r in range(3):
                p2.draw_line((x0, y0 + r * rh), (x0 + 2 * cw, y0 + r * rh))
            for c in range(3):
                p2.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + 2 * rh))
            p2.insert_text((x0 + 8, y0 + 20), "Metric")
            p2.insert_text((x0 + cw + 8, y0 + 20), "Value")
            p2.insert_text((x0 + 8, y0 + rh + 20), "Latency")
            p2.insert_text((x0 + cw + 8, y0 + rh + 20), "12ms")
            doc.save(pdf_path)

            try:
                md = extract_pdf_as_markdown_fast(doc, 1, 2)
                self.assertIn("<!-- Page 1 -->", md)
                self.assertIn("FOPO", md)
                # Page 1 不应生成 Markdown 表格分隔符；Page 2 应完整渲染出真实表格
                page1_part = md.split("<!-- Page 2 -->")[0]
                page2_part = md.split("<!-- Page 2 -->")[1]
                self.assertNotIn("|---|", page1_part, f"Mixed CJK+Latin prose shattered into fake table: {page1_part}")
                self.assertIn("|Metric|Value|", page2_part.replace(" ", ""))
                self.assertIn("|Latency|12ms|", page2_part.replace(" ", ""))
            finally:
                doc.close()

    def test_sparse_toc_fallback_and_mixed_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "sparse_mixed.pdf")
            doc = pymupdf.open()
            png_bytes = _rect_png_bytes(500, 700)
            for pno in range(1, 10):
                page = doc.new_page(width=595, height=842)
                if pno == 1:
                    # 退化第1页且含第5页纯大图扫描页
                    page.insert_text((72, 100), "Supplementary Info\nDOI: 10.1000/xyz\nBody text line " * 8, fontsize=10)
                elif pno == 2:
                    page.insert_text((72, 100), "Materials and Methods", fontsize=16)
                    page.insert_text((72, 140), "Animals and experiments " * 15, fontsize=10)
                elif pno == 5:
                    # 纯扫描大图页，无文本层
                    page.insert_image(page.rect, stream=png_bytes)
                elif pno == 7:
                    page.insert_text((72, 100), "Table S1 Summary", fontsize=16)
                    page.insert_text((72, 140), "Statistical summary paragraph " * 12, fontsize=10)
                else:
                    page.insert_text((72, 100), f"Regular digital body content on page {pno}. " * 12, fontsize=10)

            doc.set_toc([[1, "paper_draft.pdf", 1], [1, "References", 9]])
            doc.save(pdf_path)
            doc.close()

            self.assertTrue(is_degenerate_toc([[1, "paper_draft.pdf", 1], [1, "References", 9]], 9))
            probe = probe_pdf_document(pdf_path)
            self.assertTrue(probe["is_digital"])
            self.assertTrue(probe["is_mixed"], f"Expected is_mixed=True due to full-page image on p5: {probe}")

            data = build_pdf_index_data(pdf_path, allow_ocr_toc=False)
            self.assertEqual(data["source"], "layout headings scan")
            titles = [c["title"] for c in data["chapters"]]
            self.assertTrue(any("Materials and Methods" in t for t in titles), titles)
            self.assertTrue(any("Table S1" in t for t in titles), titles)

    def test_stale_v1_cache_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "demo.pdf")
            doc = pymupdf.open()
            doc.new_page().insert_text((72, 72), "Page 1")
            doc.new_page().insert_text((72, 72), "Chapter 1 Real Body")
            doc.set_toc([[1, "Chapter 1", 2]])
            doc.save(pdf_path)

            cache_dir = os.path.join(tmp, ".cache")
            os.makedirs(cache_dir, exist_ok=True)
            stale_cache = os.path.join(cache_dir, "demo_chapters.json")
            with open(stale_cache, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "total_pages": 2,
                        "chapters": [{"index": 1, "level": 1, "title": "STALE_V1", "start_phys": 1, "end_phys": 2, "page_count": 2}],
                    },
                    f,
                )

            try:
                chapters = _load_or_build_pdf_chapters(doc, pdf_path)
                self.assertEqual(len(chapters), 1)
                self.assertNotEqual(chapters[0]["title"], "STALE_V1")
                self.assertEqual(chapters[0]["start_phys"], 2)
                self.assertEqual(INDEX_SCHEMA_VERSION, 2)
            finally:
                doc.close()


class TestOcrHelper(unittest.TestCase):
    def test_rapidocr_box_row_clustering(self):
        # 同一视觉行左右分开的两个框应聚合并按 X 从左到右拼接；下一行放行
        boxes = [
            ([[380, 100], [410, 100], [410, 120], [380, 120]], "11", 0.99),
            ([[60, 103], [260, 103], [260, 123], [60, 123]], "为什么买房贷款最多贷30年？", 0.99),
            ([[60, 150], [240, 150], [240, 170], [60, 170]], "第二章 经济流动", 0.99),
            ([[380, 151], [410, 151], [410, 171], [380, 171]], "15", 0.99),
        ]
        txt = _format_rapidocr_boxes(boxes)
        lines = txt.splitlines()
        self.assertEqual(len(lines), 2, f"Expected 2 clustered lines, got: {lines}")
        self.assertTrue(lines[0].startswith("为什么买房贷款最多贷30年？") and lines[0].endswith("11"), lines[0])
        self.assertTrue(lines[1].startswith("第二章 经济流动") and lines[1].endswith("15"), lines[1])

    def test_clean_ocr_text_preserves_indent_and_urls(self):
        raw = "def demo():\n    x = 1\n    return x\n# visit https://example.com/a/b"
        cleaned = clean_ocr_text(raw)
        self.assertIn("\n    x = 1\n    return x", cleaned)
        self.assertIn("https://example.com/a/b", cleaned)

    def test_format_ocr_markdown_no_false_code_on_urls_or_prose(self):
        prose = (
            "参考文献与补充资料请访问 https://example.com/paper；详情如下；\n"
            "作者指出；实验设计包含多个对照条件。"
        )
        out_prose = format_ocr_markdown(prose, alt_text="正文说明")
        self.assertNotIn("```", out_prose, f"Prose with URL/semicolons falsely detected as code: {out_prose}")
        self.assertIn("> 参考文献", out_prose)

        code = "def add(a, b):\n    return a + b\nprint(add(1, 2))"
        out_code = format_ocr_markdown(code, alt_text="示例代码")
        self.assertIn("```python\n", out_code, f"Real Python snippet not fenced: {out_code}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
