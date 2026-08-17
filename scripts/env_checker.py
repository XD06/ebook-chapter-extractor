#!/usr/bin/env python3
"""
env_checker.py - 依赖自动检测与自愈安装工具
为 AI Agent 与终端用户提供开箱即用、零配置的电子书提取环境。
支持分级依赖管理（Tier 1: 核心解析 / Tier 2: 本地 OCR）。
"""

import sys
import os
import subprocess
import importlib

# 依赖分级定义
DEPENDENCY_TIERS = {
    "core": [
        ("pymupdf", "pymupdf"),            # PDF / 流式解析支撑
        ("bs4", "beautifulsoup4"),        # EPUB / MOBI DOM 解析
        ("html2text", "html2text"),       # 高保真 Markdown 转换
    ],
    "ocr": [
        ("rapidocr_onnxruntime", "rapidocr_onnxruntime"),  # 轻量高精度 OCR 引擎
    ]
}

PIP_INDEX_URLS = [
    "",  # 默认 PyPI 源
    "https://pypi.tuna.tsinghua.edu.cn/simple",  # 清华源加速
    "https://mirrors.aliyun.com/pypi/simple/",   # 阿里源加速
]


def is_installed(module_name: str) -> bool:
    """检测指定 Python 模块是否已安装"""
    try:
        importlib.import_module(module_name)
        return True
    except ImportError:
        return False


def install_package(pip_name: str) -> bool:
    """静默安装 Python 包，支持自动国内镜像源加速重试"""
    print(f"[*] 首次运行自动配置: 正在安装必要依赖 `{pip_name}`...", file=sys.stderr)

    # 首先尝试默认 pip 源
    cmd = [sys.executable, "-m", "pip", "install", pip_name, "--quiet"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print(f"[+] 依赖 `{pip_name}` 安装成功！", file=sys.stderr)
        return True

    # 若失败，尝试镜像源加速
    for mirror in PIP_INDEX_URLS[1:]:
        cmd_mirror = [sys.executable, "-m", "pip", "install", pip_name, "-i", mirror, "--quiet"]
        res_mirror = subprocess.run(cmd_mirror, capture_output=True, text=True)
        if res_mirror.returncode == 0:
            print(f"[+] 依赖 `{pip_name}` 通过镜像源安装成功！", file=sys.stderr)
            return True

    print(f"[!] 警告: 自动安装 `{pip_name}` 失败，详细日志: {res.stderr.strip()}", file=sys.stderr)
    return False


def ensure_core_dependencies() -> bool:
    """确保核心电子书解析依赖已安装 (Tier 1: pymupdf, beautifulsoup4, html2text)"""
    missing = []
    for mod_name, pip_name in DEPENDENCY_TIERS["core"]:
        if not is_installed(mod_name):
            missing.append(pip_name)

    if not missing:
        return True

    print(f"[*] 检测到缺少必要核心依赖: {', '.join(missing)}，正在自动安装...", file=sys.stderr)
    all_ok = True
    for pkg in missing:
        if not install_package(pkg):
            all_ok = False
    return all_ok


def ensure_ocr_dependencies() -> bool:
    """确保 OCR 回填依赖已安装 (Tier 2: rapidocr_onnxruntime)"""
    missing = []
    for mod_name, pip_name in DEPENDENCY_TIERS["ocr"]:
        if not is_installed(mod_name):
            missing.append(pip_name)

    if not missing:
        return True

    print(f"[*] 检测到已启用 OCR 功能，正在自动安装轻量 OCR 引擎: {', '.join(missing)}...", file=sys.stderr)
    all_ok = True
    for pkg in missing:
        if not install_package(pkg):
            all_ok = False
    return all_ok


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Auto check and install dependencies for eBook extractor.")
    parser.add_argument("--tier", choices=["core", "ocr", "all"], default="core", help="Dependency tier to check/install")
    args = parser.parse_args()

    if args.tier in ("core", "all"):
        ensure_core_dependencies()
    if args.tier in ("ocr", "all"):
        ensure_ocr_dependencies()
