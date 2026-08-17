#!/usr/bin/env python3
"""
build_index.py - PDF 章节索引构建工具 (带书签自愈/修复算法)
解析 PDF 的内嵌书签或前置目录页，计算每个章节的起止物理页码范围 [Start, End]，
并导出结构化 chapters.json 索引文件。
"""

import sys
import os
import re
import json
import argparse

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print("Error: PyMuPDF is required. Run: pip install pymupdf")
        sys.exit(1)


def heal_toc(toc: list, total_pages: int) -> list:
    """
    自愈/清洗书签列表：
    1. 修正 <= 0 的异常书签（向后/向下查找首个有效子节点页码提升修复）；
    2. 过滤完全无效的前置项；
    3. 保证页码在 [1, total_pages] 合法范围内。
    """
    if not toc:
        return []

    cleaned = []
    for item in toc:
        level, title, page = item[0], item[1].strip(), item[2]
        cleaned.append({"level": level, "title": title, "phys": page})

    # 第一遍：向前/向后回溯修复 <= 0 的异常页码
    for i in range(len(cleaned)):
        if cleaned[i]["phys"] <= 0:
            current_level = cleaned[i]["level"]
            # 向下寻找第一个属于它的子节点（level > current_level 且 phys > 0）
            found = False
            for j in range(i + 1, len(cleaned)):
                if cleaned[j]["level"] <= current_level:
                    # 已经到了同级或更高级标题，停止
                    break
                if cleaned[j]["phys"] > 0:
                    cleaned[i]["phys"] = cleaned[j]["phys"]
                    found = True
                    break

            # 若仍未找到，尝试看下一条有效的项
            if not found and i + 1 < len(cleaned) and cleaned[i+1]["phys"] > 0:
                cleaned[i]["phys"] = cleaned[i+1]["phys"]

    # 第二遍：过滤依然 <= 0 的无意义前置项（如空书签、封面异常等）
    valid_toc = [item for item in cleaned if 1 <= item["phys"] <= total_pages]
    return valid_toc


def build_from_toc(doc: pymupdf.Document) -> list:
    raw_toc = doc.get_toc()
    if not raw_toc:
        return []

    healed = heal_toc(raw_toc, doc.page_count)
    if not healed:
        return []

    total_pages = doc.page_count
    index = []

    for i, item in enumerate(healed):
        level, title, start_page = item["level"], item["title"], item["phys"]
        end_page = total_pages

        # 寻找同级或更高级标题作为本节结束点
        for nxt in healed[i + 1:]:
            if nxt["level"] <= level and nxt["phys"] >= start_page:
                end_page = max(start_page, nxt["phys"] - 1)
                break

        index.append({
            "index": i + 1,
            "level": level,
            "title": title,
            "start_phys": start_page,
            "end_phys": end_page,
            "page_count": end_page - start_page + 1
        })

    return index


def build_from_text_toc(doc: pymupdf.Document, max_scan_pages: int = 20) -> list:
    """无书签时，从前置文本目录提取"""
    total = doc.page_count
    toc_pages = []

    for i in range(min(max_scan_pages, total)):
        text = doc[i].get_text()
        if "目录" in text or "Contents" in text or "TABLE OF CONTENTS" in text.upper():
            toc_pages.append(i)

    if not toc_pages:
        return []

    # 提取逻辑页码模式
    extracted = []
    for pno in toc_pages:
        lines = doc[pno].get_text().splitlines()
        for line in lines:
            line = line.strip()
            # 匹配 标题 ..... 123
            m = re.match(r"^(.+?)[.\uff0e\u00b7\s]{2,}(\d+)\s*$", line)
            if m:
                t, logical_p = m.group(1).strip(), int(m.group(2))
                if len(t) >= 2 and not t.isdigit():
                    extracted.append((t, logical_p))

    if not extracted:
        return []

    # 计算 offset: 寻找正文逻辑第1页的物理页
    offset = 0
    # 策略1: get_label()
    for i in range(min(40, total)):
        if str(doc[i].get_label()).strip() == "1":
            offset = i
            break

    # 若无 label，默认根据首个章节提取逻辑值反推（假设首章在目录后几页）
    if offset == 0 and extracted:
        first_logical = extracted[0][1]
        # 假设首个目录页之后即正文
        est_phys = (toc_pages[-1] + 1) + 1  # 1-based
        offset = max(0, est_phys - first_logical)

    index = []
    for i, (t, log_p) in enumerate(extracted):
        start_phys = min(total, max(1, log_p + offset))
        end_phys = total
        if i + 1 < len(extracted):
            nxt_phys = min(total, max(1, extracted[i+1][1] + offset))
            end_phys = max(start_phys, nxt_phys - 1)

        index.append({
            "index": i + 1,
            "level": 1,
            "title": t,
            "start_phys": start_phys,
            "end_phys": end_phys,
            "page_count": end_phys - start_phys + 1
        })

    return index


def main():
    parser = argparse.ArgumentParser(description="Build and save PDF chapter index.")
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument("--output", "-o", default=None, help="Output JSON path (default: .cache/<pdf_name>_chapters.json)")
    parser.add_argument("--print", "-p", action="store_true", help="Print table to stdout")
    args = parser.parse_args()

    pdf_path = os.path.abspath(args.pdf_path)
    if not os.path.exists(pdf_path):
        print(f"Error: PDF not found: {pdf_path}")
        sys.exit(1)

    doc = pymupdf.open(pdf_path)
    index = build_from_toc(doc)
    source = "bookmarks (healed get_toc)"

    if not index:
        index = build_from_text_toc(doc)
        source = "extracted text TOC + offset"

    if not index:
        print("Warning: Could not automatically detect TOC or bookmarks. Empty index generated.")
        index = []

    # 确定输出路径
    if not args.output:
        base_dir = os.path.dirname(pdf_path)
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        cache_dir = os.path.join(base_dir, ".cache")
        os.makedirs(cache_dir, exist_ok=True)
        args.output = os.path.join(cache_dir, f"{pdf_stem}_chapters.json")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({
            "pdf_path": pdf_path,
            "total_pages": doc.page_count,
            "source": source,
            "chapters": index
        }, f, ensure_ascii=False, indent=2)

    print(f"Successfully generated chapter index ({len(index)} entries, source: {source})")
    print(f" Saved to: {args.output}")

    if args.print and index:
        print("\n" + "=" * 80)
        print(f"{'#':<4} | {'章节标题':<40} | {'物理页范围':<12} | {'页数'}")
        print("-" * 80)
        for item in index:
            indent = "  " * (item["level"] - 1) if "level" in item else ""
            title_display = (indent + item["title"])[:40]
            rng = f"{item['start_phys']}-{item['end_phys']}"
            print(f"{item['index']:<4} | {title_display:<40} | {rng:<12} | {item['page_count']}")
        print("=" * 80)


if __name__ == "__main__":
    main()
