#!/usr/bin/env python3
"""
build_index.py - 电子书章节索引构建工具 (支持 PDF / EPUB / MOBI / AZW3)
- PDF: 解析内嵌书签(内置自愈修复算法)或前置目录页，计算起止物理页码范围 [Start, End]
- EPUB / MOBI / AZW3: 解析 NCX / NAV / Spine 结构，提取完整章节树
导出统一结构化的 chapters.json 索引文件。
"""

import sys
import os
import re
import json
import argparse

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import console_safe  # noqa: F401  # 安装窄编码控制台保护（含非 GBK 字符的文件名）
from env_checker import ensure_core_dependencies
ensure_core_dependencies()
from ocr_helper import get_cache_dir
from pdf_helper import (
    detect_ebook_format,
    heal_toc_entries,
    build_index_from_toc_entries,
    build_index_from_text_toc,
    build_pdf_index_data,
)


def heal_toc(toc: list, total_pages: int, doc=None) -> list:
    """自愈/清洗书签列表（PDF 专用）"""
    return heal_toc_entries(toc, total_pages, doc=doc)


def build_from_toc(doc) -> list:
    """PDF 专用：从内嵌书签构建章节索引"""
    raw_toc = doc.get_toc()
    if not raw_toc:
        return []
    healed = heal_toc(raw_toc, doc.page_count, doc=doc)
    return build_index_from_toc_entries(healed, doc.page_count)


def build_from_text_toc(doc, max_scan_pages: int = 20, allow_ocr: bool = True) -> list:
    """PDF 专用：无书签时，从前置目录提取并计算 Offset"""
    idx, _ = build_index_from_text_toc(doc, max_scan_pages=max_scan_pages, allow_ocr=allow_ocr)
    return idx


def build_pdf_index(pdf_path: str, allow_ocr_toc: bool = True, force_layout: bool = False) -> dict:
    """PDF 索引统一入口：书签自愈 -> 印刷/扫描目录 -> 布局分析虚拟索引 三级回退"""
    return build_pdf_index_data(pdf_path, allow_ocr_toc=allow_ocr_toc, force_layout=force_layout)


def build_epub_index(epub_path: str) -> dict:
    """EPUB 索引统一入口"""
    from epub_parser import EpubBook
    with EpubBook(epub_path) as book:
        return {
            "file_path": epub_path,
            "format": "EPUB",
            "title": book.metadata.get("title", ""),
            "author": book.metadata.get("author", ""),
            "total_chapters": len(book.toc),
            "source": "EPUB TOC (NCX / NAV / Spine)",
            "chapters": book.toc
        }


def build_mobi_index(mobi_path: str) -> dict:
    """MOBI / AZW3 索引统一入口"""
    from mobi_parser import MobiBook
    with MobiBook(mobi_path) as book:
        return {
            "file_path": mobi_path,
            "format": "MOBI/AZW3",
            "title": book.header_info.get("title", ""),
            "total_chapters": len(book.toc),
            "source": "MOBI / KF8 TOC",
            "chapters": book.toc
        }


def main():
    parser = argparse.ArgumentParser(description="Build and save eBook (PDF/EPUB/MOBI/AZW3) chapter index.")
    parser.add_argument("file_path", help="Path to eBook file (PDF, EPUB, MOBI, AZW3)")
    parser.add_argument("--output", "-o", default=None, help="Output JSON path (default: .cache/<file_stem>_chapters.json)")
    parser.add_argument("--print", "-p", action="store_true", help="Print table to stdout")
    parser.add_argument("--force-layout", action="store_true", help="Force layout-based virtual index (Paper Mode) even if bookmarks exist")
    parser.add_argument("--no-ocr-toc", action="store_true", help="Skip OCR scanning of front pages for scanned PDFs")
    args = parser.parse_args()

    file_path = os.path.abspath(args.file_path)
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        sys.exit(1)

    fmt = detect_ebook_format(file_path)
    if fmt == "epub":
        result = build_epub_index(file_path)
    elif fmt == "mobi":
        result = build_mobi_index(file_path)
    else:
        result = build_pdf_index(
            file_path,
            allow_ocr_toc=not args.no_ocr_toc,
            force_layout=args.force_layout,
        )

    chapters = result.get("chapters", [])

    # 确定输出路径
    if not args.output:
        stem = os.path.splitext(os.path.basename(file_path))[0]
        cache_dir = get_cache_dir(file_path)
        os.makedirs(cache_dir, exist_ok=True)
        args.output = os.path.join(cache_dir, f"{stem}_chapters.json")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    fmt = result.get("format", "eBook")
    print(f"Successfully generated {fmt} chapter index ({len(chapters)} entries, source: {result.get('source', 'auto')})")
    print(f" Saved to: {args.output}")

    if args.print and chapters:
        print("\n" + "=" * 80)
        if fmt == "PDF":
            print(f"{'#':<4} | {'章节标题':<40} | {'物理页范围':<12} | {'页数'}")
            print("-" * 80)
            for item in chapters:
                indent = "  " * (item["level"] - 1) if "level" in item else ""
                title_display = (indent + item["title"])[:40]
                rng = f"{item['start_phys']}-{item['end_phys']}"
                print(f"{item['index']:<4} | {title_display:<40} | {rng:<12} | {item['page_count']}")
        else:
            print(f"{'#':<4} | {'章节标题':<45} | {'来源文件 / 锚点'}")
            print("-" * 80)
            for item in chapters:
                indent = "  " * (item.get("level", 1) - 1)
                title_display = (indent + item["title"])[:45]
                src = item.get("src") or item.get("file_path") or f"Offset {item.get('char_offset', 0)}"
                print(f"{item['index']:<4} | {title_display:<45} | {src}")
        print("=" * 80)


if __name__ == "__main__":
    main()
