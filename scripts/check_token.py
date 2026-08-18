#!/usr/bin/env python3
"""
check_token.py - API Token 配置检测与管理工具 (MinerU & PaddleOCR)
检测当前环境是否已配置 MinerU VLM API 与 百度飞桨 PaddleOCR API Token，
并给出清晰的状态提示与持久化配置指引。

Token 探测优先级：
  1. 命令行参数（--token / --paddle-token）
  2. 环境变量（MINERU_TOKEN / PADDLEOCR_TOKEN / PADDLE_OCR_TOKEN）
  3. 配置文件（~/.mineru/config.yaml / ~/.paddleocr/config.yaml）
"""

import os
import sys
import re
import json
import argparse
from pathlib import Path


def detect_mineru_token(token_arg: str | None = None) -> dict:
    """按优先级检测已配置的 MinerU Token（不暴露明文）"""
    source = None
    configured = False

    if token_arg and token_arg.strip():
        configured = True
        source = "cli"
    elif os.environ.get("MINERU_TOKEN"):
        configured = True
        source = "env"
    else:
        cfg = Path.home() / ".mineru" / "config.yaml"
        if cfg.exists():
            try:
                text = cfg.read_text(encoding="utf-8")
                if "token:" in text and "your_token_here" not in text:
                    configured = True
                    source = "config"
            except Exception:
                pass

    return {"service": "mineru", "configured": configured, "source": source}


def detect_paddle_token(token_arg: str | None = None) -> dict:
    """按优先级检测已配置的 PaddleOCR Token（不暴露明文）"""
    source = None
    configured = False

    if token_arg and token_arg.strip():
        configured = True
        source = "cli"
    elif (
        os.environ.get("PADDLEOCR_TOKEN")
        or os.environ.get("PADDLE_OCR_TOKEN")
        or os.environ.get("BAIDU_OCR_TOKEN")
    ):
        configured = True
        source = "env"
    else:
        cfg_paths = [
            Path.home() / ".paddleocr" / "config.yaml",
            Path.home() / ".paddle" / "config.yaml",
            Path.home() / ".config" / "paddleocr" / "config.yaml",
        ]
        for cp in cfg_paths:
            if cp.exists():
                try:
                    text = cp.read_text(encoding="utf-8")
                    m = re.search(r"token\s*:\s*([^\s#]+)", text, re.IGNORECASE)
                    if m and m.group(1).strip() and m.group(1).strip() != "your_token_here":
                        configured = True
                        source = "config"
                        break
                except Exception:
                    pass

    return {"service": "paddleocr", "configured": configured, "source": source}


def main():
    parser = argparse.ArgumentParser(description="Check MinerU & PaddleOCR API token configuration.")
    parser.add_argument("--token", "--mineru-token", help="MinerU API token (overrides env and config)")
    parser.add_argument("--paddle-token", help="PaddleOCR API token (overrides env and config)")
    parser.add_argument("--mineru", action="store_true", help="Check MinerU token only")
    parser.add_argument("--paddle", action="store_true", help="Check PaddleOCR token only")
    parser.add_argument("--json", action="store_true", help="Output raw JSON format")
    args = parser.parse_args()

    mineru_info = detect_mineru_token(args.token)
    paddle_info = detect_paddle_token(args.paddle_token)

    if args.json:
        if args.mineru and not args.paddle:
            print(json.dumps(mineru_info, ensure_ascii=False, indent=2))
        elif args.paddle and not args.mineru:
            print(json.dumps(paddle_info, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"mineru": mineru_info, "paddleocr": paddle_info}, ensure_ascii=False, indent=2))
        return

    # 人性化控制台输出
    print("=" * 60)
    print(" 云端高精解析 API Token 检测报告")
    print("=" * 60)

    # 1. PaddleOCR 检测
    if not args.mineru or args.paddle:
        if paddle_info["configured"]:
            print(f"✅ 百度飞桨 PaddleOCR Token: 已配置 (来源: {paddle_info['source']})")
            print("   -> 高精度代码与多模态版面解析 API 可用 (extract --ocr --ocr-engine paddleocr)")
        else:
            print("⚠️ 百度飞桨 PaddleOCR Token: 未配置 (将自动降级使用本地离线 RapidOCR 兜底)")
            print("   配置指引：")
            print("     * 申请地址: https://paddleocr.aistudio-app.com/")
            print("     * 环境变量: $env:PADDLEOCR_TOKEN=\"your_token_here\"")
            print("     * 配置文件: ~/.paddleocr/config.yaml 写入 `token: your_token_here`")
        print("-" * 60)

    # 2. MinerU 检测
    if not args.paddle or args.mineru:
        if mineru_info["configured"]:
            print(f"✅ MinerU VLM Token: 已配置 (来源: {mineru_info['source']})")
            print("   -> 复杂多栏/公式 PDF 高精 VLM 解析可用 (extract --model vlm)")
        else:
            print("ℹ️ MinerU VLM Token: 未配置 (PDF 将使用本地纯文本/双通道极速直读)")
            print("   配置指引：")
            print("     * 免费申请: https://mineru.net/apiManage/token")
            print("     * 环境变量: $env:MINERU_TOKEN=\"your_token_here\"")
            print("     * 认证命令: \"your_token_here\" | mineru-open-api auth")
        print("=" * 60)


if __name__ == "__main__":
    main()
