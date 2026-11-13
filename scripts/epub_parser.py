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
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urldefrag
from typing import List, Dict, Tuple, Optional, Any

try:
    from bs4 import BeautifulSoup, Comment
except ImportError:
    BeautifulSoup = None
    Comment = None

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


def _decode_bytes(raw: bytes, fallback_encoding: str = "utf-8") -> str:
    """结合 BOM 与 XML/HTML 声明编码智能解码文本，兼容 UTF-16 与非 UTF-8 EPUB"""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    head = raw[:512].decode("ascii", errors="ignore")
    m = re.search(r'(?:encoding|charset)\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)', head, re.I)
    enc = m.group(1) if m else fallback_encoding
    try:
        return raw.decode(enc, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def purge_useless_navigation(soup: BeautifulSoup) -> None:
    """清理 DOM 中无意义的导航、运行页眉页脚与顶底跳转链接，同时保留含标题或正文的语义 header/footer"""
    if not soup:
        return
    for tag in soup.find_all(["nav", "header", "footer"]):
        epub_type = str(tag.get("epub:type", "") or "").lower()
        classes = " ".join(tag.get("class", []) or []).lower()
        if tag.name == "nav":
            if "toc" in epub_type or "nav" in classes or "pagination" in classes or "landmarks" in epub_type:
                tag.decompose()
                continue
        if "pagination" in classes or "running" in classes or "page-header" in classes or "page-footer" in classes:
            tag.decompose()
            continue
        if tag.name in ("header", "footer"):
            # 若 header/footer 内部包含章节标题 h1-h3 或较长正文，说明它是语义结构而非页眉页脚
            if tag.find(["h1", "h2", "h3"]) or len(tag.get_text(" ", strip=True)) >= 120:
                continue
            tag.decompose()

    for a in soup.find_all("a"):
        text = a.get_text().strip().lower()
        if text in ("[top]", "[next]", "[previous]", "[contents]", "[back to top]", "top", "contents"):
            a.decompose()
        elif not text and not a.find(["img", "image"]) and not (a.get("id") or a.get("name")):
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


def enhance_pre_code_tags(soup: BeautifulSoup, code_placeholders: Optional[Dict[str, str]] = None) -> None:
    """
    预处理 HTML 中的代码块结构 (<pre>, <code>, <tt>, 以及等宽字体、类名标有 code/program/listing 的元素)，
    借助占位符机制脱离 <pre> 包装，彻底避免 html2text 在围栏代码块前追加 4 空格导致退化为缩进块。
    """
    if not soup:
        return

    def detect_lang(classes: list, text: str) -> str:
        c_str = " ".join(classes).lower()
        tokens = re.split(r"[\s_\-]+", c_str)
        if "cpp" in c_str or "c++" in c_str:
            return "cpp"
        if "golang" in c_str or "go" in tokens:
            return "go"
        if "python" in c_str or "py" in tokens:
            return "python"
        if "java" in tokens:
            return "java"
        if "rust" in tokens or "rs" in tokens:
            return "rust"
        if "javascript" in c_str or "js" in tokens or "typescript" in c_str or "ts" in tokens:
            return "javascript"
        if "bash" in c_str or "sh" in tokens or "shell" in c_str:
            return "bash"
        if "sql" in tokens:
            return "sql"
        if "html" in tokens or "xml" in tokens:
            return "html"
        if "c" in tokens:
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

    def extract_clean_code_text(tag) -> str:
        for br in list(tag.find_all("br")):
            br.replace_with("\n")
        return tag.get_text().strip("\r\n")

    def replace_with_fenced(tag, lang: str, body: str) -> None:
        fenced = f"\n```{lang}\n{body}\n```\n"
        if code_placeholders is not None:
            ph_key = f"EBOOKCODEBLOCKTOKEN{len(code_placeholders)}END"
            code_placeholders[ph_key] = fenced
            p_tag = soup.new_tag("p")
            p_tag.string = ph_key
            tag.replace_with(p_tag)
        else:
            div_tag = soup.new_tag("div")
            div_tag.string = fenced
            tag.replace_with(div_tag)

    # 1. 处理标准的 <pre> 块
    for pre in list(soup.find_all("pre")):
        if not pre.parent:
            continue
        code_tag = pre.find("code")
        classes = list(pre.get("class", []) or [])
        if code_tag and code_tag.get("class"):
            classes = classes + list(code_tag.get("class", []) or [])
        code_text = extract_clean_code_text(pre)
        if not code_text.strip():
            continue
        lang = detect_lang(classes, code_text)
        replace_with_fenced(pre, lang, code_text)

    # 2. 处理 class 名带 code / program / listing / monospace 或含有 monospace 样式的 div / p
    code_selectors = ["div", "p"]
    for tag in list(soup.find_all(code_selectors)):
        if not tag.parent or tag.find_parent("pre"):
            continue
        raw_txt = tag.get_text()
        if "EBOOKCODEBLOCKTOKEN" in raw_txt or tag.find(["h1", "h2", "h3", "h4", "table"]):
            continue
        classes = list(tag.get("class", []) or [])
        c_str = " ".join(classes).lower()
        style = (tag.get("style") or "").lower()

        is_code_container = bool(
            re.search(r"(?:^|[^a-z])(code|codeblock|program|listing|syntax|snippet|monospace|sourcecode)", c_str)
        ) or any(kw in style for kw in ["monospace", "courier", "consolas", "menlo"])

        if is_code_container:
            code_text = extract_clean_code_text(tag)
            if code_text.strip() and (len(code_text.splitlines()) >= 2 or any(kw in code_text for kw in [";", "{", "}", "()", "def ", "func ", "int ", "#include"])):
                lang = detect_lang(classes, code_text)
                replace_with_fenced(tag, lang, code_text)

    # 3. 处理 <font face="monospace|Courier|Consolas"> 或多行 <tt>
    for font_or_tt in list(soup.find_all(["font", "tt"])):
        if not font_or_tt.parent or font_or_tt.find_parent("pre"):
            continue
        raw_txt = font_or_tt.get_text()
        if "EBOOKCODEBLOCKTOKEN" in raw_txt:
            continue
        face = (font_or_tt.get("face") or "").lower()
        is_mono = font_or_tt.name == "tt" or any(kw in face for kw in ["monospace", "courier", "consolas"])
        if is_mono:
            text = extract_clean_code_text(font_or_tt)
            if text.strip() and (len(text.splitlines()) >= 2 or len(text.strip()) > 80):
                lang = detect_lang([], text)
                replace_with_fenced(font_or_tt, lang, text)


class EpubBook:
    """EPUB 电子书解析器"""

    def __init__(self, epub_path: str):
        self.epub_path = os.path.abspath(epub_path)
        if not os.path.exists(self.epub_path):
            raise FileNotFoundError(f"EPUB file not found: {self.epub_path}")

        self.zip_file = zipfile.ZipFile(self.epub_path, "r")
        self._namelist_lower = {name.lower(): name for name in self.zip_file.namelist()}
        self.opf_dir = ""
        self.is_drm_protected, self.drm_reason, self.drm_encrypted_uris = self._detect_drm()
        self.opf_path = self._find_opf_path()
        self.opf_dir = posixpath.dirname(self.opf_path.replace("\\", "/"))
        self.metadata, self.manifest, self.spine = self._parse_opf()
        self.toc = self._parse_toc()

    def close(self):
        if self.zip_file:
            self.zip_file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _detect_drm(self) -> Tuple[bool, str, set]:
        """检测 EPUB 是否受 DRM 加密保护（排除仅使用字体混淆 IDPF/Adobe font obfuscation 的合法文件）"""
        encrypted_uris = set()
        for drm_marker in ("meta-inf/rights.xml",):
            if drm_marker in self._namelist_lower:
                return True, f"DRM rights file detected ({drm_marker})", encrypted_uris
        if any(n.endswith(".lcpl") for n in self._namelist_lower):
            return True, "Readium LCP DRM license detected", encrypted_uris

        enc_path = self._namelist_lower.get("meta-inf/encryption.xml")
        if not enc_path:
            return False, "", encrypted_uris

        try:
            enc_xml = _decode_bytes(self.zip_file.read(enc_path))
            root = ET.fromstring(enc_xml)
        except Exception:
            return True, "Unreadable META-INF/encryption.xml", encrypted_uris

        drm_detected = False
        reason = ""
        font_exts = (".ttf", ".otf", ".woff", ".woff2", ".eot")
        for elem in root.iter():
            if _strip_ns(elem.tag) != "EncryptedData":
                continue
            alg = ""
            uri = ""
            for sub in elem.iter():
                sname = _strip_ns(sub.tag)
                if sname == "EncryptionMethod":
                    alg = (sub.get("Algorithm") or "").strip()
                elif sname == "CipherReference":
                    uri = unquote(sub.get("URI") or "").strip().replace("\\", "/").lstrip("/")

            alg_lower = alg.lower()
            is_font_obfuscation = (
                "2008/embedding" in alg_lower
                or "enc#embedded" in alg_lower
                or (uri.lower().endswith(font_exts) and not alg_lower.endswith("aes128-cbc"))
            )
            if not is_font_obfuscation:
                drm_detected = True
                if uri:
                    encrypted_uris.add(posixpath.normpath(uri).lower())
                if not reason:
                    reason = alg or "Encrypted content in META-INF/encryption.xml"

        return drm_detected, reason, encrypted_uris

    def _read_file_text(self, internal_path: str, encoding: str = "utf-8") -> str:
        """从 zip 中读取并智能解码文本文件内容"""
        raw = self._read_file_bytes(internal_path)
        return _decode_bytes(raw, fallback_encoding=encoding)

    def _read_file_bytes(self, internal_path: str) -> bytes:
        """从 zip 中读取二进制内容（O(1) 路径标准化 + OPF 目录兜底）"""
        clean = unquote(internal_path).replace("\\", "/").lstrip("/")
        norm_path = posixpath.normpath(clean)
        candidates = [norm_path]
        if self.opf_dir and not norm_path.startswith(self.opf_dir + "/"):
            candidates.append(posixpath.normpath(f"{self.opf_dir}/{norm_path}"))
        for cand in candidates:
            try:
                return self.zip_file.read(cand)
            except KeyError:
                actual = self._namelist_lower.get(cand.lower())
                if actual:
                    return self.zip_file.read(actual)
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
                txt = lbl.find("{http://www.daisy.org/z3986/2005/ncx/}text")
                if txt is None:
                    txt = lbl.find("text")
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
            "is_digital": not self.is_drm_protected,
            "is_drm": self.is_drm_protected,
            "drm_info": self.drm_reason if self.is_drm_protected else "",
            "title": self.metadata.get("title", ""),
            "author": self.metadata.get("author", ""),
            "publisher": self.metadata.get("publisher", ""),
            "language": self.metadata.get("language", ""),
            "total_spine_items": len(self.spine),
            "has_toc": len(self.toc) > 0,
            "toc_count": len(self.toc),
            "category": "DRM-Protected EPUB" if self.is_drm_protected else "E (Reflowable EPUB)",
            "recommendation": (
                f"DRM detected ({self.drm_reason}); remove DRM before extraction."
                if self.is_drm_protected
                else "Pure Python zip+xhtml parse -> Native HTML to Markdown (0.05s, 0 Token, 100% precision)"
            ),
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

    def _ensure_not_drm_locked(self, file_path: str) -> None:
        if not self.is_drm_protected:
            return
        norm_fp = posixpath.normpath(file_path.replace("\\", "/").lstrip("/")).lower()
        if not self.drm_encrypted_uris or norm_fp in self.drm_encrypted_uris:
            raise RuntimeError(f"EPUB content '{file_path}' is protected by DRM ({self.drm_reason}) and cannot be extracted.")

    def _resolve_internal_path(self, base_file: str, rel_src: str) -> str:
        clean = unquote(rel_src).split("?")[0].split("#")[0].replace("\\", "/")
        if clean.startswith("/"):
            return posixpath.normpath(clean.lstrip("/"))
        base_dir = posixpath.dirname(base_file.replace("\\", "/"))
        if base_dir:
            return posixpath.normpath(f"{base_dir}/{clean}")
        return posixpath.normpath(clean)

    def _get_chapter_segments(self, chapter_info: Dict[str, Any]) -> List[Tuple[str, Optional[str], Optional[str]]]:
        """计算目标章节跨越的 (file_path, start_anchor, stop_anchor) 片段列表"""
        start_file = chapter_info["file_path"]
        start_anchor = chapter_info.get("anchor")
        cur_level = chapter_info.get("level", 1)

        cur_idx = None
        ch_index = chapter_info.get("index")
        if ch_index is not None:
            for idx, item in enumerate(self.toc):
                if item.get("index") == ch_index:
                    cur_idx = idx
                    break
        if cur_idx is None:
            for idx, item in enumerate(self.toc):
                if (
                    item.get("file_path") == start_file
                    and item.get("anchor") == start_anchor
                    and item.get("title") == chapter_info.get("title")
                ):
                    cur_idx = idx
                    break

        stop_entry = None
        if cur_idx is not None:
            for nxt in self.toc[cur_idx + 1:]:
                if nxt.get("file_path") == start_file and nxt.get("anchor") == start_anchor:
                    continue
                if nxt.get("level", 1) <= cur_level:
                    stop_entry = nxt
                    break

        if stop_entry and stop_entry["file_path"] == start_file:
            if stop_entry.get("anchor") and stop_entry.get("anchor") != start_anchor:
                return [(start_file, start_anchor, stop_entry.get("anchor"))]
            return [(start_file, start_anchor, None)]

        spine_paths = [
            self.manifest[idref]["full_path"]
            for idref in self.spine
            if idref in self.manifest and self.manifest[idref].get("full_path")
        ]
        s_idx = None
        for idx, sp in enumerate(spine_paths):
            if sp.lower() == start_file.lower():
                s_idx = idx
                break

        if s_idx is None:
            return [(start_file, start_anchor, None)]

        if stop_entry is None:
            segments = [(start_file, start_anchor, None)]
            segments.extend((sp, None, None) for sp in spine_paths[s_idx + 1:])
            return segments

        stop_file = stop_entry["file_path"]
        stop_anchor = stop_entry.get("anchor")
        e_idx = None
        for idx in range(s_idx + 1, len(spine_paths)):
            if spine_paths[idx].lower() == stop_file.lower():
                e_idx = idx
                break

        if e_idx is None:
            return [(start_file, start_anchor, None)]

        segments = [(start_file, start_anchor, None)]
        segments.extend((sp, None, None) for sp in spine_paths[s_idx + 1 : e_idx])
        if stop_anchor:
            segments.append((stop_file, None, stop_anchor))
        return segments

    def _slice_html_by_anchors(self, full_html: str, start_anchor: Optional[str], stop_anchor: Optional[str]) -> str:
        """在同一 HTML 文档中精准截取 [start_anchor, stop_anchor) 之间的正文片段"""
        if not start_anchor and not stop_anchor:
            return full_html
        if not BeautifulSoup or Comment is None:
            return full_html

        soup = BeautifulSoup(full_html, "html.parser")
        body = soup.find("body") or soup

        def find_el(anchor: Optional[str]):
            if not anchor:
                return None
            return body.find(id=anchor) or body.find(attrs={"name": anchor})

        def lift_target(el):
            cur = el
            while (
                cur is not None
                and cur.parent is not None
                and cur.name in ("a", "span")
                and not cur.get_text(strip=True)
                and cur.parent.name in ("h1", "h2", "h3", "h4", "h5", "h6")
            ):
                cur = cur.parent
            return cur

        start_el = find_el(start_anchor)
        stop_el = find_el(stop_anchor)

        if start_anchor and start_el is None:
            return full_html

        if stop_el is None and start_el is not None and start_el.name in ("section", "article", "chapter") and len(start_el.get_text(strip=True)) >= 40:
            return str(start_el)

        start_marker = "__EBOOK_SLICE_START_MARKER__"
        stop_marker = "__EBOOK_SLICE_STOP_MARKER__"

        if start_el is not None:
            lift_target(start_el).insert_before(Comment(start_marker))
        if stop_el is not None:
            lift_target(stop_el).insert_before(Comment(stop_marker))

        raw_body = body.decode_contents() if hasattr(body, "decode_contents") else str(body)
        start_token = f"<!--{start_marker}-->"
        stop_token = f"<!--{stop_marker}-->"

        s_pos = 0
        if start_el is not None:
            found_s = raw_body.find(start_token)
            if found_s == -1:
                return full_html
            s_pos = found_s + len(start_token)

        e_pos = len(raw_body)
        if stop_el is not None:
            found_e = raw_body.find(stop_token)
            if found_e != -1:
                if found_e < s_pos:
                    return full_html
                e_pos = found_e

        fragment = raw_body[s_pos:e_pos].strip()
        if not fragment:
            return ""
        frag_soup = BeautifulSoup(fragment, "html.parser")
        if not frag_soup.get_text(strip=True) and not frag_soup.find(["img", "image"]):
            return ""
        return fragment

    def _get_chapter_html_fragments(self, chapter_info: Dict[str, Any]) -> List[Tuple[str, str]]:
        """获取目标章节拆分后的 (file_path, html_fragment) 列表"""
        segments = self._get_chapter_segments(chapter_info)
        fragments = []
        for file_path, s_anchor, e_anchor in segments:
            self._ensure_not_drm_locked(file_path)
            full_html = self._read_file_text(file_path)
            sliced = self._slice_html_by_anchors(full_html, s_anchor, e_anchor)
            if sliced and sliced.strip():
                fragments.append((file_path, sliced))
        return fragments

    def extract_chapter_html(self, chapter_info: Dict[str, Any]) -> str:
        """提取目标章节的原始 HTML 片段（支持单文件锚点精切与跨 Spine 文件合并）"""
        fragments = self._get_chapter_html_fragments(chapter_info)
        if not fragments:
            file_path = chapter_info["file_path"]
            self._ensure_not_drm_locked(file_path)
            return self._read_file_text(file_path)
        return "\n".join(frag for _, frag in fragments)

    def extract_chapter_images(self, chapter_info: Dict[str, Any], output_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        """提取目标章节关联的所有图片（按真实片段路径解析相对地址）"""
        fragments = self._get_chapter_html_fragments(chapter_info)
        if not fragments:
            fragments = [(chapter_info["file_path"], self.extract_chapter_html(chapter_info))]

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        extracted = []
        img_counter = 0
        ch_num = chapter_info.get("index", 1)

        for frag_file, frag_html in fragments:
            if BeautifulSoup:
                soup = BeautifulSoup(frag_html, "html.parser")
                imgs = soup.find_all(["img", "image"])
            else:
                imgs = []
                for m in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', frag_html, re.I):
                    imgs.append({"src": m.group(1), "alt": ""})

            for img_tag in imgs:
                if hasattr(img_tag, "get"):
                    src = img_tag.get("src") or img_tag.get("xlink:href")
                    alt = img_tag.get("alt", "")
                else:
                    src = img_tag["src"]
                    alt = img_tag.get("alt", "")

                if not src:
                    continue

                internal_img_path = self._resolve_internal_path(frag_file, src)
                try:
                    img_bytes = self._read_file_bytes(internal_img_path)
                except Exception:
                    continue

                img_counter += 1
                saved_path = None
                if output_dir:
                    ext = posixpath.splitext(internal_img_path)[1]
                    if not ext:
                        ext = detect_image_ext(img_bytes)
                    img_filename = f"ch{ch_num:03d}_img{img_counter:03d}{ext}"
                    saved_path = os.path.join(output_dir, img_filename)
                    with open(saved_path, "wb") as f:
                        f.write(img_bytes)

                extracted.append({
                    "index": img_counter,
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
        """提取目标章节并转换为 Markdown (支持跨文件拼装、围栏代码保真、插图导出与 OCR 智能回填)"""
        fragments = self._get_chapter_html_fragments(chapter_info)
        if not fragments:
            fragments = [(chapter_info["file_path"], self.extract_chapter_html(chapter_info))]

        title = chapter_info.get("title", "")

        if dump_images_dir:
            os.makedirs(dump_images_dir, exist_ok=True)

        placeholders: Dict[str, str] = {}
        code_placeholders: Dict[str, str] = {}
        pending_ocr_tasks = []
        processed_html_parts = []
        img_counter = 0
        ch_num = chapter_info.get("index", 1)

        if BeautifulSoup:
            for frag_file, frag_html in fragments:
                soup = BeautifulSoup(frag_html, "html.parser")
                purge_useless_navigation(soup)
                convert_soup_mathml_to_latex(soup)
                enhance_pre_code_tags(soup, code_placeholders=code_placeholders)

                imgs = soup.find_all(["img", "image"])
                for img in imgs:
                    src = img.get("src") or img.get("xlink:href")
                    alt = img.get("alt", "").strip()
                    if not src:
                        continue

                    internal_img_path = self._resolve_internal_path(frag_file, src)
                    try:
                        img_bytes = self._read_file_bytes(internal_img_path)
                    except Exception:
                        continue

                    img_counter += 1
                    saved_path = None
                    if dump_images_dir:
                        ext = posixpath.splitext(internal_img_path)[1]
                        if not ext:
                            ext = detect_image_ext(img_bytes)
                        img_filename = f"ch{ch_num:03d}_img{img_counter:03d}{ext}"
                        saved_path = os.path.join(dump_images_dir, img_filename)
                        with open(saved_path, "wb") as f:
                            f.write(img_bytes)

                    if ocr:
                        ph_key = f"EPUBOCRPLACEHOLDERTOKEN{len(pending_ocr_tasks)}END"
                        pending_ocr_tasks.append((ph_key, alt or f"插图 {img_counter}", saved_path, img_bytes))
                        p_tag = soup.new_tag("p")
                        p_tag.string = ph_key
                        img.replace_with(p_tag)
                    elif saved_path:
                        if img.name == "image":
                            img["xlink:href"] = saved_path
                        else:
                            img["src"] = saved_path

                processed_html_parts.append(str(soup))

            if pending_ocr_tasks:
                images_to_ocr = [task[3] for task in pending_ocr_tasks]
                ocr_texts = ocr_images_batch(images_to_ocr, engine_name=ocr_engine)
                for (ph_key, alt_val, saved_path, _), ocr_text in zip(pending_ocr_tasks, ocr_texts):
                    placeholders[ph_key] = format_ocr_markdown(ocr_text, alt_text=alt_val, image_rel_path=saved_path)

            processed_html = "\n".join(processed_html_parts)
        else:
            processed_html = "\n".join(frag for _, frag in fragments)

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
        print("Usage: python epub_parser.py <path_to_epub> [--probe] [--list]")
        sys.exit(1)

    epub_path = sys.argv[1]
    with EpubBook(epub_path) as book:
        if "--probe" in sys.argv:
            print(json.dumps(book.get_probe_info(), ensure_ascii=True, indent=2))
        elif "--list" in sys.argv:
            for item in book.toc:
                indent = "  " * (item["level"] - 1)
                print(f"[{item['index']:02d}] {indent}{item['title']} -> {item['file_path']}")
        else:
            print(f"EPUB Book: {book.metadata.get('title')}, TOC Chapters: {len(book.toc)}")


if __name__ == "__main__":
    main()
