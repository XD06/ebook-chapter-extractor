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

from epub_parser import EpubBook

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

from ocr_helper import detect_image_ext, ocr_image, format_ocr_markdown
from mathml_helper import convert_soup_mathml_to_latex


def purge_useless_navigation(soup: BeautifulSoup) -> None:
    """清理 DOM 中无意义的导航、页眉页脚与顶底跳转链接，防止稀释 Agent 上下文"""
    if not soup:
        return
    for tag in soup.find_all(["nav", "header", "footer"]):
        tag.decompose()

    for a in soup.find_all("a"):
        text = a.get_text().strip().lower()
        if text in ("[top]", "[next]", "[previous]", "[contents]", "[back to top]", "top", "contents"):
            a.decompose()
        elif not text and not a.find("img"):
            a.decompose()


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


def enhance_pre_code_tags(soup: BeautifulSoup) -> None:
    """
    预处理 HTML 中的 <pre> / <code> 标签，保留原生代码块格式与语言
    """
    for pre in soup.find_all("pre"):
        code_tag = pre.find("code")
        lang = ""
        classes = pre.get("class", [])
        if code_tag and code_tag.get("class"):
            classes += code_tag.get("class", [])
        for c in classes:
            if "cpp" in c or "c++" in c:
                lang = "cpp"
                break
            elif "python" in c or "py" in c:
                lang = "python"
                break
            elif "java" in c:
                lang = "java"
                break

        code_text = pre.get_text()
        pre.string = f"\n```{lang}\n{code_text.strip()}\n```\n"


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
            self.full_html = None
            self.toc = self.epub_book.toc

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
        """解析 PDB 数据库的记录偏移列表并提取各个 Record"""
        if len(self.raw_data) < 78:
            raise ValueError("Invalid MOBI file: file too short.")

        num_records = struct.unpack_from(">H", self.raw_data, 76)[0]
        offsets = []
        pos = 78
        for _ in range(num_records):
            if pos + 8 > len(self.raw_data):
                break
            offset = struct.unpack_from(">I", self.raw_data, pos)[0]
            offsets.append(offset)
            pos += 8

        offsets.append(len(self.raw_data))  # 哨兵结尾

        records = []
        for i in range(len(offsets) - 1):
            start = offsets[i]
            end = offsets[i + 1]
            if start <= len(self.raw_data) and end <= len(self.raw_data) and start <= end:
                records.append(self.raw_data[start:end])
            else:
                records.append(b"")
        return records

    def _parse_mobi_header(self) -> Dict[str, Any]:
        """解析 Record 0 获取 PalmDOC 和 MOBI 头部信息"""
        if not self.records:
            return {}

        rec0 = self.records[0]
        if len(rec0) < 16:
            return {}

        compression = struct.unpack_from(">H", rec0, 0)[0]
        text_length = struct.unpack_from(">I", rec0, 4)[0]
        text_records_count = struct.unpack_from(">H", rec0, 8)[0]
        record_size = struct.unpack_from(">H", rec0, 10)[0]

        title = ""
        encoding = "utf-8"
        has_kf8_boundary = False
        kf8_boundary_offset = 0
        first_image_index = None

        if len(rec0) >= 40 and rec0[16:20] == b"MOBI":
            header_len = struct.unpack_from(">I", rec0, 20)[0]
            codepage = struct.unpack_from(">I", rec0, 28)[0]
            if codepage == 1252:
                encoding = "cp1252"
            elif codepage == 65001:
                encoding = "utf-8"

            # 书名偏移与长度
            if len(rec0) >= 92:
                full_name_offset = struct.unpack_from(">I", rec0, 84)[0]
                full_name_length = struct.unpack_from(">I", rec0, 88)[0]
                if full_name_offset + full_name_length <= len(rec0):
                    try:
                        title = rec0[full_name_offset:full_name_offset + full_name_length].decode(encoding, errors="replace")
                    except Exception:
                        pass

            # 起始图片索引
            if len(rec0) >= 112:
                first_image_index = struct.unpack_from(">I", rec0, 108)[0]
                if first_image_index == 0xFFFFFFFF or first_image_index >= len(self.records):
                    first_image_index = None


            # 探测 KF8 Boundary 标记
            if len(rec0) >= 132:
                kf8_boundary_offset = struct.unpack_from(">I", rec0, 128)[0]
                if kf8_boundary_offset != 0xFFFFFFFF and kf8_boundary_offset < len(self.records):
                    has_kf8_boundary = True

        if not title:
            # 尝试从 PDB 头部名称获取
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
        # 寻找 BOUNDARY 或 RESC 标记的 KF8 数据
        kf8_start = -1
        if self.header_info.get("has_kf8_boundary"):
            kf8_start = self.header_info["kf8_boundary_offset"]

        if kf8_start < 0:
            for i, rec in enumerate(self.records):
                if rec.startswith(b"BOUNDARY") or (len(rec) >= 20 and rec[16:20] == b"KF8\x00"):
                    kf8_start = i
                    break

        # 如果没有明显的 KF8 边界，也可以尝试通过 `mobi` 模块作为 fallback
        if kf8_start >= 0 and kf8_start < len(self.records):
            try:
                # 尝试构建临时 epub 或解包
                import zipfile
                # 扫描后续记录中是否直接包含 ZIP/EPUB 文件头 (PK\x03\x04)
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

        # 尝试通过 mobi 库辅助解包 (如果有)
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
        # 记录 1 到 text_records_count 是文本数据
        for i in range(1, min(text_records_count + 1, len(self.records))):
            rec = self.records[i]
            if compression == 2:  # PalmDOC LZ77
                decompressed_chunks.append(decompress_palmdoc(rec))
            elif compression == 1:  # 无压缩
                decompressed_chunks.append(rec)
            else:
                # 其它未知压缩，尝试原样或 LZ77 解压
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

        if BeautifulSoup:
            soup = BeautifulSoup(html_text, "html.parser")
            # 策略 1: 扫描所有 h1 / h2 标签作为章节
            headings = soup.find_all(["h1", "h2", "h3"])
            if len(headings) >= 2:
                for i, h in enumerate(headings):
                    title = h.get_text().strip()
                    if title and len(title) < 100:
                        level = int(h.name[1])
                        # 查找锚点
                        anchor = h.get("id") or (h.find("a") and h.find("a").get("name")) or f"heading_{i}"
                        toc.append({
                            "index": i + 1,
                            "level": level,
                            "title": title,
                            "anchor": anchor,
                            "char_offset": html_text.find(str(h)),
                        })
                if toc:
                    return toc

        # 策略 2: 正则匹配常见章节模式（如 "第X章"、"Chapter X"、"<mbp:pagebreak"）
        pattern = re.compile(
            r"(<h[1-3][^>]*>(.*?)</h[1-3]>|"
            r"<mbp:pagebreak[^>]*>\s*<p[^>]*><b>(.*?)</b></p>|"
            r"<p[^>]*><b>(第[0-9一二三四五六七八九十百]+[章节回集卷部篇].*?)</b>|"
            r"<p[^>]*><b>(Chapter\s+\d+.*?)</b>)",
            re.IGNORECASE | re.DOTALL,
        )

        matches = list(pattern.finditer(html_text))
        for i, m in enumerate(matches):
            raw_title = m.group(2) or m.group(3) or m.group(4) or m.group(5) or ""
            clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
            if clean_title and len(clean_title) < 100:
                toc.append({
                    "index": i + 1,
                    "level": 1,
                    "title": clean_title,
                    "char_offset": m.start(),
                })

        # 兜底：如果依然没找到章节，分成 1 个整书章节
        if not toc:
            title = self.header_info.get("title") or "Full Content"
            toc.append({
                "index": 1,
                "level": 1,
                "title": title,
                "char_offset": 0,
            })

        for i, it in enumerate(toc):
            it["index"] = i + 1

        return toc

    def get_probe_info(self) -> Dict[str, Any]:
        """获取元数据探针报告"""
        if self.is_kf8 and self.epub_book:
            info = self.epub_book.get_probe_info()
            info["format"] = "MOBI / AZW3 (KF8)"
            info["file"] = os.path.basename(self.mobi_path)
            info["path"] = self.mobi_path
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
        """按关键词或序号定位章节"""
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
                "recindex": rec_idx,
                "bytes": img_bytes,
                "saved_path": saved_path,
                "alt": alt_val,
            })

        return extracted

    def extract_chapter_markdown(self, chapter_info: Dict[str, Any], dump_images_dir: Optional[str] = None, ocr: bool = False, ocr_engine: Optional[str] = None) -> str:
        """提取目标章节并转换为 Markdown (支持插图导出与 OCR 智能回填)"""
        if self.is_kf8 and self.epub_book:
            return self.epub_book.extract_chapter_markdown(chapter_info, dump_images_dir=dump_images_dir, ocr=ocr, ocr_engine=ocr_engine)

        if not self.full_html:
            return ""

        if dump_images_dir:
            os.makedirs(dump_images_dir, exist_ok=True)

        # 针对 Mobi6 HTML 按 char_offset 切分
        # 智能层级切片：默认提取当前章节及其所有子章节，直到遇到下一个同级或更高级章节
        idx = chapter_info["index"] - 1
        current_level = chapter_info.get("level", 1)
        start_offset = chapter_info.get("char_offset", 0)
        end_offset = len(self.full_html)

        for next_item in self.toc[idx + 1:]:
            next_level = next_item.get("level", 1)
            # 遇到同级或更高级节点，截断
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
                # 清洗 alt 属性中可能遗留的当年排版作者本地脏路径 (如 ..\..\新建文件夹\33-1.tif)
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

        # 还原 OCR 占位符
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
            print(f"Total chapters: {len(book.toc)}")
            for item in book.toc:
                indent = "  " * (item["level"] - 1)
                print(f"[{item['index']:03d}] {indent}{item['title']}")
        else:
            print(json.dumps(book.get_probe_info(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
