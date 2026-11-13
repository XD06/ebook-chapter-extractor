#!/usr/bin/env python3
"""
probe_pdf.py (兼 probe.py) - 1秒快速探针工具
支持自动检测 PDF / EPUB / MOBI / AZW3 电子书特征，输出推荐解析路线与结构信息。
"""

import sys
import os
import json
import argparse

# 添加 scripts 目录到模块查找路径
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import console_safe  # noqa: F401  # 安装窄编码控制台保护（含非 GBK 字符的文件名）
from env_checker import ensure_core_dependencies
ensure_core_dependencies()

from pdf_helper import detect_ebook_format, probe_pdf_document


def probe_pdf(pdf_path: str) -> dict:
    """PDF 专用探针逻辑"""
    return probe_pdf_document(pdf_path)


def probe_epub(epub_path: str) -> dict:
    """EPUB 探针逻辑"""
    try:
        from epub_parser import EpubBook
        with EpubBook(epub_path) as book:
            return book.get_probe_info()
    except Exception as e:
        return {"error": f"Failed to probe EPUB: {str(e)}"}


def probe_mobi(mobi_path: str) -> dict:
    """MOBI / AZW3 探针逻辑"""
    try:
        from mobi_parser import MobiBook
        with MobiBook(mobi_path) as book:
            return book.get_probe_info()
    except Exception as e:
        return {"error": f"Failed to probe MOBI/AZW3: {str(e)}"}


def probe_file(file_path: str) -> dict:
    """通用电子书探针分发器（魔数优先 + 扩展名兜底）"""
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    fmt = detect_ebook_format(file_path)
    if fmt == "epub":
        return probe_epub(file_path)
    elif fmt == "mobi":
        return probe_mobi(file_path)
    else:
        return probe_pdf(file_path)


def main():
    parser = argparse.ArgumentParser(description="Probe eBook (PDF/EPUB/MOBI/AZW3) structure and characteristics.")
    parser.add_argument("file_path", help="Path to eBook file (PDF, EPUB, MOBI, AZW3)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON only")

    args = parser.parse_args()
    info = probe_file(args.file_path)

    if args.json or "error" in info:
        print(json.dumps(info, ensure_ascii=True, indent=2))
        return

    fmt = info.get("format", "PDF")
    print("=" * 60)
    print(f" {fmt} 快速探针报告: {info['file']}")
    print("=" * 60)
    print(f"  * 文件路径   : {info['path']}")
    print(f"  * 文档格式   : {fmt}")
    print(f"  * 标　　题   : {info.get('title') or '(未设置)'}")
    print(f"  * 作　　者   : {info.get('author') or '(未设置)'}")
    if "total_pages" in info:
        print(f"  * 总 页 数   : {info['total_pages']} 页")
    if "total_spine_items" in info:
        print(f"  * 章节文档数 : {info['total_spine_items']} 篇")
    print(f"  * 文档类型   : {'原生数字版 (有文本层/结构化HTML)' if info.get('is_digital', True) else '扫描图像版 (无文本层)'}")
    print(f"  * 目录状态   : {'有 (' + str(info.get('toc_count', 0)) + ' 条)' if info.get('has_toc') else '无目录'}")
    if info.get("doc_type"):
        col = ' / 双栏排版' if info.get('is_two_column') else ' / 单栏'
        print(f"  * 文档形态   : {info['doc_type']}{col}")
    if info.get("paper_mode"):
        print(f"  * 虚拟章节   : Paper Mode 自动重建 {info.get('virtual_heading_count', 0)} 个标题")
    print(f"  * 决策分类   : 类别 {info.get('category', '未知')}")
    print(f"  * 推荐路线   : {info.get('recommendation', '标准解析')}")

    if info.get("toc_sample"):
        label = "目录预览" if info.get("has_toc") else "布局分析虚拟章节预览"
        print(f"\n[{label} (前 10 条)]")
        for it in info["toc_sample"]:
            indent = "  " * it.get("level", 1)
            page_info = f" (物理页: {it['page']})" if "page" in it else ""
            src_info = f" -> {it['src']}" if "src" in it and it["src"] else ""
            print(f"  {indent}- {it['title']}{page_info}{src_info}")
    print("=" * 60)


if __name__ == "__main__":
    main()
