#!/usr/bin/env python3
"""
paper_parser.py - 无目录 PDF（学术论文 / 报告 / 长文）布局分析引擎

设计目标：不依赖书签、不依赖目录页、不依赖 OCR / 大模型，纯 PyMuPDF 字体度量，
为「没有任何目录信息」的 PDF 重建可查询的虚拟章节索引。

核心策略（相对度量，而非绝对字号阈值）：
  1. 按「行」聚合 span（避免一个标题被拆成多个碎片）；
  2. 统计全文字符加权众数字号作为「正文基准字号」；
  3. 标题 = 字号显著大于基准 / 加粗短行 / 编号标题 / 学术固定标题词；
  4. 过滤跨页重复的页眉页脚；
  5. 按 (物理页, y 坐标) 排序，生成互不冲突的页级区间；
  6. 额外提供双栏阅读顺序重排与严格的文末参考文献剥离。

所有函数均可被 probe_pdf.py / build_index.py / extract_chapter.py 直接 import。
也可独立运行：python paper_parser.py "paper.pdf" --print
"""

import os
import re
import sys
import json
import argparse
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import console_safe  # noqa: F401  # 安装窄编码控制台保护（含非 GBK 字符的文件名）

try:
    import pymupdf
except ImportError:
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None


# ---------------------------------------------------------------------------
# 标题识别规则
# ---------------------------------------------------------------------------

# 编号标题：1 / 1. / 1.2 / 1.2.3 / I. / Chapter 1 / 第1章 ...
# 注意：(?i:...) 只让前缀忽略大小写，后缀必须是大写字母 / 汉字开头，
# 否则 "30 for still;"、"10 ms) during..."、"72 h;" 这类表格碎片会被误判。
NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?i:(?:chapter|section|part|appendix)\s+\d+[\w.\-]*"
    r"|第\s*[0-9一二三四五六七八九十]+\s*[章节部分]"
    r"|[IVXLC]+\."
    r"|\d+(?:\.\d+){0,3}\.?)"
    r"\s+[A-Z\u4e00-\u9fff]"
)

# 学术论文固定标题词（必须整行匹配，避免误伤正文中的 "see References"）
ACADEMIC_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"abstract|summary|keywords?|introduction|background|related work|"
    r"literature review|methodology|methods?|materials and methods|"
    r"experiments?|experimental setup|results?|results and discussion|"
    r"discussion|conclusions?(?: and future work)?|acknowledg(?:e)?ments?|"
    r"references?|bibliography|references and notes|works cited|"
    r"appendix|supplementary(?: materials| information)?|"
    r"摘要|引言|方法|实验|结果|讨论|结论|致谢|参考文献|附录"
    r")\s*[:：]?\s*$",
    re.IGNORECASE,
)

# 文末参考文献标题（截断用，比 ACADEMIC_HEADING_RE 更严：只在文档后半段生效）
REF_HEADING_RE = re.compile(
    r"^\s*(?:\d+\.?\s*)?(?:references?|bibliography|references and notes|"
    r"works cited|参考文献)\s*[:：]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

BOLD_FLAG = 1 << 4  # PyMuPDF span flags bit 4 = bold


def _norm(text: str) -> str:
    """归一化文本用于去重 / 模糊匹配"""
    return re.sub(r"[\s\-_.:：·,，;；()（）\[\]]+", "", text.lower())


def _line_records(page, page_no: int) -> list:
    """把一页的 text dict 聚合成「行」记录，保留字号 / 粗体 / 坐标"""
    records = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:  # 0 = 文本, 1 = 图片
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            # 字符数加权平均字号，比 max 更抗上标/下标干扰
            weight = sum(len(s.get("text", "")) for s in spans) or 1
            avg_size = sum(s["size"] * len(s.get("text", "")) for s in spans) / weight
            max_size = max(s["size"] for s in spans)
            bold = any(s.get("flags", 0) & BOLD_FLAG for s in spans)
            x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
            records.append({
                "text": text,
                "size": round(avg_size, 2),
                "max_size": round(max_size, 2),
                "bold": bold,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "phys": page_no + 1,
            })
    return records


def _body_size(all_lines: list) -> float:
    """字符加权众数（0.5pt 分桶）作为正文基准字号"""
    buckets = defaultdict(float)
    for ln in all_lines:
        key = round(ln["size"] * 2) / 2
        buckets[key] += max(1, len(ln["text"]))
    if not buckets:
        return 10.0
    return max(buckets.items(), key=lambda kv: kv[1])[0]


def _is_running_head(ln: dict, page_h: float, repeat_counts: dict) -> bool:
    """跨页重复出现且位于页面上下边缘 -> 页眉 / 页脚 / 页码"""
    if not page_h:
        return False
    at_edge = ln["y0"] < 0.09 * page_h or ln["y1"] > 0.93 * page_h
    key = _norm(ln["text"])
    # 去掉页码数字后再统计一次（"Journal of X 12" / "Journal of X 13"）
    key_no_digit = re.sub(r"\d+", "", key)
    repeated = repeat_counts.get(key, 0) >= 3 or repeat_counts.get(key_no_digit, 0) >= 3
    return bool(at_edge and repeated)


def _looks_like_heading(ln: dict, body: float) -> bool:
    text = ln["text"]
    if len(text) > 100 or text.isdigit():
        return False

    # 通用排除：参考文献条目 / 表格碎片 / 正文段落的典型特征
    if re.search(r"\bdoi\s*:|https?://|\bet al\.|\(\d{4}\)|\bpp?\.\s*\d|arXiv", text, re.I):
        return False
    if text.count(",") >= 3:
        return False
    if len(text) < 25 and text.endswith((".", ";", ",")):   # "statistics." 之类碎片
        return False
    if len(re.findall(r"\b[A-Z]{3,}\b", text)) >= 2:         # "Number VALUE NUMBE method" 表头
        return False
    if len(text) > 45 and text.endswith((".", ",", ";", ":", ")", "]")):
        return False
    if re.search(r"\b(for|ms|vs|per)\b", text, re.I) and re.search(r"\d", text):
        return False

    size, bold = ln["max_size"], ln["bold"]

    # 1) 字号显著大于正文（相对阈值，适配 9pt 小字论文与 14pt 大字报告）
    if size >= body * 1.22:
        return True
    # 2) 加粗短行（很多期刊用同号加粗做小节标题）
    if bold and size >= body * 0.98 and len(text) <= 70:
        return True
    # 3) 编号标题（要求首字母大写，且不像引文）
    if NUMBERED_HEADING_RE.match(text) and len(text) <= 90 and text.count(",") <= 2:
        return True
    # 4) 学术固定标题词（整行）
    if ACADEMIC_HEADING_RE.match(text):
        return True
    return False


def detect_headings(pdf_path: str, max_pages: int = None, min_font_size: float = None) -> list:
    """
    扫描 PDF 全文，返回疑似标题列表（兼容旧签名，min_font_size 保留但不再作为主判据）。
    每项: {"title", "phys", "size", "bold", "y0"}
    """
    if pymupdf is None:
        raise RuntimeError("PyMuPDF is required. Run: pip install pymupdf")

    doc = pymupdf.open(pdf_path)
    try:
        total = doc.page_count
        scan_n = total if max_pages is None else min(total, max_pages)
        all_lines = []
        page_heights = {}
        for pno in range(scan_n):
            page = doc[pno]
            page_heights[pno] = page.rect.height
            all_lines.extend(_line_records(page, pno))

        if not all_lines:
            return []

        body = _body_size(all_lines)

        # 统计每条文本出现的不同页数，用于识别页眉页脚
        repeat_counts = defaultdict(int)
        seen_per_page = defaultdict(set)
        for ln in all_lines:
            key = _norm(ln["text"])
            seen_per_page[ln["phys"]].add(key)
            seen_per_page[ln["phys"]].add(re.sub(r"\d+", "", key))
        for keys in seen_per_page.values():
            for k in keys:
                repeat_counts[k] += 1

        # 文末参考文献之后的内容不再视为章节（首页导航里的 "References" 不算）
        ref_cut = None
        for ln in all_lines:
            if ln["phys"] > scan_n * 0.5 and REF_HEADING_RE.match(ln["text"]):
                ref_cut = (ln["phys"], ln["y0"])
                break

        candidates = []
        for ln in all_lines:
            if (ln["phys"] - 1) not in page_heights:   # 修正：phys 是 1-based，page_heights 是 0-based
                continue
            if ref_cut and (ln["phys"], ln["y0"]) > ref_cut:
                continue
            if _is_running_head(ln, page_heights[ln["phys"] - 1], repeat_counts):
                continue
            if min_font_size and ln["max_size"] < min_font_size:
                continue
            if _looks_like_heading(ln, body):
                candidates.append(ln)

        # 按页 + y 排序，合并同页 y 极近的碎片（标题换行）
        candidates.sort(key=lambda x: (x["phys"], x["y0"]))
        merged, last = [], None
        for ln in candidates:
            if last and last["phys"] == ln["phys"] and abs(ln["y0"] - last["y0"]) < 3:
                last["text"] = (last["text"] + " " + ln["text"]).strip()
                last["max_size"] = max(last["max_size"], ln["max_size"])
                continue
            merged.append(dict(ln))
            last = merged[-1]

        # 全局按归一化文本去重（保留首次出现）
        out, seen = [], set()
        for ln in merged:
            key = _norm(ln["text"])
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "title": ln["text"].strip(),
                "phys": ln["phys"],
                "size": ln["max_size"],
                "bold": ln["bold"],
                "y0": round(ln["y0"], 1),
            })
        return out
    finally:
        doc.close()


def build_virtual_index(pdf_path: str, max_pages: int = None) -> list:
    """
    构建虚拟章节索引，字段与 build_index.py 的 bookmarks 索引完全一致，
    因此 build_index.py --print / extract_chapter.py --chapter 可无缝消费。
    """
    headings = detect_headings(pdf_path, max_pages=max_pages)
    if not headings:
        return []

    doc = pymupdf.open(pdf_path)
    try:
        total_pages = doc.page_count
    finally:
        doc.close()

    index = []
    for i, h in enumerate(headings):
        start_phys = h["phys"]
        # 下一个「不同物理页」的标题作为本节结束点，避免同页多标题算出 0 页 / 负数区间
        nxt_phys = None
        for nxt in headings[i + 1:]:
            if nxt["phys"] > start_phys:
                nxt_phys = nxt["phys"]
                break
        end_phys = max(start_phys, (nxt_phys - 1) if nxt_phys else total_pages)
        index.append({
            "index": i + 1,
            "level": 1,
            "title": h["title"],
            "start_phys": start_phys,
            "end_phys": end_phys,
            "page_count": end_phys - start_phys + 1,
            "y0": h.get("y0", 0),
            "source": "layout",
        })
    return index


def find_in_virtual_index(query: str, index: list) -> tuple:
    """在虚拟索引中模糊匹配章节，返回 (title, start_phys, end_phys) 或 (None, None, None)"""
    q = query.strip().lower()
    q_norm = _norm(q)
    if not q or not index:
        return None, None, None

    # 1) 严格相等
    for it in index:
        if it["title"].strip().lower() == q:
            return it["title"], it["start_phys"], it["end_phys"]
    # 2) 章节号前缀 / 词边界
    pattern = r"(?:^|[\s第])" + re.escape(q) + r"(?:[\s.、:：章节]|$)"
    for it in index:
        if re.search(pattern, it["title"].lower()) or it["title"].lower().startswith(q):
            return it["title"], it["start_phys"], it["end_phys"]
    # 3) 归一化子串
    for it in index:
        if q_norm in _norm(it["title"]):
            return it["title"], it["start_phys"], it["end_phys"]
    # 4) 普通子串
    for it in index:
        if q in it["title"].lower():
            return it["title"], it["start_phys"], it["end_phys"]
    return None, None, None


def strip_references(text: str) -> str:
    """
    严格剥离文末参考文献：标题必须独占一行，且仅取文档后 40% 中最后一次出现的位置。
    避免误伤正文中 "see References"、DOI、句中提及等情形。
    """
    matches = list(REF_HEADING_RE.finditer(text))
    if not matches:
        return text
    tail_start = int(len(text) * 0.6)
    candidates = [m for m in matches if m.start() >= tail_start]
    if not candidates:
        return text
    cut = candidates[-1].start()
    tail = text[:cut].rstrip()

    # 清理尾部孤立的页面注释 / 页眉（参考文献常独占最后一页开头，正文已被截掉）
    marks = list(re.finditer(r"<!--\s*Page\s+\d+\s*-->", tail))
    if marks:
        last = marks[-1]
        residual = tail[last.end():].strip()
        if len(residual) < 200 and not residual.endswith((".", "。", "!", "?", ":", "：")):
            tail = tail[:last.start()].rstrip()

    return tail + "\n\n> 💡 *注：文末参考文献部分已按 --strip-refs 剥离。*\n"


def detect_columns(page, threshold: float = 0.5) -> bool:
    """粗判页面是否为双栏：左右半页各有足量且互不越界的文本行"""
    lines = _line_records(page, 0)
    if len(lines) < 12:
        return False
    w = page.rect.width or 1
    left = [l for l in lines if l["x1"] <= w * 0.52]
    right = [l for l in lines if l["x0"] >= w * 0.48]
    return len(left) >= 5 and len(right) >= 5


def page_text_column_aware(page) -> str:
    """双栏 PDF 阅读顺序重排：先左栏自上而下，再右栏自上而下"""
    lines = _line_records(page, 0)
    if not lines:
        return page.get_text()
    w = page.rect.width or 1
    left = sorted([l for l in lines if l["x0"] < w * 0.5], key=lambda l: l["y0"])
    right = sorted([l for l in lines if l["x0"] >= w * 0.5], key=lambda l: l["y0"])
    if len(left) < 5 or len(right) < 5:
        return page.get_text()
    return "\n".join(l["text"] for l in left) + "\n" + "\n".join(l["text"] for l in right)


def classify_document(doc, sample_pages: int = 8) -> dict:
    """启发式判定文档类型，供探针输出更精准的推荐路线"""
    total = doc.page_count
    head_text = " ".join(doc[i].get_text() for i in range(min(sample_pages, total))).lower()
    has_abstract = bool(re.search(r"\babstract\b|\b摘要\b", head_text))
    has_keywords = bool(re.search(r"\bkeywords?\b|\b关键词\b", head_text))
    two_col = 0
    for i in range(min(sample_pages, total)):
        if detect_columns(doc[i]):
            two_col += 1
    is_two_column = two_col >= max(2, min(sample_pages, total) // 3)
    is_paper = total <= 80 and (has_abstract or has_keywords or is_two_column)
    return {
        "doc_type": "paper" if is_paper else "book/report",
        "has_abstract": has_abstract,
        "is_two_column": is_two_column,
    }


def main():
    parser = argparse.ArgumentParser(description="Layout-based virtual index for TOC-less PDFs (papers / reports).")
    parser.add_argument("file_path", help="Path to PDF")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit scanned pages (default: all)")
    parser.add_argument("--print", "-p", dest="do_print", action="store_true", help="Print virtual chapter table")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    if not os.path.exists(args.file_path):
        print(f"Error: File not found: {args.file_path}")
        return 1
    index = build_virtual_index(args.file_path, max_pages=args.max_pages)

    if args.json:
        print(json.dumps(index, ensure_ascii=True, indent=2))
        return 0

    print(f"Detected {len(index)} virtual headings (layout analysis, no TOC required).")
    if args.do_print:
        print("=" * 72)
        for it in index:
            print(f"{it['index']:<4} | {it['title'][:46]:<46} | p{it['start_phys']}-{it['end_phys']}")
        print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
