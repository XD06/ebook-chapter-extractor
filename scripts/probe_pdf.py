#!/usr/bin/env python3
"""
probe_pdf.py - 1秒快速探针工具
用于快速检测 PDF 文档的物理页数、数字/扫描特征、内嵌书签状态及推荐解析路线。
"""

import sys
import os
import json
import argparse

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print(json.dumps({"error": "PyMuPDF not installed. Run: pip install pymupdf"}))
        sys.exit(1)


def probe(pdf_path: str) -> dict:
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


def main():
    parser = argparse.ArgumentParser(description="Probe PDF structure and characteristics.")
    parser.add_argument("pdf_path", help="Path to the PDF file")
    parser.add_argument("--json", action="store_true", help="Output raw JSON only")

    args = parser.parse_args()
    info = probe(args.pdf_path)

    if args.json or "error" in info:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    print("=" * 60)
    print(f" PDF 快速探针报告: {info['file']}")
    print("=" * 60)
    print(f"  * 文件路径   : {info['path']}")
    print(f"  * 标　　题   : {info['title'] or '(未设置)'}")
    print(f"  * 作　　者   : {info['author'] or '(未设置)'}")
    print(f"  * 总 页 数   : {info['total_pages']} 页")
    print(f"  * 文档类型   : {'原生数字版 (有文本层)' if info['is_digital'] else '扫描图像版 (无文本层)'}")
    print(f"  * 内嵌书签   : {'有 (' + str(info['toc_count']) + ' 条)' if info['has_toc'] else '无书签'}")
    print(f"  * 决策分类   : 类别 {info['category']}")
    print(f"  * 推荐路线   : {info['recommendation']}")

    if info["toc_sample"]:
        print("\n[书签目录预览 (前 10 条)]")
        for it in info["toc_sample"]:
            indent = "  " * it["level"]
            print(f"  {indent}- {it['title']} (物理页: {it['page']})")
    print("=" * 60)


if __name__ == "__main__":
    main()
