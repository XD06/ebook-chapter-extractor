#!/usr/bin/env python3
"""
render_page.py - PDF 页面高清视觉渲染工具
将 PDF 的指定物理页或页面范围渲染为高清 PNG 图片，
供 AI Agent 原生多模态视觉直接阅读、理解、核对与转写。
支持：
1. 位置参数：<pdf> [start_page] [end_page] 或 <pdf> [page_range]
2. 选项参数：--pages / -p "38-40", --chapter / -c "2.3.2"
"""

import sys
import os
import re
import argparse

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import console_safe  # noqa: F401  # 安装窄编码控制台保护（含非 GBK 字符的文件名）

from env_checker import ensure_core_dependencies
ensure_core_dependencies()
from ocr_helper import get_cache_dir
from pdf_helper import find_pdf_chapter_range

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print("Error: PyMuPDF is required. Run: pip install pymupdf")
        sys.exit(1)


def render_pages(pdf_path: str, pages: list, output_dir: str = None, dpi: int = 150) -> list:
    if not os.path.exists(pdf_path):
        print(f"Error: PDF not found: {pdf_path}")
        sys.exit(1)

    doc = pymupdf.open(pdf_path)
    total = doc.page_count

    if not output_dir:
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        output_dir = get_cache_dir(pdf_path, f"{pdf_stem}_images")

    os.makedirs(output_dir, exist_ok=True)
    saved_files = []

    try:
        for p in pages:
            if p < 1 or p > total:
                print(f"Warning: Page {p} is out of bounds (1..{total}), skipping.")
                continue
            page = doc[p - 1]
            pix = page.get_pixmap(dpi=dpi)
            img_path = os.path.join(output_dir, f"page_{p:04d}.png")
            pix.save(img_path)
            saved_files.append(img_path)
            print(f"[+] Rendered page {p}/{total} -> {img_path}")
    finally:
        doc.close()

    return saved_files


def parse_page_range(range_str: str, max_pages: int) -> list:
    pages = set()
    for part in range_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start = int(start_s) if start_s else 1
            end = int(end_s) if end_s else max_pages
            if start > end:
                start, end = end, start
            for p in range(max(1, start), min(max_pages, end) + 1):
                pages.add(p)
        else:
            p = int(part)
            if 1 <= p <= max_pages:
                pages.add(p)
    return sorted(list(pages))


def main():
    parser = argparse.ArgumentParser(
        description="Render PDF pages as high-resolution images for Agent Vision."
    )
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument(
        "pages_pos",
        nargs="*",
        default=[],
        help="Optional positional page spec, e.g. '38 40', '38-40', or '12,15,20-22'",
    )
    parser.add_argument(
        "--pages",
        "-p",
        default=None,
        help="Page range to render, e.g. '1', '1-5', '12,15,20-22'",
    )
    parser.add_argument(
        "--chapter",
        "-c",
        default=None,
        help="Chapter keyword/number to render (e.g. '2.3.2', '8.2')",
    )
    parser.add_argument(
        "--output", "-o", default=None, help="Output directory for images"
    )
    parser.add_argument(
        "--dpi", type=int, default=150, help="Image DPI (default: 150)"
    )

    args = parser.parse_args()
    if not os.path.exists(args.pdf_path):
        print(f"Error: PDF not found: {args.pdf_path}")
        sys.exit(1)

    doc = pymupdf.open(args.pdf_path)
    total_pages = doc.page_count

    page_spec = "1"
    if args.chapter:
        ch_title, start_p, end_p = find_pdf_chapter_range(doc, args.chapter, pdf_path=args.pdf_path)
        if start_p is None:
            print(f"Error: Could not find chapter matching '{args.chapter}' in TOC.")
            doc.close()
            sys.exit(1)
        print(f"[*] Matched Chapter: {ch_title} -> Pages {start_p}-{end_p}")
        page_spec = f"{start_p}-{end_p}"
    elif args.pages:
        page_spec = args.pages
    elif len(args.pages_pos) == 1:
        # Check if user passed chapter name or page range in position
        pos_val = args.pages_pos[0]
        if any(c.isdigit() for c in pos_val) and ("-" in pos_val or "," in pos_val or pos_val.isdigit()):
            page_spec = pos_val
        else:
            ch_title, start_p, end_p = find_pdf_chapter_range(doc, pos_val, pdf_path=args.pdf_path)
            if start_p is not None:
                print(f"[*] Matched Chapter from positional arg: {ch_title} -> Pages {start_p}-{end_p}")
                page_spec = f"{start_p}-{end_p}"
            else:
                page_spec = pos_val
    elif len(args.pages_pos) == 2:
        try:
            p1 = int(args.pages_pos[0])
            p2 = int(args.pages_pos[1])
            page_spec = f"{p1}-{p2}"
        except ValueError:
            page_spec = f"{args.pages_pos[0]},{args.pages_pos[1]}"
    elif len(args.pages_pos) > 2:
        page_spec = ",".join(args.pages_pos)

    doc.close()
    page_list = parse_page_range(page_spec, total_pages)
    render_pages(args.pdf_path, page_list, args.output, args.dpi)


if __name__ == "__main__":
    main()
