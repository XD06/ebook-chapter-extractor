#!/usr/bin/env python3
"""
mobi_parser.py - MOBI / AZW / AZW3 (KF8) 格式电子书解析与按需提取引擎
纯 Python 实现，零外部 C / 二进制依赖，支持 PalmDOC LZ77 解压缩、KF8 (AZW3) 识别与章节提取。
"""

import os
import sys
import re
import shutil
import struct
import tempfile
import json
from urllib.parse import unquote, urldefrag
from typing import List, Dict, Tuple, Optional, Any

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

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

from ocr_helper import detect_image_ext, ocr_image, ocr_images_batch, format_ocr_markdown, get_cache_dir
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


def _get_trailing_data_size(record: bytes, extra_data_flags: int) -> int:
    """计算 MOBI/KF8 文本记录尾部附加的 extra_data_flags 字节长度"""
    if not extra_data_flags or not record:
        return 0
    size = len(record)
    num = 0
    flags = extra_data_flags >> 1
    while flags:
        if flags & 1:
            pos = size - num - 1
            val = 0
            shift = 0
            while pos >= 0 and shift < 28:
                b = record[pos]
                pos -= 1
                val |= (b & 0x7F) << shift
                shift += 7
                if b & 0x80:
                    break
            num += val
        flags >>= 1
    if extra_data_flags & 1:
        pos = size - num - 1
        if pos >= 0:
            num += (record[pos] & 0x03) + 1
    return min(num, size)


class HuffCdicDecompressor:
    """Mobipocket HUFF / CDIC (compression=17480) 纯 Python 解压器"""

    def __init__(self, huff_rec: bytes, cdic_recs: List[bytes]):
        if not huff_rec.startswith(b"HUFF"):
            raise ValueError("Invalid HUFF record magic")
        off1, off2 = struct.unpack_from(">II", huff_rec, 8)

        def _unpack_dict1(v: int) -> Tuple[int, int, int]:
            codelen, term, maxcode = v & 0x1F, v & 0x80, v >> 8
            maxcode = ((maxcode + 1) << (32 - codelen)) - 1 if codelen > 0 else 0xFFFFFFFF
            return codelen, term, maxcode

        raw_d1 = struct.unpack_from(">256I", huff_rec, off1)
        self.dict1 = [_unpack_dict1(v) for v in raw_d1]

        dict2 = struct.unpack_from(">64I", huff_rec, off2)
        min_temp = [0]
        max_temp = [0xFFFFFFFF]
        for codelen, mc in enumerate(dict2[0::2], start=1):
            min_temp.append((mc << (32 - codelen)) & 0xFFFFFFFF)
        for codelen, mc in enumerate(dict2[1::2], start=1):
            max_temp.append((((mc + 1) << (32 - codelen)) - 1) & 0xFFFFFFFF)
        self.mincode = tuple(min_temp)
        self.maxcode = tuple(max_temp)

        self.dictionary: List[List[Any]] = []
        for cdic in cdic_recs:
            if not cdic.startswith(b"CDIC"):
                continue
            phrases, bits = struct.unpack_from(">II", cdic, 8)
            n = min(1 << bits, max(0, phrases - len(self.dictionary)))
            if n <= 0 or 16 + n * 2 > len(cdic):
                continue
            offsets = struct.unpack_from(f">{n}H", cdic, 16)
            for off in offsets:
                abs_off = 16 + off
                if abs_off + 2 > len(cdic):
                    self.dictionary.append([b"", 1])
                    continue
                blen = struct.unpack_from(">H", cdic, abs_off)[0]
                slice_len = blen & 0x7FFF
                flag = blen & 0x8000
                slice_data = cdic[abs_off + 2 : abs_off + 2 + slice_len]
                self.dictionary.append([slice_data, flag])

    def unpack(self, data: bytes, _depth: int = 0) -> bytes:
        if not data or _depth > 16:
            return b""
        bits_left = len(data) * 8
        padded = data + b"\x00" * 8
        bitpos = 0
        out = bytearray()

        while bits_left > 0:
            byte_idx = bitpos >> 3
            bit_shift = bitpos & 7
            val64 = struct.unpack_from(">Q", padded, byte_idx)[0]
            code = ((val64 << bit_shift) >> 32) & 0xFFFFFFFF

            codelen, term, maxcode = self.dict1[code >> 24]
            if not term:
                while codelen < 32 and code < self.mincode[codelen]:
                    codelen += 1
                maxcode = self.maxcode[codelen]

            if codelen <= 0 or codelen > bits_left:
                break

            bitpos += codelen
            bits_left -= codelen

            r = (maxcode - code) >> (32 - codelen)
            if r < 0 or r >= len(self.dictionary):
                break
            entry = self.dictionary[r]
            if not entry[1]:
                decoded = self.unpack(entry[0], _depth + 1)
                entry[0] = decoded
                entry[1] = 1
            out.extend(entry[0])

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
        self._external_tmpdir: Optional[str] = None
        self.is_kf8, self.is_kf8_native, self.epub_book, self.kf8_tmp_path = self._try_init_kf8()

        if self.header_info.get("encryption_type", 0) != 0:
            self.full_html = ""
            self.toc = []
        elif self.is_kf8 and self.epub_book:
            self.full_html = ""
            self.toc = self.epub_book.toc
        elif self.is_kf8 and self.is_kf8_native:
            self.full_html = self._extract_kf8_native_html()
            self.toc = self._build_toc_from_html(self.full_html)
        else:
            # 传统 Mobi6 HTML 处理
            self.full_html = self._extract_raw_html()
            self.toc = self._build_toc_from_html(self.full_html)

    def close(self):
        if self.epub_book:
            self.epub_book.close()
        if self.kf8_tmp_path and os.path.exists(self.kf8_tmp_path):
            try:
                os.remove(self.kf8_tmp_path)
            except Exception:
                pass
        if self._external_tmpdir and os.path.isdir(self._external_tmpdir):
            shutil.rmtree(self._external_tmpdir, ignore_errors=True)
            self._external_tmpdir = None

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

    def _parse_exth(self, rec0: bytes, exth_offset: int, encoding: str) -> Dict[str, Any]:
        """解析 MOBI EXTH 扩展元数据块 (作者、出版社、语言、完整标题、KF8 Boundary)"""
        ext: Dict[str, Any] = {}
        if exth_offset + 12 > len(rec0) or rec0[exth_offset : exth_offset + 4] != b"EXTH":
            return ext
        exth_count = struct.unpack(">I", rec0[exth_offset + 8 : exth_offset + 12])[0]
        pos = exth_offset + 12
        authors = []
        for _ in range(exth_count):
            if pos + 8 > len(rec0):
                break
            rtype, rlen = struct.unpack(">II", rec0[pos : pos + 8])
            if rlen < 8 or pos + rlen > len(rec0):
                break
            rdata = rec0[pos + 8 : pos + rlen]
            pos += rlen
            if rtype == 100:
                val = rdata.decode(encoding, errors="replace").strip("\x00 ")
                if val:
                    authors.append(val)
            elif rtype == 101:
                ext["publisher"] = rdata.decode(encoding, errors="replace").strip("\x00 ")
            elif rtype == 503:
                ext["updated_title"] = rdata.decode(encoding, errors="replace").strip("\x00 ")
            elif rtype == 524:
                ext["language"] = rdata.decode(encoding, errors="replace").strip("\x00 ")
            elif rtype == 121 and len(rdata) == 4:
                ext["kf8_boundary"] = struct.unpack(">I", rdata)[0]
        if authors:
            ext["author"] = ", ".join(authors)
        return ext

    def _parse_header_record(self, rec: bytes) -> Dict[str, Any]:
        """解析单个 PalmDOC + MOBI Header 记录（同时复用于 Record 0 与 KF8 二级头）"""
        if len(rec) < 16:
            return {}

        compression = struct.unpack(">H", rec[0:2])[0]
        text_length = struct.unpack(">I", rec[4:8])[0]
        text_records_count = struct.unpack(">H", rec[8:10])[0]
        record_size = struct.unpack(">H", rec[10:12])[0]
        raw_enc = struct.unpack(">H", rec[12:14])[0]
        is_std_mobi_hdr = len(rec) >= 24 and rec[16:20] == b"MOBI"
        encryption_type = raw_enc if (is_std_mobi_hdr and raw_enc in (1, 2)) else 0

        encoding = "utf-8"
        first_image_index = None
        huff_offset = None
        huff_count = 0
        extra_data_flags = 0
        file_version = 0
        header_len = 0
        has_kf8_boundary = False
        kf8_boundary_offset = -1
        fdst_offset = None
        exth_meta: Dict[str, Any] = {}

        if is_std_mobi_hdr:
            header_len = struct.unpack(">I", rec[20:24])[0]
            codepage = struct.unpack(">I", rec[28:32])[0] if len(rec) >= 32 else 1252
            file_version = struct.unpack(">I", rec[36:40])[0] if len(rec) >= 40 else 0

            if codepage == 65001:
                encoding = "utf-8"
            elif codepage == 1252:
                encoding = "windows-1252"
            elif codepage == 936:
                encoding = "gbk"

            if len(rec) >= 112:
                raw_img_idx = struct.unpack(">I", rec[108:112])[0]
                if 0 < raw_img_idx < len(self.records):
                    first_image_index = raw_img_idx

            if len(rec) >= 120:
                h_off, h_cnt = struct.unpack(">II", rec[112:120])
                if 0 < h_off < len(self.records) and h_cnt >= 2:
                    huff_offset, huff_count = h_off, h_cnt

            if len(rec) >= 132:
                exth_flags = struct.unpack(">I", rec[128:132])[0]
                if exth_flags & 0x40 and header_len > 0:
                    exth_meta = self._parse_exth(rec, 16 + header_len, encoding)

            if len(rec) >= 196:
                rec192 = struct.unpack(">I", rec[192:196])[0]
                if 0 < rec192 < len(self.records):
                    target_rec = self.records[rec192]
                    if file_version >= 8 and target_rec.startswith(b"FDST"):
                        fdst_offset = rec192
                    elif file_version < 8 and target_rec.startswith(b"BOUNDARY"):
                        has_kf8_boundary = True
                        kf8_boundary_offset = rec192

            if exth_meta.get("kf8_boundary") is not None:
                b_rec = exth_meta["kf8_boundary"]
                if 0 < b_rec < len(self.records):
                    has_kf8_boundary = True
                    kf8_boundary_offset = b_rec

            if header_len >= 228 and len(rec) >= 244:
                extra_data_flags = struct.unpack(">H", rec[242:244])[0]

        title = exth_meta.get("updated_title", "")
        if not title and len(rec) >= 92:
            full_name_offset = struct.unpack(">I", rec[84:88])[0]
            full_name_length = struct.unpack(">I", rec[88:92])[0]
            if full_name_offset < len(rec) and full_name_offset + full_name_length <= len(rec):
                raw_name = rec[full_name_offset : full_name_offset + full_name_length]
                try:
                    title = raw_name.decode(encoding, errors="replace").strip()
                except Exception:
                    title = raw_name.decode("utf-8", errors="replace").strip()

        if not title:
            raw_title = self.raw_data[:32].split(b"\x00")[0]
            title = raw_title.decode("latin1", errors="replace").strip()

        return {
            "title": title,
            "author": exth_meta.get("author", ""),
            "publisher": exth_meta.get("publisher", ""),
            "language": exth_meta.get("language", ""),
            "compression": compression,
            "encryption_type": encryption_type,
            "text_length": text_length,
            "text_records_count": text_records_count,
            "record_size": record_size,
            "encoding": encoding,
            "file_version": file_version,
            "extra_data_flags": extra_data_flags,
            "huff_offset": huff_offset,
            "huff_count": huff_count,
            "first_image_index": first_image_index,
            "fdst_offset": fdst_offset,
            "has_kf8_boundary": has_kf8_boundary,
            "kf8_boundary_offset": kf8_boundary_offset,
        }

    def _parse_mobi_header(self) -> Dict[str, Any]:
        """解析 Record 0 获取 PalmDOC & MOBI 头部核心元数据"""
        if not self.records:
            return {}
        return self._parse_header_record(self.records[0])

    def _try_init_kf8(self) -> Tuple[bool, bool, Optional[EpubBook], Optional[str]]:
        """检测并初始化 AZW3/KF8：优先原生解析 standalone/BOUNDARY+MOBI(v8)，次选嵌套 ZIP 或外部 mobi 库"""
        self.kf8_header_idx = 0
        self.kf8_header_info = self.header_info

        # 1. 纯 AZW3 (Standalone KF8: Record 0 本身即 file_version >= 8)
        if self.header_info.get("file_version", 0) >= 8 and self.header_info.get("text_records_count", 0) > 0:
            return True, True, None, None

        # 2. 复合体 BOUNDARY
        kf8_start = -1
        if self.header_info.get("has_kf8_boundary"):
            kf8_start = self.header_info["kf8_boundary_offset"]

        if kf8_start < 0:
            for i, rec in enumerate(self.records):
                if rec.startswith(b"BOUNDARY") or (len(rec) >= 20 and rec[16:20] == b"KF8\x00"):
                    kf8_start = i
                    break

        if 0 < kf8_start < len(self.records):
            # 2a. BOUNDARY 后紧跟原生 KF8 MOBI 头记录
            kf8_hdr_idx = kf8_start + 1 if self.records[kf8_start].startswith(b"BOUNDARY") else kf8_start
            if kf8_hdr_idx < len(self.records):
                cand_rec = self.records[kf8_hdr_idx]
                if len(cand_rec) >= 40 and cand_rec[16:20] == b"MOBI":
                    kf8_info = self._parse_header_record(cand_rec)
                    if kf8_info.get("file_version", 0) >= 8 and kf8_info.get("text_records_count", 0) > 0:
                        self.kf8_header_idx = kf8_hdr_idx
                        self.kf8_header_info = kf8_info
                        # 补充主头缺失的 EXTH 元数据
                        for key in ("title", "author", "publisher", "language"):
                            if not kf8_info.get(key) and self.header_info.get(key):
                                kf8_info[key] = self.header_info[key]
                        return True, True, None, None

            # 2b. 嵌套 EPUB ZIP 容器兜底
            try:
                for rec in self.records[kf8_start:]:
                    if rec.startswith(b"PK\x03\x04"):
                        tmp = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
                        tmp.write(rec)
                        tmp.close()
                        try:
                            epub = EpubBook(tmp.name)
                            return True, False, epub, tmp.name
                        except Exception:
                            if os.path.exists(tmp.name):
                                os.remove(tmp.name)
            except Exception:
                pass

        # 3. 第三方 mobi 库兜底（记录 tmpdir 以便 close() 时彻底清理）
        try:
            import mobi
            tmpdir, filepath = mobi.extract(self.mobi_path)
            self._external_tmpdir = tmpdir
            if filepath.lower().endswith(".epub") and os.path.exists(filepath):
                epub = EpubBook(filepath)
                return True, False, epub, None
        except Exception:
            pass

        return False, False, None, None

    def _decompress_records(self, start_rec: int, count: int, header: Dict[str, Any]) -> bytes:
        """统一解压 Mobi6 或原生 KF8 文本记录流（自动剥离 extra_data_flags 与支持 HUFF/CDIC）"""
        compression = header.get("compression", 1)
        extra_flags = header.get("extra_data_flags", 0)

        huff_decoder = None
        if compression == 17480:
            huff_off = header.get("huff_offset")
            huff_cnt = header.get("huff_count", 0)
            if huff_off is not None and huff_cnt >= 2 and huff_off + huff_cnt <= len(self.records):
                huff_rec = self.records[huff_off]
                cdic_recs = self.records[huff_off + 1 : huff_off + huff_cnt]
                huff_decoder = HuffCdicDecompressor(huff_rec, cdic_recs)
            else:
                raise RuntimeError("MOBI uses HUFF/CDIC compression (17480) but valid HUFF/CDIC records are missing.")

        decompressed_chunks = []
        end_rec = min(start_rec + count, len(self.records))
        for i in range(start_rec, end_rec):
            rec = self.records[i]
            if extra_flags:
                trail = _get_trailing_data_size(rec, extra_flags)
                if 0 < trail < len(rec):
                    rec = rec[:-trail]

            if compression == 17480 and huff_decoder is not None:
                decompressed_chunks.append(huff_decoder.unpack(rec))
            elif compression == 2:
                decompressed_chunks.append(decompress_palmdoc(rec))
            elif compression == 1:
                decompressed_chunks.append(rec)
            else:
                try:
                    decompressed_chunks.append(decompress_palmdoc(rec))
                except Exception:
                    decompressed_chunks.append(rec)

        return b"".join(decompressed_chunks)

    def _extract_kf8_native_html(self) -> str:
        """提取原生 AZW3/KF8 主 HTML 流（结合 FDST 表仅截取主文档流，剔除内嵌 CSS 等辅助流）"""
        hdr = self.kf8_header_info
        start_rec = self.kf8_header_idx + 1
        count = hdr.get("text_records_count", 0)
        encoding = hdr.get("encoding", "utf-8")
        full_bytes = self._decompress_records(start_rec, count, hdr)

        fdst_off = hdr.get("fdst_offset")
        if fdst_off is not None and 0 <= fdst_off < len(self.records):
            fdst_rec = self.records[fdst_off]
            if len(fdst_rec) >= 20 and fdst_rec.startswith(b"FDST"):
                flow_off = struct.unpack_from(">I", fdst_rec, 4)[0]
                num_flows = struct.unpack_from(">I", fdst_rec, 8)[0]
                if num_flows >= 1 and flow_off + 8 <= len(fdst_rec):
                    f_start, f_end = struct.unpack_from(">II", fdst_rec, flow_off)
                    if 0 <= f_start < f_end <= len(full_bytes):
                        full_bytes = full_bytes[f_start:f_end]

        try:
            return full_bytes.decode(encoding, errors="replace")
        except Exception:
            return full_bytes.decode("utf-8", errors="replace")

    def _extract_raw_html(self) -> str:
        """解压 Record 1..N 获取 Mobi6 原始 HTML 文本"""
        count = self.header_info.get("text_records_count", 0)
        encoding = self.header_info.get("encoding", "utf-8")
        full_bytes = self._decompress_records(1, count, self.header_info)
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
        is_drm = self.header_info.get("encryption_type", 0) != 0
        if self.is_kf8 and self.epub_book:
            info = self.epub_book.get_probe_info()
            info["format"] = "AZW3 / MOBI (KF8)"
            info["category"] = "M (Kindle KF8/AZW3)"
            info["recommendation"] = "KF8 Container -> Native HTML/Markdown (0.05s, 0 Token)"
            return info

        hdr = self.kf8_header_info if (self.is_kf8 and self.is_kf8_native) else self.header_info
        return {
            "file": os.path.basename(self.mobi_path),
            "path": self.mobi_path,
            "format": "AZW3 / MOBI (KF8)" if self.is_kf8 else "MOBI (PalmDOC/Mobi6)",
            "is_digital": not is_drm,
            "is_drm": is_drm,
            "title": hdr.get("title", ""),
            "author": hdr.get("author", ""),
            "publisher": hdr.get("publisher", ""),
            "language": hdr.get("language", ""),
            "compression": hdr.get("compression", 1),
            "total_chars": len(self.full_html) if self.full_html else 0,
            "has_toc": len(self.toc) > 0,
            "toc_count": len(self.toc),
            "category": "DRM-Protected MOBI/AZW3" if is_drm else ("M (Kindle KF8/AZW3)" if self.is_kf8 else "M (Reflowable MOBI)"),
            "recommendation": (
                f"DRM detected (encryption_type={self.header_info.get('encryption_type')}); remove DRM before extraction."
                if is_drm
                else "Pure Python PalmDOC/HUFF unpack -> HTML to Markdown (0.05s, 0 Token)"
            ),
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
        hdr = getattr(self, "kf8_header_info", None) if (self.is_kf8 and getattr(self, "is_kf8_native", False)) else self.header_info
        first_img_idx = hdr.get("first_image_index") if hdr else None
        if first_img_idx is None:
            first_img_idx = self.header_info.get("first_image_index")
        if first_img_idx is None:
            return None
        target_idx = first_img_idx + recindex - 1
        if 0 <= target_idx < len(self.records):
            return self.records[target_idx]
        return None

    def _get_chapter_raw_html(self, chapter_info: Dict[str, Any]) -> str:
        """按 next_level <= current_level 精确切分章节 HTML 区间（统一图片抽取与 Markdown 导出边界）"""
        if self.header_info.get("encryption_type", 0) != 0:
            raise RuntimeError(
                f"MOBI/AZW3 file '{os.path.basename(self.mobi_path)}' is DRM encrypted "
                f"(encryption_type={self.header_info.get('encryption_type')}) and cannot be extracted."
            )
        if not self.full_html:
            return ""
        idx = chapter_info["index"] - 1
        current_level = chapter_info.get("level", 1)
        start_offset = chapter_info.get("char_offset", 0)
        end_offset = len(self.full_html)
        for next_item in self.toc[idx + 1:]:
            if next_item.get("level", 1) <= current_level:
                end_offset = next_item.get("char_offset", len(self.full_html))
                break
        return self.full_html[start_offset:end_offset]

    def _resolve_img_recindex(self, img_tag) -> Optional[int]:
        """从 <img> 提取 recindex 或 KF8 kindle:embed:XXXX 索引"""
        rec_val = img_tag.get("recindex") if hasattr(img_tag, "get") else None
        if rec_val:
            try:
                return int(rec_val)
            except ValueError:
                pass
        src = img_tag.get("src", "") if hasattr(img_tag, "get") else ""
        m = re.search(r"kindle:embed:([0-9A-Za-z]+)", str(src))
        if m:
            try:
                return int(m.group(1), 32)
            except ValueError:
                pass
        return None

    def extract_chapter_images(self, chapter_info: Dict[str, Any], output_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        """提取目标章节涉及的所有插图资源 (支持 Mobi6 与 KF8)"""
        if self.is_kf8 and self.epub_book:
            return self.epub_book.extract_chapter_images(chapter_info, output_dir)

        chapter_html = self._get_chapter_raw_html(chapter_info)
        if not chapter_html:
            return []

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        extracted = []
        if BeautifulSoup:
            soup = BeautifulSoup(chapter_html, "html.parser")
            imgs = soup.find_all("img")
        else:
            imgs = []
            for m in re.finditer(r'<img[^>]+recindex=[\"\']?(\d+)[\"\']?[^>]*>', chapter_html, re.I):
                imgs.append({"recindex": m.group(1), "alt": "", "src": ""})

        for i, img_tag in enumerate(imgs):
            alt_val = img_tag.get("alt", "") if hasattr(img_tag, "get") else ""
            rec_idx = self._resolve_img_recindex(img_tag)
            if rec_idx is None:
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
        """提取目标章节并转换为 Markdown (支持围栏代码保真、插图导出与 OCR 智能回填)"""
        if self.is_kf8 and self.epub_book:
            return self.epub_book.extract_chapter_markdown(
                chapter_info, dump_images_dir=dump_images_dir, ocr=ocr, ocr_engine=ocr_engine
            )

        chapter_html = self._get_chapter_raw_html(chapter_info)
        if not chapter_html:
            return ""

        if dump_images_dir:
            os.makedirs(dump_images_dir, exist_ok=True)

        title = chapter_info.get("title", "")
        placeholders: Dict[str, str] = {}
        code_placeholders: Dict[str, str] = {}
        processed_html = chapter_html

        if BeautifulSoup:
            soup = BeautifulSoup(chapter_html, "html.parser")
            purge_useless_navigation(soup)
            convert_soup_mathml_to_latex(soup)
            enhance_pre_code_tags(soup, code_placeholders=code_placeholders)

            imgs = soup.find_all("img")
            pending_ocr_tasks = []

            for i, img in enumerate(imgs):
                alt_raw = img.get("alt", "").strip()
                if alt_raw:
                    clean_name = os.path.basename(alt_raw.replace("\\", "/"))
                    alt_val = clean_name if clean_name else f"插图 {i + 1}"
                else:
                    alt_val = f"插图 {i + 1}"

                rec_idx = self._resolve_img_recindex(img)
                if rec_idx is None:
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
                    ph_key = f"MOBIOCRPLACEHOLDERTOKEN{i}END"
                    pending_ocr_tasks.append((ph_key, alt_val or f"插图 {i + 1}", saved_path, img_bytes))
                    p_tag = soup.new_tag("p")
                    p_tag.string = ph_key
                    img.replace_with(p_tag)
                elif saved_path:
                    img["src"] = saved_path

            if pending_ocr_tasks:
                images_to_ocr = [task[3] for task in pending_ocr_tasks]
                ocr_texts = ocr_images_batch(images_to_ocr, engine_name=ocr_engine)
                for (ph_key, alt_val, saved_path, _), ocr_text in zip(pending_ocr_tasks, ocr_texts):
                    placeholders[ph_key] = format_ocr_markdown(ocr_text, alt_text=alt_val, image_rel_path=saved_path)

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

        for ph_key, block in code_placeholders.items():
            md = md.replace(ph_key, f"\n{block.strip()}\n")

        for ph_key, block in placeholders.items():
            md = md.replace(ph_key, block)

        md = re.sub(r"\n{3,}", "\n\n", md).strip()

        if title and not re.match(r"^\s*#\s+", md):
            md = f"# {title}\n\n{md}"

        return md


def main():
    if len(sys.argv) < 2:
        print("Usage: python mobi_parser.py <path_to_mobi> [--probe] [--list]")
        sys.exit(1)

    mobi_path = sys.argv[1]
    with MobiBook(mobi_path) as book:
        if "--probe" in sys.argv:
            print(json.dumps(book.get_probe_info(), ensure_ascii=True, indent=2))
        elif "--list" in sys.argv:
            for item in book.toc:
                indent = "  " * (item.get("level", 1) - 1)
                print(f"[{item['index']:02d}] {indent}{item['title']}")
        else:
            print(f"MOBI Book: {book.header_info.get('title')}, TOC Chapters: {len(book.toc)}")


if __name__ == "__main__":
    main()
