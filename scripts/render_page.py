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
from ocr_helper import get_cache_dir

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print("Error: PyMuPDF is required. Run: pip install pymupdf")
        sys.exit(1)


def find_pdf_chapter_range(doc, chapter_query: str) -> tuple:
    """在 PDF 书签中智能查找匹配章节的物理起止页（支持前缀、数字、模糊匹配）"""
    raw_toc = doc.get_toc()
    if not raw_toc:
        return None, None, None

    total_pages = doc.page_count
    cleaned = []
    for item in raw_toc:
        level, title, page = item[0], item[1].strip(), item[2]
        cleaned.append({"level": level, "title": title, "phys": page})

    for i in range(len(cleaned)):
        if cleaned[i]["phys"] <= 0:
            cur_lv = cleaned[i]["level"]
            for j in range(i + 1, len(cleaned)):
                if cleaned[j]["level"] <= cur_lv:
                    break
                if cleaned[j]["phys"] > 0:
                    cleaned[i]["phys"] = cleaned[j]["phys"]
                    break

    q = chapter_query.strip()
    q_lower = q.lower()
    q_norm = re.sub(r'[\s\-_.:：·]+', '', q_lower)

    matched_idx = -1

    for i, it in enumerate(cleaned):
        if it["phys"] > 0 and it["title"].strip().lower() == q_lower:
            matched_idx = i
            break

    if matched_idx == -1:
        pattern = r'(?:^|[\s第])' + re.escape(q_lower) + r'(?:[\s.、:：章节]|$)'
        for i, it in enumerate(cleaned):
            if it["phys"] > 0:
                t_lower = it["title"].lower()
                if re.search(pattern, t_lower) or t_lower.startswith(q_lower):
                    matched_idx = i
                    break

    if matched_idx == -1:
        for i, it in enumerate(cleaned):
            if it["phys"] > 0:
                t_norm = re.sub(r'[\s\-_.:：·]+', '', it["title"].lower())
                if q_norm in t_norm:
                    matched_idx = i
                    break

    if matched_idx == -1:
        for i, it in enumerate(cleaned):
            if it["phys"] > 0 and q_lower in it["title"].lower():
                matched_idx = i
                break

    if matched_idx == -1:
        return None, None, None

    target = cleaned[matched_idx]
    start_phys = max(1, min(total_pages, target["phys"]))
    cur_level = target["level"]
    end_phys = total_pages

    for it in cleaned[matched_idx + 1:]:
        if it["level"] <= cur_level and it["phys"] >= start_phys:
            end_phys = max(start_phys, min(total_pages, it["phys"] - 1))
            break

    return target["title"], start_phys, end_phys


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
        ch_title, start_p, end_p = find_pdf_chapter_range(doc, args.chapter)
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
            ch_title, start_p, end_p = find_pdf_chapter_range(doc, pos_val)
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
