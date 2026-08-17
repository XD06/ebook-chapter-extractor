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

from env_checker import ensure_core_dependencies
ensure_core_dependencies()

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None


def probe_pdf(pdf_path: str) -> dict:
    """PDF 专用探针逻辑（完全保持原有实现）"""
    if pymupdf is None:
        return {"error": "PyMuPDF not installed. Run: pip install pymupdf"}

    if not os.path.exists(pdf_path):
        return {"error": f"File not found: {pdf_path}"}

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as e:
        return {"error": f"Failed to open PDF: {str(e)}"}

    total_pages = doc.page_count
    meta = doc.metadata or {}
    toc = doc.get_toc()

    # 采样前 5 页和第 10 页（若有）文本
    sample_indices = list(range(min(5, total_pages)))
    if total_pages > 10:
        sample_indices.append(9)

    char_counts = [len(doc[i].get_text().strip()) for i in sample_indices]
    avg_chars = sum(char_counts) / len(char_counts) if char_counts else 0
    is_digital = avg_chars > 30

    # 判断分类
    has_toc = len(toc) > 0
    if is_digital and has_toc:
        category = "A (Digital + Has TOC)"
        recommendation = "MarkItDown / PyMuPDF on physical pages directly (No offset needed)"
    elif is_digital and not has_toc:
        category = "B (Digital + No TOC)"
        recommendation = "Extract TOC text from front pages -> compute offset -> MarkItDown"
    elif not is_digital and has_toc:
        category = "C (Scanned + Has TOC)"
        recommendation = "Slice target pages -> mineru-open-api extract --model vlm (Token优先; 无Token才flash-extract, <=20 pages)"
    else:
        category = "D (Scanned + No TOC)"
        recommendation = "OCR TOC pages -> detect chapter title / calibrate page footer -> MinerU extract --model vlm (Token优先)"

    # 提取精简目录样本
    toc_sample = []
    for item in toc[:10]:
        toc_sample.append({
            "level": item[0],
            "title": item[1],
            "page": item[2]
        })

    return {
        "file": os.path.basename(pdf_path),
        "path": os.path.abspath(pdf_path),
        "format": "PDF",
        "total_pages": total_pages,
        "is_digital": is_digital,
        "has_toc": has_toc,
        "toc_count": len(toc),
        "category": category,
        "recommendation": recommendation,
        "toc_sample": toc_sample,
        "sample_char_counts": char_counts,
        "title": meta.get("title", ""),
        "author": meta.get("author", "")
    }


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
    """通用电子书探针分发器"""
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".epub":
        return probe_epub(file_path)
    elif ext in (".mobi", ".azw", ".azw3", ".prc"):
        return probe_mobi(file_path)
    else:
        # 默认作为 PDF 解析（保持原有行为）
        return probe_pdf(file_path)


def main():
    parser = argparse.ArgumentParser(description="Probe eBook (PDF/EPUB/MOBI/AZW3) structure and characteristics.")
    parser.add_argument("file_path", help="Path to eBook file (PDF, EPUB, MOBI, AZW3)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON only")

    args = parser.parse_args()
    info = probe_file(args.file_path)

    if args.json or "error" in info:
        print(json.dumps(info, ensure_ascii=False, indent=2))
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
    print(f"  * 决策分类   : 类别 {info.get('category', '未知')}")
    print(f"  * 推荐路线   : {info.get('recommendation', '标准解析')}")

    if info.get("toc_sample"):
        print("\n[目录预览 (前 10 条)]")
        for it in info["toc_sample"]:
            indent = "  " * it.get("level", 1)
            page_info = f" (物理页: {it['page']})" if "page" in it else ""
            src_info = f" -> {it['src']}" if "src" in it and it["src"] else ""
            print(f"  {indent}- {it['title']}{page_info}{src_info}")
    print("=" * 60)


if __name__ == "__main__":
    main()
