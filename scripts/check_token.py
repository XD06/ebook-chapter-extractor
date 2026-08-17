#!/usr/bin/env python3
"""
check_token.py - MinerU Token 配置检测工具
检测当前环境是否已配置 MinerU API Token，并给出清晰的状态提示与配置指引。

优先级（与 skill 文档一致）：
  1. --token <token> 命令行参数（最高优先，临时）
  2. MINERU_TOKEN 环境变量
  3. ~/.mineru/config.yaml 配置文件（持久化）

用法：
  python check_token.py                 # 检测并打印状态
  python check_token.py --json          # 输出 JSON（供脚本判断）
"""

import os
import sys
import json
import argparse
from pathlib import Path


def detect_token(token_arg: str | None = None) -> dict:
    """按优先级检测已配置的 Token（不暴露明文，仅返回是否存在/来源）。"""
    source = None
    configured = False

    # 1. 命令行参数（临时，最高优先）
    if token_arg:
        configured = True
        source = "cli"

    # 2. 环境变量
    elif os.environ.get("MINERU_TOKEN"):
        configured = True
        source = "env"

    # 3. 配置文件 ~/.mineru/config.yaml
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

    return {"configured": configured, "source": source}


def main():
    parser = argparse.ArgumentParser(description="Check MinerU API token configuration.")
    parser.add_argument("--token", help="API token (overrides env and config, highest priority)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON only")
    args = parser.parse_args()

    info = detect_token(args.token)

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    if info["configured"]:
        print(f"✅ MinerU Token 已配置 (来源: {info['source']}) — 可使用精确 VLM 解析 (extract --model vlm)")
    else:
        print("❌ 未检测到 MinerU Token — 将降级使用 flash-extract（质量差、排队慢）")
        print()
        print("配置指引（推荐优先提供 Token 以获得高精度 VLM 解析）：")
        print("  免费申请: https://mineru.net/apiManage/token")
        print("  方式 A (临时): $env:MINERU_TOKEN=\"your_token_here\"")
        print("  方式 B (持久): \"your_token_here\" | mineru-open-api auth")
        print("  注: 不要使用 `mineru-open-api auth --token ...`（v0.5.9 会卡交互）")


if __name__ == "__main__":
    main()