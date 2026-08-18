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

from env_checker import ensure_core_dependencies, ensure_ocr_dependencies
from ocr_helper import get_cache_dir, ocr_image, ocr_images_batch, clean_ocr_text, clean_code_ocr
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

def find_pdf_chapter_range(doc, chapter_query: str) -> tuple:
    """PDF 专用：在书签中查找匹配章节的物理起止页（支持前缀、数字、模糊匹配与书签自愈）"""
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

    # 1. 严格全等匹配
    for i, it in enumerate(cleaned):
        if it["phys"] > 0 and it["title"].strip().lower() == q_lower:
            matched_idx = i
            break

    # 2. 章节号前缀/边界匹配 (e.g. "2.3.2" 匹配 "2.3.2 指针" 或 "第2章" 匹配 "第2章 程序结构")
    if matched_idx == -1:
        pattern = r'(?:^|[\s第])' + re.escape(q_lower) + r'(?:[\s.、:：章节]|$)'
        for i, it in enumerate(cleaned):
            if it["phys"] > 0:
                t_lower = it["title"].lower()
                if re.search(pattern, t_lower) or t_lower.startswith(q_lower):
                    matched_idx = i
                    break

    # 3. 归一化子串匹配
    if matched_idx == -1:
        for i, it in enumerate(cleaned):
            if it["phys"] > 0:
                t_norm = re.sub(r'[\s\-_.:：·]+', '', it["title"].lower())
                if q_norm in t_norm:
                    matched_idx = i
                    break

    # 4. 普通子串包含匹配
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


def extract_pdf_as_text(doc, start_phys: int, end_phys: int) -> str:
    chunks = []
    for pno in range(start_phys - 1, end_phys):
        text = doc[pno].get_text()
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


def extract_pdf_as_markdown(doc, start_phys: int, end_phys: int, output_md: str = None) -> str:
    try:
        from markitdown import MarkItDown
    except ImportError:
        print("Warning: markitdown not installed. Falling back to PyMuPDF text.", file=sys.stderr)
        return extract_pdf_as_text(doc, start_phys, end_phys)

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
    start_p, end_p, ch_title = None, None, ""

    if args.range:
        parts = args.range.split("-")
        start_p = int(parts[0])
        end_p = int(parts[1]) if len(parts) > 1 else start_p
        ch_title = f"Page_{start_p}_{end_p}"
    elif args.chapter:
        ch_title, start_p, end_p = find_pdf_chapter_range(doc, args.chapter)
        if start_p is None:
            print(f"Error: Could not find chapter matching '{args.chapter}' in TOC.")
            sys.exit(1)
    else:
        print("Error: Must specify either --chapter or --range for PDF.")
        sys.exit(1)

    page_count = end_p - start_p + 1
    print(f"[*] Target: {ch_title} | Physical Pages: {start_p} to {end_p} ({page_count} pages)", file=sys.stderr)

    # 嗅探目标页面是否为纯扫描/图像版（无内置文字层）
    raw_page_texts = [doc[pno].get_text().strip() for pno in range(start_p - 1, end_p)]
    total_chars = sum(len(t) for t in raw_page_texts)
    is_scanned = (total_chars < 30 * page_count) or args.ocr

    # 1. 显式指定切片为子 PDF
    if args.format == "pdf":
        out_path = args.output or f"{ch_title.replace(':', '_')}.pdf"
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
        if args.ocr:
            ensure_ocr_dependencies()
        pdf_stem = os.path.splitext(os.path.basename(pdf_path))[0]
        out_dir = get_cache_dir(pdf_path, f"{pdf_stem}_images")
        imgs = render_images(doc, start_p, end_p, out_dir, args.dpi)
        print(f"[*] Scanned PDF detected ({total_chars} chars extracted). Rendered {len(imgs)} pages for Agent Vision.", file=sys.stderr)

        # 运行 OCR 文本识别（多图/多页统一批量极速提取，Paddle 批量 API + RapidOCR 兜底）
        ocr_results = ocr_images_batch(imgs, engine_name=args.ocr_engine)
        ocr_blocks = []
        for i, (img_p, ocr_res) in enumerate(zip(imgs, ocr_results)):
            phys_num = start_p + i
            cleaned_res = clean_ocr_text(ocr_res)
            ocr_blocks.append(f"<!-- Page {phys_num} -->\n{cleaned_res}")

        ocr_full_text = "\n\n".join(ocr_blocks)

        # 构建双通道 Markdown 内容
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
                print(json.dumps(result, ensure_ascii=False, indent=2))
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
        content = extract_pdf_as_text(doc, start_p, end_p)
    else:  # md
        content = extract_pdf_as_markdown(doc, start_p, end_p, args.output)

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
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Saved to: {args.output}")
    else:
        print(content)


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
                idx2 = int(parts[1]) if len(parts) > 1 else idx1
                md_chunks = []
                for i in range(idx1, idx2 + 1):
                    item = book.find_chapter(index=i)
                    if item:
                        md_chunks.append(book.extract_chapter_markdown(
                            item,
                            dump_images_dir=dump_images_dir,
                            ocr=args.ocr,
                            ocr_engine=args.ocr_engine
                        ))
                full_md = "\n\n---\n\n".join(md_chunks)
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
            dumped_imgs = [
                os.path.join(dump_images_dir, f)
                for f in os.listdir(dump_images_dir)
                if os.path.isfile(os.path.join(dump_images_dir, f))
            ]

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
                print(json.dumps(result, ensure_ascii=False, indent=2))
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
    parser.add_argument("--output", "-o", help="Output file or directory path")
    parser.add_argument("--dpi", type=int, default=150, help="DPI for PDF image rendering")
    parser.add_argument("--ocr", action="store_true", help="Enable OCR recognition for embedded images / scanned PDF into Markdown")
    parser.add_argument("--dump-images", nargs="?", const=True, default=False, help="Dump embedded chapter images to folder (for Vision LLM reading)")
    parser.add_argument("--ocr-engine", choices=["paddleocr", "rapidocr", "paddle", "rapid"], help="Specify OCR engine: 'paddleocr' (Baidu AI Studio API) or 'rapidocr' (local fallback, default: auto)")
    parser.add_argument("--paddle-token", help="Baidu PaddleOCR API Token (overrides environment variable / config)")
    parser.add_argument("--json", action="store_true", help="Output result as JSON object with metadata (Agent friendly)")

    args = parser.parse_args()
    if args.paddle_token:
        os.environ["PADDLEOCR_TOKEN"] = args.paddle_token.strip()
    file_path = os.path.abspath(args.file_path)
    if not os.path.exists(file_path):
        print(f"Error: File not found: {file_path}")
        sys.exit(1)

    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".epub", ".mobi", ".azw", ".azw3", ".prc"):
        handle_ebook_extraction(args, file_path, ext)
    else:
        handle_pdf_extraction(args, file_path)


if __name__ == "__main__":
    main()
