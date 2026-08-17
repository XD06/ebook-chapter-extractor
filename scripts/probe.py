#!/usr/bin/env python3
"""
probe.py - 全格式电子书快速探针工具 (支持 PDF / EPUB / MOBI / AZW3)
"""

import sys
import os

# 直接导入 probe_pdf 的 main 入口
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
from probe_pdf import main

if __name__ == "__main__":
    main()
