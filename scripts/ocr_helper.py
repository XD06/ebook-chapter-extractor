#!/usr/bin/env python3
"""
ocr_helper.py - 多后端轻量 OCR 工具助手、代码排版自愈与插图处理模块
支持优先级探测：
1. rapidocr_onnxruntime (推荐，高精度、轻量、跨平台)
2. pytesseract (若系统已安装 tesseract 二进制)
3. easyocr
4. Windows Native WinRT OCR (Windows 10/11 平台 0 外部包依赖兜底)
"""

import os
import sys
import re
import tempfile
import subprocess
from typing import Optional, Union, Dict, Any, List


def is_windows() -> bool:
    return sys.platform.startswith("win")


def get_cache_dir(file_path: str, sub_name: str = "") -> str:
    """获取缓存目录，源目录只读或无权限时自动优雅降级到系统临时目录"""
    base_dir = os.path.dirname(os.path.abspath(file_path))
    target_dir = os.path.join(base_dir, ".cache")
    if sub_name:
        target_dir = os.path.join(target_dir, sub_name)
    try:
        os.makedirs(target_dir, exist_ok=True)
        test_file = os.path.join(target_dir, ".perm_test")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        return target_dir
    except (PermissionError, OSError):
        fallback = os.path.join(tempfile.gettempdir(), ".ebook_cache")
        if sub_name:
            fallback = os.path.join(fallback, sub_name)
        os.makedirs(fallback, exist_ok=True)
        return fallback


def detect_image_ext(data: bytes) -> str:
    """根据文件头 Magic Number 检测图片真实后缀"""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return ".tif"
    if data.startswith(b"<svg") or b"<svg" in data[:100]:
        return ".svg"
    return ".png"


def detect_ocr_engine() -> str:
    """探测当前环境下可用的 OCR 引擎"""
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return "rapidocr"
    except ImportError:
        pass

    if is_windows():
        return "win_ocr"

    try:
        import pytesseract  # noqa: F401
        return "pytesseract"
    except ImportError:
        pass

    try:
        import easyocr  # noqa: F401
        return "easyocr"
    except ImportError:
        pass

    return "none"


_RAPIDOCR_INSTANCE = None


def get_rapidocr():
    global _RAPIDOCR_INSTANCE
    if _RAPIDOCR_INSTANCE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPIDOCR_INSTANCE = RapidOCR()
        except Exception:
            _RAPIDOCR_INSTANCE = False
    return _RAPIDOCR_INSTANCE if _RAPIDOCR_INSTANCE is not False else None


def ocr_image_rapidocr(image_input: Union[str, bytes]) -> str:
    engine = get_rapidocr()
    if not engine:
        return ""
    try:
        result, _ = engine(image_input)
        if not result:
            return ""
        lines = [item[1] for item in result if item and len(item) >= 2 and item[1].strip()]
        return "\n".join(lines)
    except Exception as e:
        sys.stderr.write(f"RapidOCR recognition error: {e}\n")
        return ""


def ocr_image_winrt(image_path: str) -> str:
    """Windows 原生 WinRT OCR (PowerShell 兜底调用)"""
    ps_script = f"""
[Windows.Globalization.Language,Windows.Foundation.UniversalApiContract,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation.UniversalApiContract,ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation.UniversalApiContract,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Foundation.UniversalApiContract,ContentType=WindowsRuntime] | Out-Null

$filePath = '{image_path.replace("'", "''")}'
$file = [Windows.Storage.StorageFile]::GetFileFromPathAsync($filePath).GetAwaiter().GetResult()
$stream = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read).GetAwaiter().GetResult()
$decoder = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream).GetAwaiter().GetResult()
$bitmap = $decoder.GetSoftwareBitmapAsync().GetAwaiter().GetResult()

$lang = New-Object Windows.Globalization.Language("zh-Hans-CN")
if (-not [Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) {{
    $ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}} else {{
    $ocr = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
}}

$ocrResult = $ocr.RecognizeAsync($bitmap).GetAwaiter().GetResult()
$ocrResult.Text
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="ignore"
        )
        return proc.stdout.strip()
    except Exception as e:
        sys.stderr.write(f"WinRT OCR error: {e}\n")
        return ""


def ocr_image_pytesseract(image_input: Union[str, bytes]) -> str:
    try:
        import pytesseract
        from PIL import Image
        import io
        if isinstance(image_input, bytes):
            img = Image.open(io.BytesIO(image_input))
        else:
            img = Image.open(image_input)
        return pytesseract.image_to_string(img, lang="chi_sim+eng").strip()
    except Exception as e:
        sys.stderr.write(f"Pytesseract error: {e}\n")
        return ""


def ocr_image(image_input: Union[str, bytes], engine_name: Optional[str] = None) -> str:
    """对单张图片执行 OCR 识别"""
    if engine_name is None:
        engine_name = detect_ocr_engine()

    if engine_name == "rapidocr":
        res = ocr_image_rapidocr(image_input)
        if res:
            return res
        if is_windows():
            engine_name = "win_ocr"

    if engine_name == "win_ocr":
        tmp_path = None
        if isinstance(image_input, bytes):
            ext = detect_image_ext(image_input)
            fd, tmp_path = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, "wb") as f:
                f.write(image_input)
            target_path = tmp_path
        else:
            target_path = image_input

        try:
            return ocr_image_winrt(target_path)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    if engine_name == "pytesseract":
        return ocr_image_pytesseract(image_input)

    return ""


def clean_ocr_text(text: str) -> str:
    """
    通用 OCR 文本清洗与规范化：
    1. 清除控制字符、零宽空格、不可见 Unicode 乱码
    2. 规范化代码上下文中的全角标点符号 (（）［］｛｝ 等)
    3. 修复被 OCR 拆开的注释符号 (/ / -> //, / * -> /*) 与赋值符 (: = -> :=)
    """
    if not text:
        return ""

    # 清除控制字符 (保留 \n, \r, \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff\u200b\ufffd]", "", text)

    lines = text.splitlines()
    cleaned = []
    for line in lines:
        l = line
        # 修复拆分的注释与运算符
        l = re.sub(r'(^|\s)/[ \t]+/(?=\s|$|[a-zA-Z\u4e00-\u9fa5])', r'\1//', l)
        l = re.sub(r'(^|\s)/[ \t]+\*', r'\1/*', l)
        l = re.sub(r'\*[ \t]+/(?=\s|$)', r'*/', l)
        l = re.sub(r'(\w|\s)[:：][ \t]*=(?=\s|\w)', r'\1:=', l)

        # 在代码/表达式特征明显的行中，规范化全角标点
        is_code_like = bool(re.search(r"(:=|\b(func|var|import|package|int|float|string|return|if|for|switch|class|def|struct)\b|[;{}()=<>+\-*/&|])", l))
        if is_code_like:
            l = l.replace("（", "(").replace("）", ")")
            l = l.replace("［", "[").replace("］", "]")
            l = l.replace("｛", "{").replace("｝", "}")
            l = l.replace("“", '"').replace("”", '"')
            l = l.replace("‘", "'").replace("’", "'")

        cleaned.append(l)

    return "\n".join(cleaned)


def clean_code_ocr(text: str) -> str:
    """
    针对 OCR 提取的代码做轻量启发式自愈与清洗：
    1. 修正尖括号与问号: <iostream? -> <iostream>
    2. 修正末尾分号与冒号误认: std: -> std; / 0 : -> 0; / endl : -> endl;
    3. 清洗空行与异常断行
    4. 修复常见关键字粘连与注释符号
    """
    text = clean_ocr_text(text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    cleaned_lines = []

    for line in lines:
        line = re.sub(r'#include\s*<([^>]+)[\?？]', r'#include <\1>', line)
        line = re.sub(r'#include\s*<([^>]+)$', r'#include <\1>', line)
        line = re.sub(r'(using\s+namespace\s+std)\s*[:：]', r'\1;', line)
        line = re.sub(r'(return\s+\d+)\s*[:：]', r'\1;', line)
        line = re.sub(r'(endl)\s*[:：]', r'\1;', line)
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def format_ocr_markdown(ocr_text: str, alt_text: str = "", image_rel_path: Optional[str] = None) -> str:
    """
    将 OCR 识别出的文本格式化为适合放入 Markdown 的块
    如果是代码，使用代码块包装；如果是普通文字，使用引用块包装。
    """
    if not ocr_text or not ocr_text.strip():
        if image_rel_path:
            return f"\n![{alt_text}]({image_rel_path})\n"
        return ""

    text = clean_ocr_text(ocr_text.strip())

    code_keywords = [
        "#include", "int main", "void ", "std::", "cout", "cin", "def ", "class ",
        "return ", "import ", "public class", "fn ", "let ", "const ", "var ",
        "struct ", "typedef", "printf(", "scanf(", "func ", "package ", ":="
    ]
    is_code = any(kw in text for kw in code_keywords) or (
        ("{" in text and "}" in text) or (";" in text and len(text.splitlines()) >= 3)
    )

    header = f"🖼️ **[插图: {alt_text}]**" if alt_text else "🖼️ **[插图]**"
    if image_rel_path:
        header += f" *(高清原图路径: `{image_rel_path}`)*"

    if is_code:
        text = clean_code_ocr(text)
        lang = ""
        if any(k in text for k in ["#include", "cout", "std::"]):
            lang = "cpp"
        elif any(k in text for k in ["func ", "package ", "fmt.", "os.Open", "flag."]):
            lang = "go"
        elif any(k in text for k in ["def ", "import ", "print(", "__init__"]):
            lang = "python"
        elif any(k in text for k in ["printf(", "scanf(", "int main", "char *"]):
            lang = "c"
        return f"\n{header}\n```{lang}\n{text}\n```\n"
    else:
        quoted = "\n> ".join(text.splitlines())
        return f"\n{header}\n> {quoted}\n"
