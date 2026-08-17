#!/usr/bin/env python3
"""
extract_chapter.py - PDF 按需章节提取与切片工具 (含 Agent Native Vision 支持)
根据章节标题或物理页码范围，按需提取目标内容为纯文本、Markdown、独立子 PDF 切片或渲染为高清视觉图片。
"""

import sys
import os
import argparse
import tempfile

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        print("Error: PyMuPDF is required. Run: pip install pymupdf")
        sys.exit(1)


def find_chapter_range(doc: pymupdf.Document, chapter_query: str) -> tuple:
    """在书签中查找匹配章节的物理起止页"""
    raw_toc = doc.get_toc()
    if not raw_toc:
        return None, None, None

    # 清洗书签，修复 <= 0 的项
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

    # 匹配目标章节
    matched_idx = -1
    query_lower = chapter_query.lower()
    for i, it in enumerate(cleaned):
        if query_lower in it["title"].lower() and it["phys"] > 0:
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


def extract_as_text(doc: pymupdf.Document, start_phys: int, end_phys: int) -> str:
    chunks = []
    for pno in range(start_phys - 1, end_phys):
        text = doc[pno].get_text()
        chunks.append(f"<!-- Page {pno + 1} -->\n{text}")
    return "\n\n".join(chunks)


def slice_sub_pdf(doc: pymupdf.Document, start_phys: int, end_phys: int, output_path: str):
    sub = pymupdf.open()
    for pno in range(start_phys - 1, end_phys):
        sub.insert_pdf(doc, from_page=pno, to_page=pno)
    sub.save(output_path)
    sub.close()


def render_images(doc: pymupdf.Document, start_phys: int, end_phys: int, output_dir: str, dpi: int = 150) -> list:
    os.makedirs(output_dir, exist_ok=True)
    images = []
    for pno in range(start_phys - 1, end_phys):
        page = doc[pno]
        pix = page.get_pixmap(dpi=dpi)
        img_path = os.path.join(output_dir, f"page_{pno + 1:04d}.png")
        pix.save(img_path)
        images.append(img_path)
    return images


def extract_as_markdown(doc: pymupdf.Document, start_phys: int, end_phys: int, output_md: str = None) -> str:
    try:
        from markitdown import MarkItDown
    except ImportError:
        print("Warning: markitdown not installed. Falling back to PyMuPDF text.")
        return extract_as_text(doc, start_phys, end_phys)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        slice_sub_pdf(doc, start_phys, end_phys, tmp_path)
        md_engine = MarkItDown()
        result = md_engine.convert(tmp_path)
        content = result.text_content
        if output_md:
            with open(output_md, "w", encoding="utf-8") as f:
                f.write(content)
        return content
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Extract PDF chapter on-demand.")
    parser.add_argument("pdf_path", help="Path to PDF file")
    parser.add_argument("--chapter", "-c", help="Chapter title keyword (e.g. 'Chapter 1', '第2章')")
    parser.add_argument("--range", "-r", help="Physical page range 'start-end' (1-based)")
    parser.add_argument("--format", "-f", choices=["text", "md", "pdf", "image"], default="text", help="Output format")
    parser.add_argument("--output", "-o", help="Output file or directory path")
    parser.add_argument("--dpi", type=int, default=150, help="DPI for image rendering")

    args = parser.parse_args()
    pdf_path = os.path.abspath(args.pdf_path)
    if not os.path.exists(pdf_path):
        print(f"Error: PDF not found: {pdf_path}")
        sys.exit(1)

    doc = pymupdf.open(pdf_path)

    start_p, end_p, ch_title = None, None, ""
    if args.range:
        parts = args.range.split("-")
        start_p = int(parts[0])
        end_p = int(parts[1]) if len(parts) > 1 else start_p
        ch_title = f"Page_{start_p}_{end_p}"
    elif args.chapter:
        ch_title, start_p, end_p = find_chapter_range(doc, args.chapter)
        if start_p is None:
            print(f"Error: Could not find chapter matching '{args.chapter}' in TOC.")
            sys.exit(1)
    else:
        print("Error: Must specify either --chapter or --range.")
        sys.exit(1)

    page_count = end_p - start_p + 1
    print(f"[*] Target: {ch_title} | Physical Pages: {start_p} to {end_p} ({page_count} pages)", file=sys.stderr)

    # Sanity Check
    first_lines = [l.strip() for l in doc[start_p - 1].get_text().splitlines() if l.strip()][:3]
    print(f"[*] Sanity Check (Page {start_p} first lines): {first_lines}", file=sys.stderr)

    if args.format == "text":
        txt = extract_as_text(doc, start_p, end_p)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(txt)
            print(f"Saved text to: {args.output}")
        else:
            print(txt)

    elif args.format == "md":
        md = extract_as_markdown(doc, start_p, end_p, args.output)
        if not args.output:
            print(md)
        else:
            print(f"Saved Markdown to: {args.output}")

    elif args.format == "pdf":
        out_path = args.output or f"{ch_title.replace(':', '_')}.pdf"
        slice_sub_pdf(doc, start_p, end_p, out_path)
        print(f"Sliced PDF saved to: {out_path} ({os.path.getsize(out_path)} bytes)")

    elif args.format == "image":
        out_dir = args.output or os.path.join(os.path.dirname(pdf_path), ".cache", f"{ch_title}_images")
        imgs = render_images(doc, start_p, end_p, out_dir, args.dpi)
        print(f"Rendered {len(imgs)} pages to: {out_dir}")


if __name__ == "__main__":
    main()
