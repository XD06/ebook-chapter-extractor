#!/usr/bin/env python3
"""
pdf_helper.py - PDF 通用格式嗅探、书签自愈、目录构建与高速高精度 Markdown 提取引擎
为 probe_pdf / build_index / extract_chapter / render_page 提供共享核心能力，消除重复逻辑。
"""

import contextlib
import io
import os
import re
import json
import sys
from typing import List, Dict, Tuple, Optional, Any

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None

from ocr_helper import get_cache_dir, detect_ocr_engine, ocr_image_rapidocr


INDEX_SCHEMA_VERSION = 2


# ==============================================================================
# 1. 通用电子书魔数与格式嗅探
# ==============================================================================

def detect_ebook_format(file_path: str) -> str:
    """结合文件头魔数与扩展名精准识别电子书格式 ('pdf' / 'epub' / 'mobi')"""
    ext = os.path.splitext(file_path)[1].lower()
    try:
        with open(file_path, "rb") as f:
            head = f.read(128)
        if head.startswith(b"%PDF-") or b"%PDF-" in head[:64]:
            return "pdf"
        if len(head) >= 68 and head[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
            return "mobi"
        if head.startswith((b"PK\x03\x04", b"PK\x05\x06")) and ext in (".epub", ".zip", ""):
            return "epub"
    except OSError:
        pass

    if ext == ".epub":
        return "epub"
    if ext in (".mobi", ".azw", ".azw3", ".prc"):
        return "mobi"
    return "pdf"


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清理 Windows / Linux 跨平台非法文件名字符"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "slice").strip())
    cleaned = cleaned.strip(" ._")
    return (cleaned or "slice")[:max_len]


# ==============================================================================
# 2. PDF 快速深度探针
# ==============================================================================

def _page_has_large_image(page) -> bool:
    """判断页面是否包含大面积扫描图像"""
    try:
        page_area = max(1.0, page.rect.get_area())
        for info in page.get_image_info():
            bbox = info.get("bbox")
            if bbox:
                w = max(0.0, bbox[2] - bbox[0])
                h = max(0.0, bbox[3] - bbox[1])
                if (w * h) / page_area >= 0.35:
                    return True
        # 兜底检查：若 get_image_info 为空但 get_images 非空且文本极少
        return bool(page.get_images())
    except Exception:
        return bool(page.get_images())


def is_page_scanned(page) -> bool:
    """单页级别判断是否为扫描图像页（文本极少且含图像）"""
    text = page.get_text("text").strip()
    if len(text) >= 40:
        return False
    # 存在少量页码字符(1~4位)但含整页扫描图时仍判为扫描页
    if len(text) < 30 and _page_has_large_image(page):
        return True
    return len(text) < 15 and bool(page.get_images())


def probe_pdf_document(pdf_path: str) -> dict:
    """全书均匀采样探测 PDF 特征（避免仅采样前几页导致混合文档误判）"""
    if pymupdf is None:
        return {"error": "PyMuPDF not installed. Run: pip install pymupdf"}

    if not os.path.exists(pdf_path):
        return {"error": f"File not found: {pdf_path}"}

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as e:
        return {"error": f"Failed to open PDF: {str(e)}"}

    try:
        if doc.needs_pass:
            return {
                "file": os.path.basename(pdf_path),
                "path": os.path.abspath(pdf_path),
                "format": "PDF",
                "total_pages": doc.page_count,
                "is_encrypted": True,
                "error": "PDF is password-protected (requires password to read).",
            }

        total_pages = doc.page_count
        meta = doc.metadata or {}
        toc = doc.get_toc()

        # 均匀采样：前 3 页 + 正文等分采样点（最多 9 页）
        raw_indices = [0, 1, 2]
        if total_pages > 4:
            for frac in (0.15, 0.35, 0.55, 0.75, 0.9):
                raw_indices.append(int((total_pages - 1) * frac))
        if total_pages > 10:
            raw_indices.append(9)
        sample_indices = sorted(set(i for i in raw_indices if 0 <= i < total_pages))

        char_counts = []
        scanned_flags = []
        for i in sample_indices:
            page = doc[i]
            txt = page.get_text("text").strip()
            char_counts.append(len(txt))
            scanned_flags.append(len(txt) < 30 and _page_has_large_image(page))

        # 若正文较长，排除前 2 页纯封面干扰来判定主体是否数字版
        eval_counts = char_counts[2:] if len(char_counts) >= 5 else char_counts
        eval_scanned = scanned_flags[2:] if len(scanned_flags) >= 5 else scanned_flags
        digital_cnt = sum(1 for c, sc in zip(eval_counts, eval_scanned) if c >= 35 and not sc)
        is_digital = digital_cnt >= max(1, (len(eval_counts) + 1) // 2)
        is_mixed = is_digital and any(scanned_flags)

        has_toc = len(toc) > 0
        has_text_toc = False
        if is_digital:
            for i in range(min(20, total_pages)):
                t = doc[i].get_text("text")
                if _is_toc_like_text(t):
                    has_text_toc = True
                    break

        paper_headings = []
        if is_digital and not has_toc:
            try:
                from paper_parser import detect_headings
                paper_headings = detect_headings(pdf_path)
            except Exception:
                paper_headings = []

        if is_digital:
            try:
                from paper_parser import classify_document
                doc_class = classify_document(doc)
            except Exception:
                doc_class = {"doc_type": "unknown", "is_two_column": False, "has_abstract": False}
        else:
            doc_class = {"doc_type": "unknown", "is_two_column": False, "has_abstract": False}

        if is_digital and has_toc:
            category = "A (Digital + Has TOC)"
            recommendation = "Fast PyMuPDF layout extraction on physical pages (No offset needed)"
        elif is_digital and has_text_toc:
            category = "B1 (Digital + Printed TOC, No Bookmarks)"
            recommendation = (
                "build_index.py first: extract printed TOC -> compute Offset -> Fast PyMuPDF; "
                f"layout Paper Mode ({len(paper_headings)} headings) as fallback via --force-layout"
            )
        elif is_digital and not has_toc:
            category = "B2 (Digital + No TOC - Paper Mode)"
            col_hint = " Two-column detected: prefer --column-aware / visual read." if doc_class["is_two_column"] else ""
            recommendation = (
                f"Paper Mode: {len(paper_headings)} virtual headings from layout analysis. "
                f"Use --chapter <heading> or --range <start-end>.{col_hint}"
            )
        elif not is_digital and has_toc:
            category = "C (Scanned + Has TOC)"
            recommendation = "Dual-channel render + OCR or slice target pages -> MinerU VLM"
        else:
            category = "D (Scanned + No TOC)"
            recommendation = "OCR front TOC pages (build_index) -> Dual-channel render / MinerU VLM"

        toc_sample = []
        source_list = toc if has_toc else paper_headings
        for item in source_list[:10]:
            toc_sample.append({
                "level": item[0] if has_toc else 1,
                "title": item[1] if has_toc else item["title"],
                "page": item[2] if has_toc else item["phys"],
            })

        return {
            "file": os.path.basename(pdf_path),
            "path": os.path.abspath(pdf_path),
            "format": "PDF",
            "total_pages": total_pages,
            "is_digital": is_digital,
            "is_mixed": is_mixed,
            "is_encrypted": False,
            "has_toc": has_toc,
            "has_text_toc": has_text_toc,
            "toc_count": len(toc),
            "category": category,
            "recommendation": recommendation,
            "doc_type": doc_class["doc_type"],
            "is_two_column": doc_class["is_two_column"],
            "virtual_heading_count": len(paper_headings),
            "paper_mode": bool(is_digital and not has_toc and not has_text_toc and paper_headings),
            "toc_sample": toc_sample,
            "sample_char_counts": char_counts,
            "title": meta.get("title", ""),
            "author": meta.get("author", ""),
        }
    finally:
        doc.close()


# ==============================================================================
# 3. 书签自愈、无书签目录解析与稀疏书签兜底
# ==============================================================================

def _norm_match_text(s: str) -> str:
    return re.sub(r"[\s\-_.:：·，。！？、()（）「」『』\[\]]+", "", (s or "").lower())


def _search_title_in_pages(doc, title: str, low: int, high: int) -> Optional[int]:
    """在 [low, high] 物理页窗口内检索章节标题，返回最先匹配的 1-based 物理页码"""
    if doc is None or (high - low) > 25:
        return None
    q_norm = _norm_match_text(title)
    if len(q_norm) < 3:
        return None

    for p in range(max(1, low), min(doc.page_count, high) + 1):
        try:
            page_txt = doc[p - 1].get_text("text")
            if not page_txt.strip():
                continue
            # 优先匹配独立短行，再回退全页归一化子串
            for line in page_txt.splitlines():
                l_norm = _norm_match_text(line)
                if l_norm and (q_norm == l_norm or (len(l_norm) <= len(q_norm) + 8 and q_norm in l_norm)):
                    return p
            if q_norm in _norm_match_text(page_txt):
                return p
        except Exception:
            continue
    return None


def heal_toc_entries(toc: list, total_pages: int, doc=None) -> list:
    """
    自愈/清洗 PDF 书签列表：
    1. 识别 <= 0、越界以及非单调回跳至 page==1 的断链书签；
    2. 在合法单调区间 [low, high] 内优先通过正文标题检索精准定页，否则子级提升或合理插值；
    3. 修复扉页处误标为 L1 的同页子项（如译者姓名被标为 L1 导致吞并全书其余 L2 章节）。
    """
    if not toc:
        return []

    cleaned = []
    for item in toc:
        level = int(item[0]) if len(item) > 0 else 1
        title = str(item[1]).strip() if len(item) > 1 else ""
        page = int(item[2]) if len(item) > 2 else 0
        cleaned.append({"level": level, "title": title, "phys": page})

    # 1. 识别异常项：<=0、>total_pages，或已翻过第2页后突然回跳到 page==1 的断链书签
    broken = [False] * len(cleaned)
    max_seen = 0
    has_later_gt2 = [False] * len(cleaned)
    seen_gt2 = False
    for i in range(len(cleaned) - 1, -1, -1):
        has_later_gt2[i] = seen_gt2
        if 2 < cleaned[i]["phys"] <= total_pages:
            seen_gt2 = True

    for i, it in enumerate(cleaned):
        p = it["phys"]
        if p <= 0 or p > total_pages:
            broken[i] = True
        elif i > 0 and p == 1 and max_seen > 2 and (has_later_gt2[i] or i == len(cleaned) - 1):
            broken[i] = True
        else:
            max_seen = max(max_seen, p)

    # 2. 按前后有效书签构建单调区间 [low, high] 并逐一修复
    last_valid = 1
    for i, it in enumerate(cleaned):
        if not broken[i]:
            last_valid = it["phys"]
            continue

        low = last_valid
        high = total_pages
        for j in range(i + 1, len(cleaned)):
            if not broken[j]:
                high = cleaned[j]["phys"]
                break
        if low > high:
            low = high

        # 2a. 若为数字 PDF，尝试在 [low, high] 内精确匹配标题
        matched_p = _search_title_in_pages(doc, it["title"], low, high)
        if matched_p is not None:
            it["phys"] = matched_p
            broken[i] = False
            last_valid = matched_p
            continue

        # 2b. 检查直接有效子级（若紧邻子级即有效，或前有断链子级）
        cur_lv = it["level"]
        first_valid_child = None
        has_broken_child_before = False
        for j in range(i + 1, len(cleaned)):
            if cleaned[j]["level"] <= cur_lv:
                break
            if not broken[j] and low <= cleaned[j]["phys"] <= high:
                first_valid_child = cleaned[j]["phys"]
                break
            if broken[j]:
                has_broken_child_before = True

        if first_valid_child is not None:
            if has_broken_child_before and (first_valid_child - low) >= 2:
                healed_p = min(first_valid_child, low + 1)
            else:
                healed_p = first_valid_child
        elif i + 1 < len(cleaned) and not broken[i + 1] and low <= cleaned[i + 1]["phys"] <= high:
            # 叶子断链项：若前后页有空隙，优先推进 1 页而非直接并入下一章
            if (high - low) >= 2:
                healed_p = low + 1
            else:
                healed_p = cleaned[i + 1]["phys"]
        else:
            healed_p = min(high, low + 1) if (high - low) >= 2 else high

        it["phys"] = max(1, min(total_pages, healed_p))
        broken[i] = False
        last_valid = it["phys"]

    # 3. 修正前置信息页的同页层级倒挂 (如 L2 译者简介 -> 同页 L1 译者名 -> 后续全书 L2 章节且再无 L1)
    has_l1_after_front = any(it["level"] == 1 and it["phys"] > 5 for it in cleaned)
    if not has_l1_after_front:
        for i in range(1, len(cleaned)):
            prev_it, cur_it = cleaned[i - 1], cleaned[i]
            if (
                cur_it["level"] == 1
                and cur_it["phys"] <= 5
                and prev_it["level"] >= 2
                and prev_it["phys"] == cur_it["phys"]
            ):
                cur_it["level"] = prev_it["level"] + 1
        # 若正文(phys > 5) 全为 L2+ 而前置页(phys <= 5) 残留 L1 书签，统一降级为正文基准层级以防吞并全书
        body_levels = [it["level"] for it in cleaned if it["phys"] > 5]
        if body_levels:
            base_body_lv = min(body_levels)
            if base_body_lv >= 2:
                for it in cleaned:
                    if it["level"] == 1 and it["phys"] <= 5:
                        it["level"] = base_body_lv

    return [it for it in cleaned if 1 <= it["phys"] <= total_pages]


def build_index_from_toc_entries(healed: list, total_pages: int) -> list:
    """将清洗后的书签列表转换为带 [start_phys, end_phys] 范围的索引"""
    if not healed:
        return []
    index = []
    for i, item in enumerate(healed):
        level, title, start_page = item["level"], item["title"], item["phys"]
        end_page = total_pages
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
            "page_count": end_page - start_page + 1,
        })
    return index


def _is_toc_like_text(text: str) -> bool:
    if not text:
        return False
    upper = text.upper()
    if "目录" in text or "目 录" in text or "CONTENTS" in upper or "TABLE OF CONTENTS" in upper:
        return True
    # 若超过 3 行以页码结尾，也视为目录延续页
    hits = 0
    for line in text.splitlines():
        if re.match(r"^.{2,60}?[.．·\s]{2,}\d{1,4}\s*$", line.strip()):
            hits += 1
    return hits >= 3


def _parse_toc_lines(lines: List[str], total_pages: int) -> List[Tuple[str, int]]:
    """从目录页行文本提取 (标题, 逻辑页码)，兼容省略号、空格分隔及跨行页码"""
    extracted = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        i += 1
        if not line or line.isdigit():
            continue
        if any(k in line for k in ("目录", "目 录")) and len(line) <= 8:
            continue
        if line.upper() in ("CONTENTS", "TABLE OF CONTENTS"):
            continue

        # 模式1：标题 + 分隔符(点/2+空格/制表符) + 页码
        m = re.match(r"^(.+?)(?:[.．·\t]+|\s{2,})(\d{1,4})\s*$", line)
        if not m:
            # 模式2：单空格分隔（需确保标题不以年份/数字单位误判）
            m = re.match(r"^(.+?[^\d])\s+(\d{1,4})\s*$", line)
        if m:
            t, p = m.group(1).strip(" .．·\t"), int(m.group(2))
            if len(t) >= 2 and not t.isdigit() and 1 <= p <= total_pages + 30:
                extracted.append((t, p))
            continue

        # 模式3：标题独占一行，页码紧跟下一行（OCR 偶发分行）
        if i < n and lines[i].strip().isdigit():
            p = int(lines[i].strip())
            if len(line) >= 2 and 1 <= p <= total_pages + 30:
                extracted.append((line, p))
                i += 1
                continue

    return extracted


def _calibrate_offset(doc, extracted: List[Tuple[str, int]], toc_last_idx: int, is_scanned_doc: bool) -> int:
    """结合 get_label() 与正文标题实测落点精准校准 Offset"""
    total = doc.page_count

    # 策略 1：PDF 内置逻辑页标签
    for i in range(min(50, total)):
        if str(doc[i].get_label()).strip() == "1":
            return i  # phys_1based = i + 1, offset = phys - 1 = i

    if not extracted:
        return 0

    # 取首个逻辑页码合理的目录条目做实测定位
    sample_entries = [e for e in extracted[:4] if 1 <= e[1] <= total]
    if not sample_entries:
        return 0

    first_title, first_log = sample_entries[0]
    q_norm = _norm_match_text(first_title)[:10]

    # 策略 2：数字 PDF 直接在前 40 页搜索首章标题真实物理页
    if not is_scanned_doc and len(q_norm) >= 3:
        for pno in range(toc_last_idx + 1, min(total, toc_last_idx + 40)):
            if q_norm in _norm_match_text(doc[pno].get_text("text")):
                return max(0, (pno + 1) - first_log)

    # 策略 3：扫描版 PDF —— 优先验证 Offset=0（页码即物理页）以及常见偏移量 (toc_last_idx+1)
    if is_scanned_doc and len(q_norm) >= 3 and detect_ocr_engine() != "none":
        candidate_offsets = [0, max(0, (toc_last_idx + 2) - first_log)]
        checked = set()
        for cand_off in candidate_offsets:
            if cand_off in checked:
                continue
            checked.add(cand_off)
            cand_pno = first_log + cand_off - 1
            if toc_last_idx < cand_pno < total:
                pix = doc[cand_pno].get_pixmap(dpi=100)
                ocr_txt = ocr_image_rapidocr(pix.tobytes("png"))
                if q_norm in _norm_match_text(ocr_txt):
                    return cand_off

    # 兜底估算
    est_phys = (toc_last_idx + 1) + 1
    return max(0, est_phys - first_log)


def build_index_from_text_toc(doc, max_scan_pages: int = 16, allow_ocr: bool = True) -> Tuple[list, str]:
    """从前置印刷目录页构建索引（支持数字版快速抽取与扫描版早停 OCR 抽取）"""
    total = doc.page_count
    scan_limit = min(max_scan_pages, total)

    # 检测前几页是否有数字文字层
    digital_front = any(len(doc[i].get_text("text").strip()) >= 30 for i in range(min(6, total)))
    toc_pages_lines: List[Tuple[int, List[str]]] = []

    if digital_front:
        for i in range(scan_limit):
            txt = doc[i].get_text("text")
            if _is_toc_like_text(txt):
                toc_pages_lines.append((i, txt.splitlines()))
            elif toc_pages_lines:
                break
        is_scanned_doc = False
    else:
        is_scanned_doc = True
        if not allow_ocr or detect_ocr_engine() == "none":
            return [], "none (scanned PDF without OCR or TOC)"
        # 扫描件快速早停扫描：仅检查前 8 页，连续 4 页无目录或离开目录区即停止
        ocr_limit = min(8, total)
        for i in range(ocr_limit):
            pix = doc[i].get_pixmap(dpi=105)
            txt = ocr_image_rapidocr(pix.tobytes("png"))
            if _is_toc_like_text(txt):
                toc_pages_lines.append((i, txt.splitlines()))
            else:
                if toc_pages_lines:
                    break
                if i >= 3:
                    break

    if not toc_pages_lines:
        return [], "none (no TOC or bookmarks found)"

    extracted = []
    for _, lines in toc_pages_lines:
        extracted.extend(_parse_toc_lines(lines, total))

    if not extracted:
        return [], "none (TOC page found but entries unparsed)"

    offset = _calibrate_offset(doc, extracted, toc_pages_lines[-1][0], is_scanned_doc)

    index = []
    for i, (t, log_p) in enumerate(extracted):
        start_phys = min(total, max(1, log_p + offset))
        end_phys = total
        if i + 1 < len(extracted):
            nxt_phys = min(total, max(1, extracted[i + 1][1] + offset))
            end_phys = max(start_phys, nxt_phys - 1)

        index.append({
            "index": i + 1,
            "level": 1,
            "title": t,
            "start_phys": start_phys,
            "end_phys": end_phys,
            "page_count": end_phys - start_phys + 1,
        })

    src = "scanned OCR TOC + calibrated offset" if is_scanned_doc else "extracted text TOC + offset"
    return index, src


def build_index_from_headings(doc) -> list:
    """稀疏/退化书签兜底：基于字号直方图与加粗标题样式扫描数字版 PDF 章节结构"""
    total = doc.page_count
    if total == 0:
        return []

    flags = getattr(pymupdf, "TEXTFLAGS_TEXT", 0)
    size_counts: Dict[float, int] = {}
    candidates = []

    heading_pat = re.compile(
        r"^(?:第[0-9一二三四五六七八九十百]+[章节篇部]|Chapter\s+\d+|Part\s+[IVX\d]+|"
        r"Materials\s+and\s+Methods|Behavioral\s+assays|Electrophysiological\s+Recordings|"
        r"References(?:\s+and\s+Notes)?|Appendix|Table\s+S?\d+|Fig(?:ure)?\.?\s+S?\d+|Movie\s+S?\d+|"
        r"\d+(?:\.\d+){0,2}\s+\S+)",
        re.IGNORECASE,
    )

    for pno in range(total):
        d = doc[pno].get_text("dict", flags=flags)
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                spans = [s for s in l.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                txt = "".join(s["text"] for s in spans).strip()
                sz = round(max(s["size"] for s in spans), 1)
                is_bold = any(s.get("flags", 0) & 16 for s in spans)
                size_counts[sz] = size_counts.get(sz, 0) + len(txt)
                if 2 <= len(txt) <= 75 and not txt.isdigit():
                    candidates.append((pno + 1, sz, is_bold, txt))

    if not size_counts or not candidates:
        return []

    body_sz = max(size_counts.items(), key=lambda x: x[1])[0]
    toc_items = []
    seen = set()
    for p, sz, is_bold, txt in candidates:
        # 过滤常见页眉网址与年份
        if txt.startswith(("http://", "https://", "www.")) or "DOI:" in txt:
            continue
        is_large = sz >= body_sz * 1.18
        is_pattern_bold = is_bold and bool(heading_pat.match(txt))
        if not (is_large or is_pattern_bold):
            continue
        key = (p, txt.lower())
        if key in seen:
            continue
        seen.add(key)
        level = 1 if (sz >= body_sz * 1.18 or re.match(r"^(?:第.+章|Chapter\s+\d+|Materials|References)", txt, re.I)) else 2
        toc_items.append({"level": level, "title": txt, "phys": p})

    if len(toc_items) < 2:
        return []

    return build_index_from_toc_entries(toc_items, total)


def is_degenerate_toc(raw_toc: list, total_pages: int) -> bool:
    """判断 PDF 内嵌书签是否退化无用（如仅包含文件名和 References 共 1~2 条）"""
    if not raw_toc:
        return True
    if total_pages >= 8 and len(raw_toc) <= 2:
        first_title = str(raw_toc[0][1]).strip().lower()
        if first_title.endswith((".pdf", ".doc", ".docx")):
            return True
        if len(raw_toc) == 1:
            return True
    return False


def build_pdf_index_data(pdf_path: str, allow_ocr_toc: bool = True, force_layout: bool = False) -> dict:
    """PDF 章节索引统一构建入口（书签自愈 -> 印刷目录/OCR -> 样式标题/Paper Mode 布局分析兜底）"""
    if pymupdf is None:
        raise RuntimeError("PyMuPDF is required for PDF indexing. Run: pip install pymupdf")

    doc = pymupdf.open(pdf_path)
    try:
        if doc.needs_pass:
            return {
                "schema_version": INDEX_SCHEMA_VERSION,
                "file_path": pdf_path,
                "format": "PDF",
                "total_pages": doc.page_count,
                "source": "error (encrypted PDF)",
                "is_virtual": False,
                "chapters": [],
            }

        raw_toc = doc.get_toc()
        index = []
        source = "none (no TOC or bookmarks found)"
        is_virtual = False

        if force_layout:
            try:
                from paper_parser import build_virtual_index
                index = build_virtual_index(pdf_path)
            except Exception:
                index = []
            if not index:
                index = build_index_from_headings(doc)
            source = "layout analysis (paper mode, forced)"
            is_virtual = True
        else:
            if raw_toc and not is_degenerate_toc(raw_toc, doc.page_count):
                healed = heal_toc_entries(raw_toc, doc.page_count, doc=doc)
                index = build_index_from_toc_entries(healed, doc.page_count)
                source = "bookmarks (healed get_toc)"

            if not index:
                index, source = build_index_from_text_toc(doc, allow_ocr=allow_ocr_toc)

            if not index:
                heading_idx = build_index_from_headings(doc)
                if heading_idx:
                    index = heading_idx
                    source = "layout headings scan"
                    is_virtual = True
                else:
                    try:
                        from paper_parser import build_virtual_index
                        v_idx = build_virtual_index(pdf_path)
                    except Exception:
                        v_idx = []
                    if v_idx:
                        index = v_idx
                        source = "layout analysis (paper mode)"
                        is_virtual = True
                    elif raw_toc:
                        healed = heal_toc_entries(raw_toc, doc.page_count, doc=doc)
                        index = build_index_from_toc_entries(healed, doc.page_count)
                        source = "bookmarks (sparse get_toc)"

        return {
            "schema_version": INDEX_SCHEMA_VERSION,
            "file_path": pdf_path,
            "format": "PDF",
            "total_pages": doc.page_count,
            "source": source,
            "is_virtual": is_virtual,
            "chapters": index,
        }
    finally:
        doc.close()


def _load_or_build_pdf_chapters(doc, pdf_path: Optional[str]) -> list:
    """优先复用同路径有效缓存索引（校验 schema_version），否则现场构建"""
    if pdf_path:
        stem = os.path.splitext(os.path.basename(pdf_path))[0]
        cache_file = os.path.join(get_cache_dir(pdf_path), f"{stem}_chapters.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                if (
                    cached.get("schema_version") == INDEX_SCHEMA_VERSION
                    and cached.get("total_pages") == doc.page_count
                    and cached.get("chapters")
                ):
                    return cached["chapters"]
            except Exception:
                pass

    raw_toc = doc.get_toc()
    if raw_toc and not is_degenerate_toc(raw_toc, doc.page_count):
        healed = heal_toc_entries(raw_toc, doc.page_count, doc=doc)
        return build_index_from_toc_entries(healed, doc.page_count)

    idx, _ = build_index_from_text_toc(doc, allow_ocr=True)
    if idx:
        return idx

    idx = build_index_from_headings(doc)
    if idx:
        return idx

    if raw_toc:
        healed = heal_toc_entries(raw_toc, doc.page_count, doc=doc)
        return build_index_from_toc_entries(healed, doc.page_count)
    return []


def find_pdf_chapter_range(
    doc,
    chapter_query: Optional[str] = None,
    chapter_index: Optional[int] = None,
    pdf_path: Optional[str] = None,
) -> tuple:
    """
    统一 PDF 章节定位器（支持书签自愈、缓存复用、无书签印刷目录与标题扫描）：
    支持 --index 序号或 --chapter 模糊关键词检索。
    """
    chapters = _load_or_build_pdf_chapters(doc, pdf_path)
    if not chapters:
        return None, None, None

    if chapter_index is not None:
        for ch in chapters:
            if ch.get("index") == chapter_index:
                return ch["title"], ch["start_phys"], ch["end_phys"]
        return None, None, None

    if not chapter_query:
        return None, None, None

    q = chapter_query.strip()
    q_lower = q.lower()
    q_norm = re.sub(r"[\s\-_.:：·]+", "", q_lower)

    # 1. 严格全等
    for ch in chapters:
        if ch["title"].strip().lower() == q_lower:
            return ch["title"], ch["start_phys"], ch["end_phys"]

    # 2. 章节号前缀/边界匹配
    pattern = r"(?:^|[\s第])" + re.escape(q_lower) + r"(?:[\s.、:：章节]|$)"
    for ch in chapters:
        t_lower = ch["title"].lower()
        if re.search(pattern, t_lower) or t_lower.startswith(q_lower):
            return ch["title"], ch["start_phys"], ch["end_phys"]

    # 3. 归一化子串匹配
    for ch in chapters:
        t_norm = re.sub(r"[\s\-_.:：·]+", "", ch["title"].lower())
        if q_norm and q_norm in t_norm:
            return ch["title"], ch["start_phys"], ch["end_phys"]

    # 4. 普通子串包含
    for ch in chapters:
        if q_lower in ch["title"].lower():
            return ch["title"], ch["start_phys"], ch["end_phys"]

    return None, None, None


# ==============================================================================
# 4. 高速高精度版面感知 PDF -> Markdown 转换器
# ==============================================================================

def _collect_running_margin_noise(pages_dicts: list, page_heights: list) -> set:
    """统计跨页重复出现的页眉/页脚短文本（数字归一化）"""
    n_pages = len(pages_dicts)
    if n_pages < 3:
        return set()

    freq: Dict[str, int] = {}
    for (_, d), h in zip(pages_dicts, page_heights):
        seen_on_page = set()
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                y0, y1 = l["bbox"][1], l["bbox"][3]
                if y0 > 55 and y1 < h - 55:
                    continue
                txt = "".join(s.get("text", "") for s in l.get("spans", [])).strip()
                if 1 <= len(txt) <= 60:
                    norm = re.sub(r"\d+", "#", txt)
                    seen_on_page.add(norm)
        for norm in seen_on_page:
            freq[norm] = freq.get(norm, 0) + 1

    threshold = max(2, int(n_pages * 0.4))
    return {norm for norm, cnt in freq.items() if cnt >= threshold}


def _join_spans_as_line(spans: list) -> str:
    """智能拼接同行 spans，兼顾中英文间隔与上/下标紧凑连接"""
    spans_sorted = sorted(spans, key=lambda s: s["x0"])
    out = [spans_sorted[0]["text"]]
    prev = spans_sorted[0]
    for cur in spans_sorted[1:]:
        prev_txt = out[-1]
        cur_txt = cur["text"]
        if not cur_txt:
            continue
        gap = cur["x0"] - prev["x1"]
        is_sub_or_sup = cur["sz"] < prev["sz"] * 0.72 or prev["sz"] < cur["sz"] * 0.72
        if (
            not is_sub_or_sup
            and gap > max(1.5, 0.16 * min(prev["sz"], cur["sz"]))
            and not prev_txt.endswith((" ", "\t", "-", "/", "(", "（", "「", "《"))
            and not cur_txt.startswith((" ", "\t", ".", ",", ";", ":", ")", "）", "」", "》"))
            and (prev_txt[-1].isascii() and cur_txt[0].isascii())
        ):
            out.append(" " + cur_txt)
        else:
            out.append(cur_txt)
        prev = cur
    return "".join(out).strip()


def _group_spans_to_lines(span_records: list, body_sz: float) -> list:
    """使用排版基线 origin[1] 聚合同行文字，彻底规避字体 bbox 膨胀导致的错行"""
    if not span_records:
        return []

    # 先按主要正文 span（非微小角标）建立行基线，再把角标按垂直重叠归并进去
    normal_spans = [s for s in span_records if s["sz"] >= body_sz * 0.7]
    small_spans = [s for s in span_records if s["sz"] < body_sz * 0.7]
    if not normal_spans:
        normal_spans = span_records
        small_spans = []

    normal_spans.sort(key=lambda s: (s["oy"], s["x0"]))
    rows = []  # [{"oy": float, "y0": float, "y1": float, "spans": list}]
    tol = max(3.0, body_sz * 0.42)

    for s in normal_spans:
        placed = False
        for row in reversed(rows):
            if abs(s["oy"] - row["oy"]) <= tol:
                row["spans"].append(s)
                n = len(row["spans"])
                row["oy"] = (row["oy"] * (n - 1) + s["oy"]) / n
                row["y0"] = min(row["y0"], s["y0"])
                row["y1"] = max(row["y1"], s["y1"])
                placed = True
                break
            if s["oy"] - row["oy"] > tol * 3:
                break
        if not placed:
            rows.append({"oy": s["oy"], "y0": s["y0"], "y1": s["y1"], "spans": [s]})

    # 将上下标归并到最邻近的垂直行内（保留原顺序）
    for s in small_spans:
        best_row = None
        best_dist = 1e9
        for row in rows:
            dist = abs(s["oy"] - row["oy"])
            if dist <= body_sz * 0.95 and dist < best_dist:
                best_dist = dist
                best_row = row
        if best_row is not None:
            best_row["spans"].append(s)
        else:
            rows.append({"oy": s["oy"], "y0": s["y0"], "y1": s["y1"], "spans": [s]})

    rows.sort(key=lambda r: r["oy"])
    result = []
    for r in rows:
        text = _join_spans_as_line(r["spans"])
        if not text:
            continue
        max_sz = max(s["sz"] for s in r["spans"])
        is_bold = any(s["bold"] for s in r["spans"] if s["sz"] >= max_sz * 0.9)
        min_x0 = min(s["x0"] for s in r["spans"])
        result.append({
            "oy": r["oy"],
            "y0": r["y0"],
            "y1": r["y1"],
            "x0": min_x0,
            "max_sz": max_sz,
            "bold": is_bold,
            "text": text,
        })
    return result


def _detect_two_column_split(span_records: list, page_w: float) -> Optional[float]:
    """检测是否为双栏排版，若是则返回分栏中线 x"""
    if len(span_records) < 20:
        return None
    mid = page_w * 0.5
    left_only = sum(1 for s in span_records if s["x1"] < mid - 4)
    right_only = sum(1 for s in span_records if s["x0"] > mid + 4)
    crossing = sum(1 for s in span_records if s["x0"] < mid - 12 and s["x1"] > mid + 12)
    if left_only >= 8 and right_only >= 8 and crossing <= max(3, int(len(span_records) * 0.08)):
        return mid
    return None


def extract_pdf_as_markdown_fast(doc, start_phys: int, end_phys: int) -> str:
    """
    高速、高保真 PyMuPDF 版面感知 Markdown 提取：
    - 基于 origin[1] 基线对齐（杜绝外文字体 bbox 膨胀引发假表格与错行）
    - 矢量线段探测 + page.find_tables() 精准转换 Markdown 表格
    - 跨页重复页眉页脚/页码自动过滤、字号标题还原、双栏阅读顺序支持
    """
    flags = getattr(pymupdf, "TEXTFLAGS_TEXT", 0)
    pages_dicts = []
    page_heights = []
    size_counts: Dict[float, int] = {}

    for pno in range(start_phys - 1, end_phys):
        page = doc[pno]
        d = page.get_text("dict", flags=flags)
        pages_dicts.append((pno + 1, page, d))
        page_heights.append(page.rect.height)
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                for s in l.get("spans", []):
                    t = s.get("text", "").strip()
                    if t:
                        sz = round(s.get("size", 12.0), 1)
                        size_counts[sz] = size_counts.get(sz, 0) + len(t)

    body_sz = max(size_counts.items(), key=lambda x: x[1])[0] if size_counts else 12.0
    noise_norms = _collect_running_margin_noise([(p, d) for p, _, d in pages_dicts], page_heights)

    md_pages = []
    for pnum, page, d in pages_dicts:
        page_w, page_h = page.rect.width, page.rect.height
        table_rects = []
        elements = []  # (sort_y, col_order, md_text)

        # 仅当页面存在矢量线框时调用 find_tables，并屏蔽 PyMuPDF 向 stdout 打印的提示以免污染 --json 输出
        if len(page.get_drawings()) >= 2:
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    tabs = page.find_tables()
                for t in tabs.tables:
                    data = t.extract()
                    if data and len(data) >= 2 and len(data[0]) >= 2:
                        non_empty_cols = sum(
                            1 for col_i in range(len(data[0]))
                            if any((row[col_i] or "").strip() for row in data)
                        )
                        if non_empty_cols >= 2:
                            table_rects.append(pymupdf.Rect(t.bbox))
                            elements.append((t.bbox[1], 0, "\n" + t.to_markdown().strip() + "\n"))
            except Exception:
                pass

        span_records = []
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for l in b.get("lines", []):
                lrect = pymupdf.Rect(l["bbox"])
                if any(lrect.intersects(tr) for tr in table_rects):
                    continue
                for s in l.get("spans", []):
                    raw_txt = s.get("text", "")
                    if not raw_txt.strip():
                        continue
                    origin = s.get("origin")
                    oy = origin[1] if origin else s["bbox"][3]
                    span_records.append({
                        "x0": s["bbox"][0],
                        "y0": s["bbox"][1],
                        "x1": s["bbox"][2],
                        "y1": s["bbox"][3],
                        "oy": oy,
                        "sz": s.get("size", body_sz),
                        "bold": bool(s.get("flags", 0) & 16),
                        "text": raw_txt,
                    })

        split_x = _detect_two_column_split(span_records, page_w)
        if split_x is not None:
            top_spans = [s for s in span_records if s["x0"] < split_x - 12 and s["x1"] > split_x + 12]
            left_spans = [s for s in span_records if s["x1"] <= split_x + 12 and s not in top_spans]
            right_spans = [s for s in span_records if s["x0"] >= split_x - 12 and s not in top_spans]
            groups = [(0, top_spans), (1, left_spans), (2, right_spans)]
        else:
            groups = [(0, span_records)]

        for col_idx, grp in groups:
            lines = _group_spans_to_lines(grp, body_sz)
            for idx_l, ln in enumerate(lines):
                s = ln["text"]
                if not s:
                    continue
                # 过滤页眉页脚噪声与孤立页码
                in_margin = (ln["oy"] < 55) or (ln["oy"] > page_h - 45)
                if in_margin:
                    norm = re.sub(r"\d+", "#", s)
                    if norm in noise_norms or (s.isdigit() and len(s) <= 4):
                        continue

                # 标题层级推断
                is_short = len(s) <= 80
                if is_short and ln["max_sz"] >= body_sz * 1.35:
                    line_md = f"## {s}"
                elif is_short and ln["max_sz"] >= body_sz * 1.15:
                    line_md = f"### {s}"
                elif is_short and ln["bold"] and not s.endswith((".", "。", "，", ",", ";", "；", ":")):
                    # 检查前后是否有行间距或独立段落特征
                    prev_gap = (ln["oy"] - lines[idx_l - 1]["oy"]) if idx_l > 0 else 999
                    if prev_gap > body_sz * 1.35:
                        line_md = f"### {s}"
                    else:
                        line_md = s
                else:
                    line_md = s

                sort_key = (0, ln["oy"]) if split_x is None else (col_idx, ln["oy"])
                elements.append((sort_key[1], col_idx, line_md))

        if split_x is not None:
            elements.sort(key=lambda el: (el[1], el[0]))
        else:
            elements.sort(key=lambda el: el[0])

        page_body = "\n".join(el[2] for el in elements).strip()
        md_pages.append(f"<!-- Page {pnum} -->\n{page_body}")

    return "\n\n".join(md_pages).strip()
