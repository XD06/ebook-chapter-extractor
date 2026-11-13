#!/usr/bin/env python3
"""
extract_chapter.py - 电子书按需章节提取与切片工具 (支持 PDF / EPUB / MOBI / AZW3)
- PDF: 支持按章节标题或物理页范围提取为纯文本、Markdown、独立子 PDF 切片或渲染高清图像；
       遇扫描版/无文本层 PDF 时，自动智能降级至高清渲染 + 本地 OCR 双通道输出 (视觉直读原图 + 离线文本兜底)；
- EPUB / MOBI / AZW3: 支持按章节标题或序号提取为结构化 Markdown、纯文本或原始 HTML，
       支持代码块智能识别与插图 RapidOCR 回填。
"""

import sys
import os
import re
import argparse
import tempfile
import json

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import console_safe  # noqa: F401  # 安装窄编码控制台保护（含非 GBK 字符的文件名）
from env_checker import ensure_core_dependencies, ensure_ocr_dependencies
from ocr_helper import get_cache_dir, ocr_image, ocr_images_batch, clean_ocr_text, clean_code_ocr
from pdf_helper import (
    detect_ebook_format,
    sanitize_filename,
    find_pdf_chapter_range,
    is_page_scanned,
    extract_pdf_as_markdown_fast,
)
ensure_core_dependencies()

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None


# ==============================================================================
# PDF 提取实现部分
# ==============================================================================


def extract_pdf_as_text(doc, start_phys: int, end_phys: int, column_aware: bool = False) -> str:
    """按页提取文本；column_aware=True 时对双栏页面做「先左栏后右栏」阅读顺序重排"""
    if column_aware:
        from paper_parser import page_text_column_aware
    chunks = []
    for pno in range(start_phys - 1, end_phys):
        text = page_text_column_aware(doc[pno]) if column_aware else doc[pno].get_text()
        chunks.append(f"<!-- Page {pno + 1} -->\n{text}")
    return "\n\n".join(chunks)


def slice_sub_pdf(doc, start_phys: int, end_phys: int, output_path: str):
    sub = pymupdf.open()
    for pno in range(start_phys - 1, end_phys):
        sub.insert_pdf(doc, from_page=pno, to_page=pno)
    sub.save(output_path)
    sub.close()


def render_images(doc, start_phys: int, end_phys: int, output_dir: str, dpi: int = 150) -> list:
    os.makedirs(output_dir, exist_ok=True)
    images = []
    for pno in range(start_phys - 1, end_phys):
        page = doc[pno]
        pix = page.get_pixmap(dpi=dpi)
        img_path = os.path.join(output_dir, f"page_{pno + 1:04d}.png")
        pix.save(img_path)
        images.append(img_path)
    return images


def extract_pdf_as_markdown(doc, start_phys: int, end_phys: int, output_md: str = None, engine: str = "auto") -> str:
    if engine in ("auto", "pymupdf"):
        content = extract_pdf_as_markdown_fast(doc, start_phys, end_phys)
        if not content.strip():
            content = extract_pdf_as_text(doc, start_phys, end_phys)
        if output_md:
            with open(output_md, "w", encoding="utf-8") as f:
                f.write(content)
        return content

    try:
        from markitdown import MarkItDown
    except ImportError:
        print("Warning: markitdown not installed. Falling back to fast PyMuPDF Markdown.", file=sys.stderr)
        content = extract_pdf_as_markdown_fast(doc, start_phys, end_phys)
        if output_md:
            with open(output_md, "w", encoding="utf-8") as f:
                f.write(content)
        return content

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


def handle_pdf_extraction(args, pdf_path: str):
    """处理 PDF 提取，包含扫描件自动降级与双通道输出机制"""
    if pymupdf is None:
        print("Error: PyMuPDF is required. Run: pip install pymupdf")
        sys.exit(1)

    doc = pymupdf.open(pdf_path)
    try:
        if doc.needs_pass:
            print("Error: PDF is password-protected (encrypted).")
            sys.exit(1)

        total_pages = doc.page_count
        start_p, end_p, ch_title = None, None, ""

        if args.range:
            parts = args.range.split("-")
            start_p = int(parts[0])
            end_p = int(parts[1]) if len(parts) > 1 and parts[1].strip() else start_p
            if start_p > end_p:
                start_p, end_p = end_p, start_p
            start_p = max(1, min(total_pages, start_p))
            end_p = max(start_p, min(total_pages, end_p))
            ch_title = f"Page_{start_p}_{end_p}"
        elif args.index is not None:
            ch_title, start_p, end_p = find_pdf_chapter_range(doc, chapter_index=args.index, pdf_path=pdf_path)
            if start_p is None:
                print(f"Error: Could not find chapter index {args.index} in PDF TOC/index.", file=sys.stderr)
                print("[*] Hint: `python scripts/build_index.py <pdf> --print` lists detected headings; or use --range <start-end>.", file=sys.stderr)
                sys.exit(1)
        elif args.chapter:
            if getattr(args, "paper_mode", False):
                from paper_parser import build_virtual_index, find_in_virtual_index
                ch_title, start_p, end_p = find_in_virtual_index(args.chapter, build_virtual_index(pdf_path))
            else:
                ch_title, start_p, end_p = find_pdf_chapter_range(doc, chapter_query=args.chapter, pdf_path=pdf_path)
            if start_p is None:
                print(f"Error: Could not find chapter matching '{args.chapter}' in TOC or virtual layout index.", file=sys.stderr)
                print("[*] Hint: `python scripts/build_index.py <pdf> --print` lists detected headings; or use --range <start-end>.", file=sys.stderr)
                sys.exit(1)
        else:
            print("Error: Must specify --chapter, --index, or --range for PDF.")
            sys.exit(1)

        page_count = end_p - start_p + 1
        print(f"[*] Target: {ch_title} | Physical Pages: {start_p} to {end_p} ({page_count} pages)", file=sys.stderr)

        # 逐页嗅探是否为纯扫描/图像版，避免混合章节误判
        scanned_count = sum(1 for pno in range(start_p - 1, end_p) if is_page_scanned(doc[pno]))
        total_chars = sum(len(doc[pno].get_text().strip()) for pno in range(start_p - 1, end_p))
        is_scanned = args.ocr or (scanned_count >= max(1, (page_count + 1) // 2))

        # 1. 显式指定切片为子 PDF
        if args.format == "pdf":
            out_path = args.output or f"{sanitize_filename(ch_title)}.pdf"
            slice_sub_pdf(doc, start_p, end_p, out_path)
            print(f"Sliced PDF saved to: {out_path} ({os.path.getsize(out_path)} bytes)")
            return

        # 2. 显式指定渲染图像
        if args.format == "image":
            pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
            out_dir = args.output or get_cache_dir(pdf_path, f"{pdf_stem}_images")
            imgs = render_images(doc, start_p, end_p, out_dir, args.dpi)
            print(f"Rendered {len(imgs)} pages to: {out_dir}")
            return

        # 3. 扫描版 PDF 智能降级与双通道输出 (Dual-Channel Output)
        if is_scanned:
            ensure_ocr_dependencies()
            pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
            out_dir = get_cache_dir(pdf_path, f"{pdf_stem}_images")
            imgs = render_images(doc, start_p, end_p, out_dir, args.dpi)
            print(f"[*] Scanned PDF detected ({total_chars} chars extracted). Rendered {len(imgs)} pages for Agent Vision.", file=sys.stderr)

            ocr_results = ocr_images_batch(imgs, engine_name=args.ocr_engine)
            ocr_blocks = []
            for i, (_, ocr_res) in enumerate(zip(imgs, ocr_results)):
                phys_num = start_p + i
                cleaned_res = clean_ocr_text(ocr_res)
                ocr_blocks.append(f"<!-- Page {phys_num} -->\n{cleaned_res}")

            ocr_full_text = "\n\n".join(ocr_blocks)
            img_bullet_list = "\n".join([f"- Page {start_p + i}: `{img_path}`" for i, img_path in enumerate(imgs)])
            dual_channel_md = f"""# {ch_title}

> 💡 **Agent 视觉直读模式**：本章节为扫描件/图层版，已自动切片渲染为高清图像。推荐 Agent 多模态视觉模型直接阅读下列图片以获得 100% 精确排版、代码与公式；下方同时附带离线 OCR 文本作为快速参考通道。

### 🖼️ 高清视觉渲染切片（共 {len(imgs)} 页）
{img_bullet_list}

---

### 📝 离线 OCR 提取文本通道
{ocr_full_text}
"""
            final_content = dual_channel_md if args.format == "md" else ocr_full_text

            if getattr(args, "json", False):
                result = {
                    "success": True,
                    "file": pdf_path,
                    "title": ch_title,
                    "start_phys": start_p,
                    "end_phys": end_p,
                    "page_count": page_count,
                    "format": args.format,
                    "is_scanned": True,
                    "images_dumped": imgs,
                    "content": final_content,
                    "stats": {
                        "char_count": len(final_content),
                        "line_count": len(final_content.splitlines()),
                        "image_count": len(imgs),
                    }
                }
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    print(f"Saved JSON to: {args.output}")
                else:
                    print(json.dumps(result, ensure_ascii=True, indent=2))
                return

            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(final_content)
                print(f"Saved dual-channel extraction to: {args.output}")
            else:
                print(final_content)
            return

        # 4. 正常数字版 PDF 文本/Markdown 提取
        first_lines = [l.strip() for l in doc[start_p - 1].get_text().splitlines() if l.strip()][:3]
        print(f"[*] Sanity Check (Page {start_p} first lines): {first_lines}", file=sys.stderr)

        if args.format == "text":
            content = extract_pdf_as_text(doc, start_p, end_p, column_aware=getattr(args, "column_aware", False))
        else:  # md
            content = extract_pdf_as_markdown(doc, start_p, end_p, output_md=None, engine=getattr(args, "engine", "auto"))

        # 显式开关：仅当用户要求时剥离文末参考文献，避免误伤普通电子书
        if getattr(args, "strip_refs", False):
            from paper_parser import strip_references
            content = strip_references(content)

        if getattr(args, "json", False):
            result = {
                "success": True,
                "file": pdf_path,
                "title": ch_title,
                "start_phys": start_p,
                "end_phys": end_p,
                "page_count": page_count,
                "format": args.format,
                "is_scanned": False,
                "images_dumped": [],
                "content": content,
                "stats": {
                    "char_count": len(content),
                    "line_count": len(content.splitlines()),
                    "image_count": 0,
                }
            }
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"Saved JSON to: {args.output}")
            else:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            return

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Saved to: {args.output}")
        else:
            print(content)
    finally:
        doc.close()


# ==============================================================================
# EPUB / MOBI / AZW3 提取实现部分
# ==============================================================================

def handle_ebook_extraction(args, file_path: str, ext: str):
    """处理 EPUB / MOBI / AZW3 电子书提取"""
    if args.ocr:
        ensure_ocr_dependencies()

    if ext == ".epub":
        from epub_parser import EpubBook
        book_ctx = EpubBook(file_path)
    else:
        from mobi_parser import MobiBook
        book_ctx = MobiBook(file_path)

    # 智能原图导出逻辑
    dump_images_dir = None
    if args.dump_images:
        if args.dump_images is True or args.dump_images == "":
            dump_images_dir = get_cache_dir(file_path, "images")
        else:
            dump_images_dir = os.path.abspath(args.dump_images)
    elif args.ocr:
        dump_images_dir = get_cache_dir(file_path, "images")

    with book_ctx as book:
        ch_target = None
        if args.index:
            ch_target = book.find_chapter(index=args.index)
        elif args.chapter:
            ch_target = book.find_chapter(query=args.chapter)
        elif args.range:
            try:
                parts = args.range.split("-")
                idx1 = int(parts[0])
                idx2 = int(parts[1]) if len(parts) > 1 and parts[1].strip() else idx1
                md_chunks = []
                seen_targets = set()
                for i in range(idx1, idx2 + 1):
                    item = book.find_chapter(index=i)
                    if item:
                        dedup_key = (item.get("file_path"), item.get("anchor"), item.get("char_offset"))
                        if dedup_key in seen_targets:
                            continue
                        seen_targets.add(dedup_key)
                        md_chunks.append(book.extract_chapter_markdown(
                            item,
                            dump_images_dir=dump_images_dir,
                            ocr=args.ocr,
                            ocr_engine=args.ocr_engine
                        ))
                full_md = "\n\n---\n\n".join(md_chunks)
                if getattr(args, "json", False):
                    result = {
                        "success": True,
                        "file": file_path,
                        "range": f"{idx1}-{idx2}",
                        "format": args.format,
                        "content": full_md,
                        "stats": {
                            "char_count": len(full_md),
                            "line_count": len(full_md.splitlines()),
                        }
                    }
                    if args.output:
                        with open(args.output, "w", encoding="utf-8") as f:
                            json.dump(result, f, ensure_ascii=False, indent=2)
                        print(f"Saved JSON to: {args.output}")
                    else:
                        print(json.dumps(result, ensure_ascii=True, indent=2))
                    return
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as f:
                        f.write(full_md)
                    print(f"Saved extracted chapters [{idx1}-{idx2}] to: {args.output}")
                else:
                    print(full_md)
                return
            except Exception as e:
                print(f"Error parsing index range: {e}")
                sys.exit(1)
        else:
            print("Error: Must specify --chapter, --index or --range.")
            sys.exit(1)

        if not ch_target:
            print(f"Error: Could not find chapter matching criteria (chapter='{args.chapter}', index={args.index})")
            sys.exit(1)

        print(f"[*] Target Chapter: [{ch_target.get('index', 0)}] {ch_target['title']}", file=sys.stderr)

        ch_prefix = f"ch{ch_target.get('index', 1):03d}_img"
        if args.format == "html" and hasattr(book, "extract_chapter_html"):
            content = book.extract_chapter_html(ch_target)
        else:
            content = book.extract_chapter_markdown(
                ch_target,
                dump_images_dir=dump_images_dir,
                ocr=args.ocr,
                ocr_engine=args.ocr_engine
            )

        char_count = len(content)
        line_count = len(content.splitlines())
        dumped_imgs = []
        if dump_images_dir and os.path.exists(dump_images_dir):
            dumped_imgs = sorted(
                os.path.join(dump_images_dir, f)
                for f in os.listdir(dump_images_dir)
                if f.startswith(ch_prefix) and os.path.isfile(os.path.join(dump_images_dir, f))
            )

        if getattr(args, "json", False):
            result = {
                "success": True,
                "file": file_path,
                "chapter_index": ch_target.get("index"),
                "chapter_level": ch_target.get("level", 1),
                "title": ch_target.get("title", ""),
                "format": args.format,
                "content": content,
                "images_dumped": dumped_imgs,
                "stats": {
                    "char_count": char_count,
                    "line_count": line_count,
                    "image_count": len(dumped_imgs),
                }
            }
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                print(f"Saved JSON to: {args.output}")
            else:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            return

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Saved to: {args.output}")
            if dump_images_dir and os.path.exists(dump_images_dir):
                print(f"[*] Dumped {len(dumped_imgs)} images to: {dump_images_dir}")
        else:
            print(content)


def main():
    parser = argparse.ArgumentParser(description="Extract eBook chapter on-demand (PDF/EPUB/MOBI/AZW3).")
    parser.add_argument("file_path", help="Path to eBook file (PDF, EPUB, MOBI, AZW3)")
    parser.add_argument("--chapter", "-c", help="Chapter title keyword or number (e.g. '2.3.2', '8.2', 'Chapter 1')")
    parser.add_argument("--index", "-i", type=int, help="Chapter index number (1-based, mainly for EPUB/MOBI)")
    parser.add_argument("--range", "-r", help="Physical page range for PDF ('start-end') or chapter range for EPUB ('1-3')")
    parser.add_argument("--format", "-f", choices=["text", "md", "pdf", "image", "html"], default="md", help="Output format (default: md)")
    parser.add_argument("--engine", choices=["auto", "pymupdf", "markitdown"], default="auto", help="PDF Markdown engine (default: auto fast PyMuPDF)")
    parser.add_argument("--output", "-o", help="Output file or directory path")
    parser.add_argument("--dpi", type=int, default=150, help="DPI for PDF image rendering")
    parser.add_argument("--ocr", action="store_true", help="Enable OCR recognition for embedded images / scanned PDF into Markdown")
    parser.add_argument("--dump-images", nargs="?", const=True, default=False, help="Dump embedded chapter images to folder (for Vision LLM reading)")
    parser.add_argument("--ocr-engine", choices=["paddleocr", "rapidocr", "paddle", "rapid"], help="Specify OCR engine: 'paddleocr' (Baidu AI Studio API) or 'rapidocr' (local fallback, default: auto)")
    parser.add_argument("--paddle-token", help="Baidu PaddleOCR API Token (overrides environment variable / config)")
    parser.add_argument("--json", action="store_true", help="Output result as JSON object with metadata (Agent friendly)")
    parser.add_argument("--strip-refs", action="store_true", help="[Paper Mode] Strip trailing References/Bibliography section (opt-in; never applied to eBooks by default)")
    parser.add_argument("--column-aware", action="store_true", help="[Paper Mode] Reorder two-column PDF text left-column-first (for --format text)")
    parser.add_argument("--paper-mode", action="store_true", help="[Paper Mode] Force layout-based virtual index even when bookmarks exist")

    args = parser.parse_args()
    if args.paddle_token:
        os.environ["PADDLEOCR_TOKEN"] = args.paddle_token.strip()
    file_path = os.path.abspath(args.file_path)
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        sys.exit(1)

    fmt = detect_ebook_format(file_path)
    if fmt == "epub":
        handle_ebook_extraction(args, file_path, ".epub")
    elif fmt == "mobi":
        handle_ebook_extraction(args, file_path, ".mobi")
    else:
        handle_pdf_extraction(args, file_path)


if __name__ == "__main__":
    main()
