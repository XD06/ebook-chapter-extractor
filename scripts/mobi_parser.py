#!/usr/bin/env python3
"""
mobi_parser.py - MOBI / AZW / AZW3 (KF8) 格式电子书解析与按需提取引擎
纯 Python 实现，零外部 C / 二进制依赖，支持 PalmDOC LZ77 解压缩、KF8 (AZW3) 识别与章节提取。
"""

import os
import sys
import re
import struct
import tempfile
import json
from urllib.parse import unquote, urldefrag
from typing import List, Dict, Tuple, Optional, Any

from epub_parser import EpubBook, enhance_pre_code_tags, purge_useless_navigation

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    import markdownify
except ImportError:
    markdownify = None

try:
    import html2text
except ImportError:
    html2text = None

from ocr_helper import detect_image_ext, ocr_image, format_ocr_markdown, get_cache_dir
from mathml_helper import convert_soup_mathml_to_latex


def decompress_palmdoc(data: bytes) -> bytes:
    """PalmDOC LZ77 解压缩算法 (纯 Python 实现)"""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        byte = data[i]
        i += 1
        if byte == 0x00:
            out.append(0)
        elif 1 <= byte <= 8:
            out.extend(data[i:i + byte])
            i += byte
        elif byte <= 0x7F:
            out.append(byte)
        elif 0x80 <= byte <= 0xBF:
            if i >= n:
                break
            next_byte = data[i]
            i += 1
            distance = ((byte & 0x3F) << 3) | (next_byte >> 5)
            length = (next_byte & 0x1F) + 3
            start_pos = len(out) - distance
            for _ in range(length):
                if 0 <= start_pos < len(out):
                    out.append(out[start_pos])
                    start_pos += 1
                else:
                    out.append(32)  # 空格兜底
        else:  # 0xC0 .. 0xFF
            out.append(32)  # 空格
            out.append(byte ^ 0x80)
    return bytes(out)


class MobiBook:
    """MOBI / AZW / AZW3 (KF8) 电子书解析器"""

    def __init__(self, mobi_path: str):
        self.mobi_path = os.path.abspath(mobi_path)
        if not os.path.exists(self.mobi_path):
            raise FileNotFoundError(f"MOBI file not found: {self.mobi_path}")

        with open(self.mobi_path, "rb") as f:
            self.raw_data = f.read()

        self.records = self._parse_pdb_records()
        self.header_info = self._parse_mobi_header()
        self.is_kf8, self.epub_book, self.kf8_tmp_path = self._try_init_kf8()

        if not self.is_kf8:
            # 传统 Mobi6 HTML 处理
            self.full_html = self._extract_raw_html()
            self.toc = self._build_toc_from_html(self.full_html)
        else:
            self.full_html = ""
            self.toc = self.epub_book.toc if self.epub_book else []

    def close(self):
        if self.epub_book:
            self.epub_book.close()
        if self.kf8_tmp_path and os.path.exists(self.kf8_tmp_path):
            try:
                os.remove(self.kf8_tmp_path)
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _parse_pdb_records(self) -> List[bytes]:
        """解析 Palm Database 格式的记录列表"""
        if len(self.raw_data) < 78:
            raise ValueError("Invalid MOBI/PDB file: header too short.")

        num_records = struct.unpack(">H", self.raw_data[76:78])[0]
        record_offsets = []

        for i in range(num_records):
            pos = 78 + i * 8
            if pos + 4 > len(self.raw_data):
                break
            offset = struct.unpack(">I", self.raw_data[pos:pos + 4])[0]
            record_offsets.append(offset)

        records = []
        for i in range(len(record_offsets)):
            start = record_offsets[i]
            end = record_offsets[i + 1] if i + 1 < len(record_offsets) else len(self.raw_data)
            records.append(self.raw_data[start:end])

        return records

    def _parse_mobi_header(self) -> Dict[str, Any]:
        """解析 Record 0 获取 PalmDOC & MOBI 头部核心元数据"""
        if not self.records:
            return {}

        rec0 = self.records[0]
        if len(rec0) < 16:
            return {}

        compression = struct.unpack(">H", rec0[0:2])[0]
        text_length = struct.unpack(">I", rec0[4:8])[0]
        text_records_count = struct.unpack(">H", rec0[8:10])[0]
        record_size = struct.unpack(">H", rec0[10:12])[0]

        encoding = "utf-8"
        first_image_index = None
        has_kf8_boundary = False
        kf8_boundary_offset = -1

        # 检查是否包含 MOBI 标识
        if len(rec0) >= 24 and rec0[16:20] == b"MOBI":
            header_len = struct.unpack(">I", rec0[20:24])[0]
            mobi_type = struct.unpack(">I", rec0[24:28])[0] if len(rec0) >= 28 else 0
            codepage = struct.unpack(">I", rec0[28:32])[0] if len(rec0) >= 32 else 1252

            if codepage == 65001:
                encoding = "utf-8"
            elif codepage == 1252:
                encoding = "windows-1252"
            elif codepage == 936:
                encoding = "gbk"

            if len(rec0) >= 112:
                first_image_index = struct.unpack(">I", rec0[108:112])[0]

            if len(rec0) >= 136:
                exth_flags = struct.unpack(">I", rec0[128:132])[0]
                if exth_flags & 0x40:
                    pass

            if len(rec0) >= 196:
                boundary_rec = struct.unpack(">I", rec0[192:196])[0]
                if boundary_rec != 0xFFFFFFFF and boundary_rec < len(self.records):
                    has_kf8_boundary = True
                    kf8_boundary_offset = boundary_rec

        title = ""
        if len(rec0) >= 88:
            full_name_offset = struct.unpack(">I", rec0[84:88])[0]
            full_name_length = struct.unpack(">I", rec0[88:92])[0] if len(rec0) >= 92 else 0
            if full_name_offset < len(rec0) and full_name_offset + full_name_length <= len(rec0):
                raw_name = rec0[full_name_offset:full_name_offset + full_name_length]
                try:
                    title = raw_name.decode(encoding, errors="replace").strip()
                except Exception:
                    title = raw_name.decode("utf-8", errors="replace").strip()

        if not title:
            raw_title = self.raw_data[:32].split(b"\x00")[0]
            title = raw_title.decode("latin1", errors="replace").strip()

        return {
            "title": title,
            "compression": compression,
            "text_length": text_length,
            "text_records_count": text_records_count,
            "record_size": record_size,
            "encoding": encoding,
            "first_image_index": first_image_index,
            "has_kf8_boundary": has_kf8_boundary,
            "kf8_boundary_offset": kf8_boundary_offset,
        }

    def _try_init_kf8(self) -> Tuple[bool, Optional[EpubBook], Optional[str]]:
        """检测并尝试从 AZW3/KF8 复合体中提取完整的 EPUB 容器"""
        kf8_start = -1
        if self.header_info.get("has_kf8_boundary"):
            kf8_start = self.header_info["kf8_boundary_offset"]

        if kf8_start < 0:
            for i, rec in enumerate(self.records):
                if rec.startswith(b"BOUNDARY") or (len(rec) >= 20 and rec[16:20] == b"KF8\x00"):
                    kf8_start = i
                    break

        if kf8_start >= 0 and kf8_start < len(self.records):
            try:
                for rec in self.records[kf8_start:]:
                    if rec.startswith(b"PK\x03\x04"):
                        tmp = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
                        tmp.write(rec)
                        tmp.close()
                        try:
                            epub = EpubBook(tmp.name)
                            return True, epub, tmp.name
                        except Exception:
                            if os.path.exists(tmp.name):
                                os.remove(tmp.name)
            except Exception:
                pass

        try:
            import mobi
            tmpdir, filepath = mobi.extract(self.mobi_path)
            if filepath.lower().endswith(".epub") and os.path.exists(filepath):
                epub = EpubBook(filepath)
                return True, epub, None
        except Exception:
            pass

        return False, None, None

    def _extract_raw_html(self) -> str:
        """解压 Record 1..N 获取 Mobi6 原始 HTML 文本"""
        compression = self.header_info.get("compression", 1)
        text_records_count = self.header_info.get("text_records_count", 0)
        encoding = self.header_info.get("encoding", "utf-8")

        decompressed_chunks = []
        for i in range(1, min(text_records_count + 1, len(self.records))):
            rec = self.records[i]
            if compression == 2:  # PalmDOC LZ77
                decompressed_chunks.append(decompress_palmdoc(rec))
            elif compression == 1:  # 无压缩
                decompressed_chunks.append(rec)
            else:
                try:
                    decompressed_chunks.append(decompress_palmdoc(rec))
                except Exception:
                    decompressed_chunks.append(rec)

        full_bytes = b"".join(decompressed_chunks)
        try:
            return full_bytes.decode(encoding, errors="replace")
        except Exception:
            return full_bytes.decode("utf-8", errors="replace")

    def _build_toc_from_html(self, html_text: str) -> List[Dict[str, Any]]:
        """从解压的 Mobi HTML 文本中智能识别章节断点与目录"""
        toc = []
        if not html_text:
            return toc

        # 方案 A: 寻找 HTML 标题标签 <h1>, <h2>, <h3>
        header_matches = list(re.finditer(r"<(h[1-4])[^>]*>(.*?)</\1>", html_text, re.IGNORECASE | re.DOTALL))
        if len(header_matches) >= 2:
            for i, m in enumerate(header_matches):
                tag_name = m.group(1).lower()
                level = int(tag_name[1])
                clean_title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if clean_title and len(clean_title) < 100:
                    toc.append({
                        "index": i + 1,
                        "title": clean_title,
                        "level": level,
                        "char_offset": m.start(),
                    })
            if len(toc) >= 2:
                return toc

        # 方案 B: 寻找带 name 或 id 的 <a> 锚点标签
        anchor_matches = list(re.finditer(r'<a\s+[^>]*(?:name|id)=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_text, re.IGNORECASE | re.DOTALL))
        valid_anchors = []
        for m in anchor_matches:
            anchor_name = m.group(1)
            raw_text = m.group(2)
            clean_title = re.sub(r"<[^>]+>", "", raw_text).strip()
            if not clean_title:
                after_text = html_text[m.end():m.end() + 200]
                lines = [l.strip() for l in re.sub(r"<[^>]+>", "", after_text).splitlines() if l.strip()]
                if lines:
                    clean_title = lines[0]

            if clean_title and len(clean_title) < 80:
                is_chapter_like = any(kw in clean_title for kw in [
                    "第", "章", "Chapter", "Section", "前言", "附录", "目录", "序言", "后记", "引言", "Overview"
                ]) or bool(re.match(r"^\d+[\.\s、]", clean_title))

                if is_chapter_like:
                    valid_anchors.append({
                        "title": clean_title,
                        "level": 1,
                        "anchor": anchor_name,
                        "char_offset": m.start(),
                    })

        if len(valid_anchors) >= 2:
            for i, t in enumerate(valid_anchors):
                t["index"] = i + 1
            return valid_anchors

        # 方案 C: 智能正则嗅探纯文本/加粗中的章节标题模式
        regex_patterns = [
            r'<p[^>]*>(?:<b[^>]*>|<strong[^>]*>)?\s*(第[0-9一二三四五六七八九十百]+[章节篇卷部]\s*[^<\n]+)(?:</b>|</strong>)?\s*</p>',
            r'<p[^>]*>(?:<b[^>]*>|<strong[^>]*>)?\s*(Chapter\s+\d+[^<\n]*)(?:</b>|</strong>)?\s*</p>',
            r'<p[^>]*>(?:<b[^>]*>|<strong[^>]*>)?\s*(\d+\.\d+(?:\.\d+)?\s+[^<\n]+)(?:</b>|</strong>)?\s*</p>',
            r'<p[^>]*class=["\'](?:chapter|title|heading|head|bt)["\'][^>]*>(.*?)</p>',
            r'<p[^>]*>(?:<b[^>]*>|<strong[^>]*>)\s*([^<\n]{2,40})\s*(?:</b>|</strong>)\s*</p>',
        ]

        for pat in regex_patterns:
            matches = list(re.finditer(pat, html_text, re.IGNORECASE))
            if len(matches) >= 2:
                for i, m in enumerate(matches):
                    clean_title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                    if clean_title and len(clean_title) <= 60:
                        level = 1
                        if re.match(r"^\d+\.\d+\.\d+", clean_title):
                            level = 3
                        elif re.match(r"^\d+\.\d+", clean_title):
                            level = 2
                        toc.append({
                            "index": len(toc) + 1,
                            "title": clean_title,
                            "level": level,
                            "char_offset": m.start(),
                        })
                if len(toc) >= 2:
                    return toc

        # 方案 D: mbp:pagebreak 或 hr 兜底分页
        pagebreaks = list(re.finditer(r"<(?:mbp:pagebreak|hr)[^>]*>", html_text, re.IGNORECASE))
        if pagebreaks:
            for i, pb in enumerate(pagebreaks):
                after_text = html_text[pb.end():pb.end() + 300]
                lines = [l.strip() for l in re.sub(r"<[^>]+>", "", after_text).splitlines() if l.strip()]
                ch_name = lines[0] if lines else f"Section {i + 1}"
                toc.append({
                    "index": i + 1,
                    "title": ch_name[:60],
                    "level": 1,
                    "char_offset": pb.start(),
                })
            return toc

        # 方案 E: 整本作为单章节兜底
        return [{
            "index": 1,
            "title": self.header_info.get("title") or "Full Book Content",
            "level": 1,
            "char_offset": 0,
        }]

    def get_probe_info(self) -> Dict[str, Any]:
        """获取元数据探针报告"""
        if self.is_kf8 and self.epub_book:
            info = self.epub_book.get_probe_info()
            info["format"] = "AZW3 / MOBI (KF8)"
            info["category"] = "M (Kindle KF8/AZW3)"
            info["recommendation"] = "KF8 Container -> Native HTML/Markdown (0.05s, 0 Token)"
            return info

        return {
            "file": os.path.basename(self.mobi_path),
            "path": self.mobi_path,
            "format": "MOBI (PalmDOC/Mobi6)",
            "is_digital": True,
            "title": self.header_info.get("title", ""),
            "total_chars": len(self.full_html) if self.full_html else 0,
            "has_toc": len(self.toc) > 0,
            "toc_count": len(self.toc),
            "category": "M (Reflowable MOBI)",
            "recommendation": "Pure Python PalmDOC LZ77 unpack -> HTML to Markdown (0.05s, 0 Token)",
            "toc_sample": [
                {"index": item["index"], "level": item["level"], "title": item["title"]}
                for item in self.toc[:10]
            ],
        }

    def find_chapter(self, query: str = None, index: int = None) -> Optional[Dict[str, Any]]:
        """按关键字或序号定位章节（支持前缀、数字、模糊匹配）"""
        if self.is_kf8 and self.epub_book:
            return self.epub_book.find_chapter(query, index)

        if index is not None:
            for it in self.toc:
                if it["index"] == index:
                    return it

        if query:
            q_lower = query.lower().strip()
            for it in self.toc:
                if it["title"].lower() == q_lower:
                    return it
            pattern = r'(?:^|[\s第])' + re.escape(q_lower) + r'(?:[\s.、:：章节]|$)'
            for it in self.toc:
                t_lower = it["title"].lower()
                if re.search(pattern, t_lower) or t_lower.startswith(q_lower):
                    return it
            q_norm = re.sub(r'[\s\-_.:：·]+', '', q_lower)
            for it in self.toc:
                t_norm = re.sub(r'[\s\-_.:：·]+', '', it["title"].lower())
                if q_norm in t_norm:
                    return it
            for it in self.toc:
                if q_lower in it["title"].lower():
                    return it
        return None

    def get_image_by_recindex(self, recindex: int) -> Optional[bytes]:
        """根据 recindex 编号从 PDB Records 中抽取图片原始二进制"""
        first_img_idx = self.header_info.get("first_image_index")
        if first_img_idx is None:
            return None
        target_idx = first_img_idx + recindex - 1
        if 0 <= target_idx < len(self.records):
            return self.records[target_idx]
        return None

    def extract_chapter_images(self, chapter_info: Dict[str, Any], output_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        """提取目标章节涉及的所有插图资源 (支持 Mobi6 与 KF8)"""
        if self.is_kf8 and self.epub_book:
            return self.epub_book.extract_chapter_images(chapter_info, output_dir)

        if not self.full_html:
            return []

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        idx = chapter_info["index"] - 1
        start_offset = chapter_info.get("char_offset", 0)
        end_offset = len(self.full_html)
        if idx + 1 < len(self.toc):
            end_offset = self.toc[idx + 1].get("char_offset", len(self.full_html))

        chapter_html = self.full_html[start_offset:end_offset]
        extracted = []

        if BeautifulSoup:
            soup = BeautifulSoup(chapter_html, "html.parser")
            imgs = soup.find_all("img")
        else:
            imgs = []
            for m in re.finditer(r'<img[^>]+recindex=[\"\']?(\d+)[\"\']?[^>]*>', chapter_html, re.I):
                imgs.append({"recindex": m.group(1), "alt": ""})

        for i, img_tag in enumerate(imgs):
            if hasattr(img_tag, "get"):
                rec_val = img_tag.get("recindex")
                alt_val = img_tag.get("alt", "")
            else:
                rec_val = img_tag.get("recindex")
                alt_val = img_tag.get("alt", "")

            if not rec_val:
                continue

            try:
                rec_idx = int(rec_val)
            except ValueError:
                continue

            img_bytes = self.get_image_by_recindex(rec_idx)
            if not img_bytes:
                continue

            saved_path = None
            if output_dir:
                ext = detect_image_ext(img_bytes)
                ch_num = chapter_info.get("index", 1)
                img_filename = f"ch{ch_num:03d}_img{i + 1:03d}{ext}"
                saved_path = os.path.join(output_dir, img_filename)
                with open(saved_path, "wb") as f:
                    f.write(img_bytes)

            extracted.append({
                "index": i + 1,
                "recindex": rec_idx,
                "saved_path": saved_path,
                "alt": alt_val,
                "bytes": img_bytes,
            })

        return extracted

    def extract_chapter_markdown(
        self,
        chapter_info: Dict[str, Any],
        dump_images_dir: Optional[str] = None,
        ocr: bool = False,
        ocr_engine: Optional[str] = None,
    ) -> str:
        """提取目标章节并转换为 Markdown (支持插图导出与 OCR 智能回填)"""
        if self.is_kf8 and self.epub_book:
            return self.epub_book.extract_chapter_markdown(
                chapter_info, dump_images_dir=dump_images_dir, ocr=ocr, ocr_engine=ocr_engine
            )

        if not self.full_html:
            return ""

        if dump_images_dir:
            os.makedirs(dump_images_dir, exist_ok=True)

        idx = chapter_info["index"] - 1
        current_level = chapter_info.get("level", 1)
        start_offset = chapter_info.get("char_offset", 0)
        end_offset = len(self.full_html)

        for next_item in self.toc[idx + 1:]:
            next_level = next_item.get("level", 1)
            if next_level <= current_level:
                end_offset = next_item.get("char_offset", len(self.full_html))
                break

        chapter_html = self.full_html[start_offset:end_offset]
        title = chapter_info.get("title", "")
        placeholders = {}
        processed_html = chapter_html

        if BeautifulSoup:
            soup = BeautifulSoup(chapter_html, "html.parser")
            purge_useless_navigation(soup)
            convert_soup_mathml_to_latex(soup)
            enhance_pre_code_tags(soup)

            imgs = soup.find_all("img")
            for i, img in enumerate(imgs):
                rec_val = img.get("recindex")
                alt_raw = img.get("alt", "").strip()
                if alt_raw:
                    clean_name = os.path.basename(alt_raw.replace("\\", "/"))
                    alt_val = clean_name if clean_name else f"插图 {i + 1}"
                else:
                    alt_val = f"插图 {i + 1}"
                if not rec_val:
                    continue

                try:
                    rec_idx = int(rec_val)
                except ValueError:
                    continue

                img_bytes = self.get_image_by_recindex(rec_idx)
                if not img_bytes:
                    continue

                saved_path = None
                if dump_images_dir:
                    ext = detect_image_ext(img_bytes)
                    ch_num = chapter_info.get("index", 1)
                    img_filename = f"ch{ch_num:03d}_img{i + 1:03d}{ext}"
                    saved_path = os.path.join(dump_images_dir, img_filename)
                    with open(saved_path, "wb") as f:
                        f.write(img_bytes)

                if ocr:
                    ocr_text = ocr_image(img_bytes, engine_name=ocr_engine)
                    ph_key = f"__MOBI_OCR_PLACEHOLDER_{i}__"
                    placeholders[ph_key] = format_ocr_markdown(ocr_text, alt_text=alt_val or f"插图 {i + 1}", image_rel_path=saved_path)
                    p_tag = soup.new_tag("p")
                    p_tag.string = ph_key
                    img.replace_with(p_tag)
                elif saved_path:
                    img["src"] = saved_path

            processed_html = str(soup)

        if html2text:
            h = html2text.HTML2Text()
            h.body_width = 0
            h.unicode_snob = True
            md = h.handle(processed_html).strip()
        elif markdownify:
            md = markdownify.markdownify(processed_html, heading_style="ATX").strip()
        elif BeautifulSoup:
            soup = BeautifulSoup(processed_html, "html.parser")
            md = soup.get_text(separator="\n\n").strip()
        else:
            md = re.sub(r"<[^>]+>", "", processed_html).strip()

        for ph_key, block in placeholders.items():
            md = md.replace(ph_key, block)

        if title and not md.startswith("# "):
            md = f"# {title}\n\n{md}"

        return md


def main():
    if len(sys.argv) < 2:
        print("Usage: python mobi_parser.py <path_to_mobi> [--probe] [--list]")
        sys.exit(1)

    mobi_path = sys.argv[1]
    with MobiBook(mobi_path) as book:
        if "--probe" in sys.argv:
            print(json.dumps(book.get_probe_info(), ensure_ascii=False, indent=2))
        elif "--list" in sys.argv:
            for item in book.toc:
                indent = "  " * (item.get("level", 1) - 1)
                print(f"[{item['index']:02d}] {indent}{item['title']}")
        else:
            print(f"MOBI Book: {book.header_info.get('title')}, TOC Chapters: {len(book.toc)}")


if __name__ == "__main__":
    main()
