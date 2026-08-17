#!/usr/bin/env python3
"""
epub_parser.py - EPUB 格式电子书解析与按需提取引擎
支持 EPUB 2 (toc.ncx) 与 EPUB 3 (nav.xhtml)，以及 spine 兜底；
支持单章节 HTML 提取与高质量 Markdown 转换。
"""

import os
import sys
import re
import json
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urldefrag
from typing import List, Dict, Tuple, Optional, Any

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


def purge_useless_navigation(soup: BeautifulSoup) -> None:
    """清理 DOM 中无意义的导航、页眉页脚与顶底跳转链接，防止稀释 Agent 上下文"""
    if not soup:
        return
    for tag in soup.find_all(["nav", "header", "footer"]):
        epub_type = tag.get("epub:type", "")
        classes = " ".join(tag.get("class", []))
        if "toc" in epub_type or "nav" in classes or "pagination" in classes or tag.name in ("header", "footer"):
            tag.decompose()

    for a in soup.find_all("a"):
        text = a.get_text().strip().lower()
        if text in ("[top]", "[next]", "[previous]", "[contents]", "[back to top]", "top", "contents"):
            a.decompose()
        elif not text and not a.find(["img", "image"]):
            a.decompose()


# XML 命名空间常量
NAMESPACES = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}


def _strip_ns(tag: str) -> str:
    """去除 XML 标签的命名空间前缀"""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def enhance_pre_code_tags(soup: BeautifulSoup) -> None:
    """
    预处理 HTML 中的代码块结构 (<pre>, <code>, <tt>, 以及等宽字体、类名标有 code/program/listing 的元素)，
    将其规范化为标准的 fenced code block 格式，防止被 markdown 转换器错误折行或退化为普通文本。
    """
    if not soup:
        return

    def detect_lang(classes: list, text: str) -> str:
        c_str = " ".join(classes).lower()
        if "cpp" in c_str or "c++" in c_str:
            return "cpp"
        if "golang" in c_str or "go" in c_str.split():
            return "go"
        if "python" in c_str or "py" in c_str.split():
            return "python"
        if "java" in c_str.split():
            return "java"
        if "rust" in c_str.split() or "rs" in c_str.split():
            return "rust"
        if "javascript" in c_str or "js" in c_str.split() or "typescript" in c_str or "ts" in c_str.split():
            return "javascript"
        if "bash" in c_str or "sh" in c_str.split() or "shell" in c_str:
            return "bash"
        if "sql" in c_str.split():
            return "sql"
        if "html" in c_str.split() or "xml" in c_str.split():
            return "html"
        if "c" in c_str.split():
            return "c"
        if "#include" in text or "std::" in text or "cout <<" in text:
            return "cpp"
        if "package " in text and ("func " in text or "import (" in text or ":=" in text):
            return "go"
        if "def " in text and (":" in text or "import " in text or "print(" in text):
            return "python"
        if "printf(" in text or "int main(" in text or "char *" in text:
            return "c"
        return ""

    # 1. 处理标准的 <pre> 块
    for pre in list(soup.find_all("pre")):
        code_tag = pre.find("code")
        classes = pre.get("class", [])
        if code_tag and code_tag.get("class"):
            classes = classes + code_tag.get("class", [])
        code_text = pre.get_text()
        lang = detect_lang(classes, code_text)
        pre.string = f"\n```{lang}\n{code_text.strip()}\n```\n"

    # 2. 处理 class 名带 code / program / listing / monospace 或含有 monospace 样式的 div / p
    code_selectors = ["div", "p"]
    for tag in list(soup.find_all(code_selectors)):
        if tag.find_parent("pre"):
            continue
        classes = tag.get("class", [])
        c_str = " ".join(classes).lower()
        style = (tag.get("style") or "").lower()

        is_code_container = any(kw in c_str for kw in ["code", "program", "listing", "syntax", "snippet", "monospace"]) or \
                            any(kw in style for kw in ["monospace", "courier", "consolas", "menlo"])

        if is_code_container:
            code_text = tag.get_text()
            if code_text.strip() and (len(code_text.splitlines()) >= 2 or any(kw in code_text for kw in [";", "{", "}", "()", "def ", "func ", "int ", "#include"])):
                lang = detect_lang(classes, code_text)
                tag.string = f"\n```{lang}\n{code_text.strip()}\n```\n"

    # 3. 处理 <font face="monospace|Courier|Consolas"> 或多行 <tt>
    for font_or_tt in list(soup.find_all(["font", "tt"])):
        if font_or_tt.find_parent("pre"):
            continue
        face = (font_or_tt.get("face") or "").lower()
        is_mono = font_or_tt.name == "tt" or any(kw in face for kw in ["monospace", "courier", "consolas"])
        if is_mono:
            text = font_or_tt.get_text()
            if len(text.splitlines()) >= 2 or len(text.strip()) > 80:
                lang = detect_lang([], text)
                font_or_tt.string = f"\n```{lang}\n{text.strip()}\n```\n"


class EpubBook:
    """EPUB 电子书解析器"""

    def __init__(self, epub_path: str):
        self.epub_path = os.path.abspath(epub_path)
        if not os.path.exists(self.epub_path):
            raise FileNotFoundError(f"EPUB file not found: {self.epub_path}")

        self.zip_file = zipfile.ZipFile(self.epub_path, "r")
        self.opf_path = self._find_opf_path()
        self.opf_dir = os.path.dirname(self.opf_path)
        self.metadata, self.manifest, self.spine = self._parse_opf()
        self.toc = self._parse_toc()

    def close(self):
        if self.zip_file:
            self.zip_file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _read_file_text(self, internal_path: str, encoding: str = "utf-8") -> str:
        """从 zip 中读取文本文件内容"""
        norm_path = internal_path.replace("\\", "/").lstrip("/")
        try:
            raw = self.zip_file.read(norm_path)
            return raw.decode(encoding, errors="replace")
        except KeyError:
            for name in self.zip_file.namelist():
                if name.lower() == norm_path.lower():
                    raw = self.zip_file.read(name)
                    return raw.decode(encoding, errors="replace")
            raise FileNotFoundError(f"Path '{internal_path}' not found in EPUB archive.")

    def _read_file_bytes(self, internal_path: str) -> bytes:
        """从 zip 中读取二进制内容"""
        norm_path = internal_path.replace("\\", "/").lstrip("/")
        try:
            return self.zip_file.read(norm_path)
        except KeyError:
            for name in self.zip_file.namelist():
                if name.lower() == norm_path.lower():
                    return self.zip_file.read(name)
            raise FileNotFoundError(f"Path '{internal_path}' not found in EPUB archive.")

    def _find_opf_path(self) -> str:
        """从 META-INF/container.xml 定位 content.opf 路径"""
        container_xml = self._read_file_text("META-INF/container.xml")
        root = ET.fromstring(container_xml)
        for elem in root.findall(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"):
            full_path = elem.get("full-path")
            if full_path:
                return full_path
        for elem in root.findall(".//rootfile"):
            full_path = elem.get("full-path")
            if full_path:
                return full_path
        raise ValueError("Could not find rootfile in META-INF/container.xml")

    def _parse_opf(self) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]], List[str]]:
        """解析 OPF 文件元数据、Manifest 和 Spine"""
        opf_xml = self._read_file_text(self.opf_path)
        root = ET.fromstring(opf_xml)

        metadata = {}
        manifest = {}
        spine = []

        for child in root:
            tag = _strip_ns(child.tag).lower()
            if tag == "metadata":
                for m in child:
                    mtag = _strip_ns(m.tag).lower()
                    if mtag == "title":
                        metadata["title"] = (m.text or "").strip()
                    elif mtag == "creator":
                        metadata["author"] = (m.text or "").strip()
                    elif mtag == "language":
                        metadata["language"] = (m.text or "").strip()
                    elif mtag == "identifier":
                        metadata["identifier"] = (m.text or "").strip()
                    elif mtag == "publisher":
                        metadata["publisher"] = (m.text or "").strip()
            elif tag == "manifest":
                for item in child:
                    item_id = item.get("id")
                    href = item.get("href")
                    media_type = item.get("media-type")
                    properties = item.get("properties", "")
                    if item_id and href:
                        href_unquoted = unquote(href)
                        if self.opf_dir:
                            full_path = os.path.normpath(f"{self.opf_dir}/{href_unquoted}").replace("\\", "/")
                        else:
                            full_path = os.path.normpath(href_unquoted).replace("\\", "/")
                        manifest[item_id] = {
                            "id": item_id,
                            "href": href_unquoted,
                            "full_path": full_path,
                            "media_type": media_type,
                            "properties": properties,
                        }
            elif tag == "spine":
                self.spine_toc_id = child.get("toc")
                for itemref in child:
                    idref = itemref.get("idref")
                    if idref:
                        spine.append(idref)

        return metadata, manifest, spine

    def _parse_toc(self) -> List[Dict[str, Any]]:
        """提取 TOC 目录，优先 EPUB 3 (nav.xhtml)，其次 EPUB 2 (toc.ncx)，最后 Spine 兜底"""
        nav_item = None
        for item in self.manifest.values():
            if "nav" in item.get("properties", "").split():
                nav_item = item
                break

        if nav_item:
            try:
                toc = self._parse_nav_xhtml(nav_item["full_path"])
                if toc:
                    for i, t in enumerate(toc):
                        t["index"] = i + 1
                    return toc
            except Exception:
                pass

        ncx_item = None
        if hasattr(self, "spine_toc_id") and self.spine_toc_id:
            ncx_item = self.manifest.get(self.spine_toc_id)

        if not ncx_item:
            for item in self.manifest.values():
                if item.get("media_type") == "application/x-dtbncx+xml" or item["href"].lower().endswith(".ncx"):
                    ncx_item = item
                    break

        if ncx_item:
            try:
                toc = self._parse_toc_ncx(ncx_item["full_path"])
                if toc:
                    for i, t in enumerate(toc):
                        t["index"] = i + 1
                    return toc
            except Exception:
                pass

        toc = self._build_toc_from_spine()
        for i, t in enumerate(toc):
            t["index"] = i + 1
        return toc

    def _parse_toc_ncx(self, ncx_path: str) -> List[Dict[str, Any]]:
        """解析 toc.ncx XML"""
        ncx_xml = self._read_file_text(ncx_path)
        root = ET.fromstring(ncx_xml)
        ncx_dir = os.path.dirname(ncx_path)
        toc = []

        def parse_navpoint(elem, level=1):
            title = ""
            for lbl in elem.findall("{http://www.daisy.org/z3986/2005/ncx/}navLabel") or elem.findall("navLabel"):
                txt = lbl.find("{http://www.daisy.org/z3986/2005/ncx/}text") or lbl.find("text")
                if txt is not None and txt.text:
                    title = txt.text.strip()
                    break

            src = ""
            for cnt in elem.findall("{http://www.daisy.org/z3986/2005/ncx/}content") or elem.findall("content"):
                src = cnt.get("src", "")
                if src:
                    break

            if title and src:
                src_unquoted = unquote(src)
                src_clean, anchor = urldefrag(src_unquoted)
                if ncx_dir:
                    full_path = os.path.normpath(f"{ncx_dir}/{src_clean}").replace("\\", "/")
                else:
                    full_path = os.path.normpath(src_clean).replace("\\", "/")

                toc.append({
                    "title": title,
                    "level": level,
                    "src": src_unquoted,
                    "file_path": full_path,
                    "anchor": anchor or None,
                })

            for child in elem.findall("{http://www.daisy.org/z3986/2005/ncx/}navPoint") or elem.findall("navPoint"):
                parse_navpoint(child, level + 1)

        nav_map = root.find("{http://www.daisy.org/z3986/2005/ncx/}navMap")
        if nav_map is None:
            nav_map = root.find("navMap")

        if nav_map is not None:
            for np in nav_map.findall("{http://www.daisy.org/z3986/2005/ncx/}navPoint") or nav_map.findall("navPoint"):
                parse_navpoint(np, 1)

        return toc

    def _parse_nav_xhtml(self, nav_path: str) -> List[Dict[str, Any]]:
        """解析 EPUB 3 的 nav.xhtml"""
        nav_html = self._read_file_text(nav_path)
        nav_dir = os.path.dirname(nav_path)
        toc = []

        if BeautifulSoup:
            soup = BeautifulSoup(nav_html, "html.parser")
            nav_elem = soup.find("nav", attrs={"epub:type": "toc"}) or soup.find("nav")
            if not nav_elem:
                return []

            def parse_ol(ol_tag, current_level=1):
                for li in ol_tag.find_all("li", recursive=False):
                    a = li.find("a", recursive=False)
                    if a and a.get("href"):
                        href = unquote(a["href"].strip())
                        title = a.get_text().strip()
                        src_clean, anchor = urldefrag(href)
                        if nav_dir:
                            full_path = os.path.normpath(f"{nav_dir}/{src_clean}").replace("\\", "/")
                        else:
                            full_path = os.path.normpath(src_clean).replace("\\", "/")
                        toc.append({
                            "title": title,
                            "level": current_level,
                            "src": href,
                            "file_path": full_path,
                            "anchor": anchor or None,
                        })

                    sub_ol = li.find("ol", recursive=False)
                    if sub_ol:
                        parse_ol(sub_ol, current_level + 1)

            top_ol = nav_elem.find("ol")
            if top_ol:
                parse_ol(top_ol, 1)
        else:
            matches = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', nav_html, re.DOTALL | re.IGNORECASE)
            for href, raw_text in matches:
                clean_text = re.sub(r"<[^>]+>", "", raw_text).strip()
                if clean_text:
                    src_clean, anchor = urldefrag(href)
                    if nav_dir:
                        full_path = os.path.normpath(f"{nav_dir}/{src_clean}").replace("\\", "/")
                    else:
                        full_path = os.path.normpath(src_clean).replace("\\", "/")
                    toc.append({
                        "title": clean_text,
                        "level": 1,
                        "src": href,
                        "file_path": full_path,
                        "anchor": anchor or None,
                    })

        return toc

    def _build_toc_from_spine(self) -> List[Dict[str, Any]]:
        """从 Spine 构建章节目录（兜底方案）"""
        toc = []
        for idref in self.spine:
            item = self.manifest.get(idref)
            if not item:
                continue
            full_path = item["full_path"]
            title = idref
            try:
                html_text = self._read_file_text(full_path)
                m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
                if m and m.group(1).strip():
                    title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                else:
                    h1 = re.search(r"<h[1-2][^>]*>(.*?)</h[1-2]>", html_text, re.IGNORECASE | re.DOTALL)
                    if h1 and h1.group(1).strip():
                        title = re.sub(r"<[^>]+>", "", h1.group(1)).strip()
            except Exception:
                pass

            toc.append({
                "title": title or idref,
                "level": 1,
                "src": item["href"],
                "file_path": full_path,
                "anchor": None,
            })
        return toc

    def get_probe_info(self) -> Dict[str, Any]:
        """获取元数据探针报告"""
        return {
            "file": os.path.basename(self.epub_path),
            "path": self.epub_path,
            "format": "EPUB",
            "is_digital": True,
            "title": self.metadata.get("title", ""),
            "author": self.metadata.get("author", ""),
            "language": self.metadata.get("language", ""),
            "total_spine_items": len(self.spine),
            "has_toc": len(self.toc) > 0,
            "toc_count": len(self.toc),
            "category": "E (Reflowable EPUB)",
            "recommendation": "Pure Python zip+xhtml parse -> Native HTML to Markdown (0.05s, 0 Token, 100% precision)",
            "toc_sample": [
                {"index": item["index"], "level": item["level"], "title": item["title"], "src": item.get("src", "")}
                for item in self.toc[:10]
            ],
        }

    def find_chapter(self, query: str = None, index: int = None) -> Optional[Dict[str, Any]]:
        """根据章节名关键词或序号检索章节"""
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

    def extract_chapter_html(self, chapter_info: Dict[str, Any]) -> str:
        """提取目标章节的原始 HTML 片段"""
        file_path = chapter_info["file_path"]
        anchor = chapter_info.get("anchor")

        full_html = self._read_file_text(file_path)
        if not anchor or not BeautifulSoup:
            return full_html

        soup = BeautifulSoup(full_html, "html.parser")
        target_elem = soup.find(id=anchor) or soup.find(attrs={"name": anchor})
        if target_elem:
            if target_elem.name in ("section", "div", "article", "chapter"):
                return str(target_elem)
            return full_html

        return full_html

    def extract_chapter_images(self, chapter_info: Dict[str, Any], output_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        """提取目标章节关联的所有图片"""
        file_path = chapter_info["file_path"]
        ch_dir = os.path.dirname(file_path)
        full_html = self._read_file_text(file_path)

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        extracted = []
        if BeautifulSoup:
            soup = BeautifulSoup(full_html, "html.parser")
            imgs = soup.find_all(["img", "image"])
        else:
            imgs = []
            for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', full_html, re.I):
                imgs.append({"src": m.group(1), "alt": ""})

        for i, img_tag in enumerate(imgs):
            if hasattr(img_tag, "get"):
                src = img_tag.get("src") or img_tag.get("xlink:href")
                alt = img_tag.get("alt", "")
            else:
                src = img_tag["src"]
                alt = img_tag.get("alt", "")

            if not src:
                continue

            src_clean = unquote(src).split("?")[0].split("#")[0]
            if ch_dir:
                internal_img_path = os.path.normpath(f"{ch_dir}/{src_clean}").replace("\\", "/")
            else:
                internal_img_path = os.path.normpath(src_clean).replace("\\", "/")

            try:
                img_bytes = self._read_file_bytes(internal_img_path)
            except Exception:
                continue

            saved_path = None
            if output_dir:
                ext = os.path.splitext(internal_img_path)[1]
                if not ext:
                    ext = detect_image_ext(img_bytes)
                ch_num = chapter_info.get("index", 1)
                img_filename = f"ch{ch_num:03d}_img{i + 1:03d}{ext}"
                saved_path = os.path.join(output_dir, img_filename)
                with open(saved_path, "wb") as f:
                    f.write(img_bytes)

            extracted.append({
                "index": i + 1,
                "internal_path": internal_img_path,
                "saved_path": saved_path,
                "alt": alt,
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
        html_content = self.extract_chapter_html(chapter_info)
        file_path = chapter_info["file_path"]
        ch_dir = os.path.dirname(file_path)
        title = chapter_info.get("title", "")

        if dump_images_dir:
            os.makedirs(dump_images_dir, exist_ok=True)

        placeholders = {}
        processed_html = html_content

        if BeautifulSoup:
            soup = BeautifulSoup(html_content, "html.parser")
            purge_useless_navigation(soup)
            convert_soup_mathml_to_latex(soup)
            enhance_pre_code_tags(soup)

            imgs = soup.find_all(["img", "image"])
            for i, img in enumerate(imgs):
                src = img.get("src") or img.get("xlink:href")
                alt = img.get("alt", "").strip()
                if not src:
                    continue

                src_clean = unquote(src).split("?")[0].split("#")[0]
                if ch_dir:
                    internal_img_path = os.path.normpath(f"{ch_dir}/{src_clean}").replace("\\", "/")
                else:
                    internal_img_path = os.path.normpath(src_clean).replace("\\", "/")

                try:
                    img_bytes = self._read_file_bytes(internal_img_path)
                except Exception:
                    continue

                saved_path = None
                if dump_images_dir:
                    ext = os.path.splitext(internal_img_path)[1]
                    if not ext:
                        ext = detect_image_ext(img_bytes)
                    ch_num = chapter_info.get("index", 1)
                    img_filename = f"ch{ch_num:03d}_img{i + 1:03d}{ext}"
                    saved_path = os.path.join(dump_images_dir, img_filename)
                    with open(saved_path, "wb") as f:
                        f.write(img_bytes)

                if ocr:
                    ocr_text = ocr_image(img_bytes, engine_name=ocr_engine)
                    ph_key = f"__EPUB_OCR_PLACEHOLDER_{i}__"
                    placeholders[ph_key] = format_ocr_markdown(ocr_text, alt_text=alt or f"插图 {i + 1}", image_rel_path=saved_path)
                    p_tag = soup.new_tag("p")
                    p_tag.string = ph_key
                    img.replace_with(p_tag)
                elif saved_path:
                    if img.name == "image":
                        img["xlink:href"] = saved_path
                    else:
                        img["src"] = saved_path

            processed_html = str(soup)

        if html2text:
            h = html2text.HTML2Text()
            h.body_width = 0
            h.unicode_snob = True
            h.ignore_images = False
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
        print("Usage: python epub_parser.py <path_to_epub> [--probe] [--list]")
        sys.exit(1)

    epub_path = sys.argv[1]
    with EpubBook(epub_path) as book:
        if "--probe" in sys.argv:
            print(json.dumps(book.get_probe_info(), ensure_ascii=False, indent=2))
        elif "--list" in sys.argv:
            for item in book.toc:
                indent = "  " * (item["level"] - 1)
                print(f"[{item['index']:02d}] {indent}{item['title']} -> {item['file_path']}")
        else:
            print(f"EPUB Book: {book.metadata.get('title')}, TOC Chapters: {len(book.toc)}")


if __name__ == "__main__":
    main()
