#!/usr/bin/env python3
"""
render_page.py - PDF 页面高清视觉渲染工具
将 PDF 的指定物理页或页面范围渲染为高清 PNG 图片，
供 AI Agent 原生多模态视觉直接阅读、理解、核对与转写。
"""

import sys
import os
import argparse

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
        base_dir = os.path.dirname(os.path.abspath(pdf_path))
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        output_dir = os.path.join(base_dir, ".cache", f"{pdf_stem}_images")

    os.makedirs(output_dir, exist_ok=True)
    saved_files = []

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
            for p in range(start, end + 1):
                pages.add(p)
        else:
            pages.add(int(part))
    return sorted(list(pages))


def main():
    parser = argparse.ArgumentParser(description="Render PDF pages as high-resolution images for Agent Vision.")
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument("--pages", "-p", default="1", help="Page range to render, e.g. '1', '1-5', '12,15,20-22'")
    parser.add_argument("--output", "-o", default=None, help="Output directory for images")
    parser.add_argument("--dpi", type=int, default=150, help="Image DPI (default: 150)")

    args = parser.parse_args()
    doc = pymupdf.open(args.pdf_path)
    page_list = parse_page_range(args.pages, doc.page_count)
    doc.close()

    render_pages(args.pdf_path, page_list, args.output, args.dpi)


if __name__ == "__main__":
    main()
