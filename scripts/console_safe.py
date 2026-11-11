#!/usr/bin/env python3
"""
console_safe.py - Windows 窄编码控制台（GBK / CP936）输出保护

问题：
    Windows 中文环境默认 stdout / stderr 编码为 GBK。当文件名或章节标题含
    日文中点「・」、emoji、生僻汉字等字符时，print() 会抛 UnicodeEncodeError，
    导致 CLI 直接崩溃且无任何输出（真实案例：文件名含「・」的 PDF 探针零输出）。

方案：
    包装 sys.stdout / sys.stderr 的 write()。正常路径原样写出，不改变用户终端
    既有编码（GBK 终端下中文仍正常显示）；仅当遇到无法编码的字符时，用
    backslashreplace 转义为 \\uXXXX 字面量后重试，保证永不崩溃。
    该转义在 JSON 字符串中恰好是合法转义，下游 json.loads 可无损还原。

用法：
    所有 CLI 入口在其他业务导入之前 `import console_safe` 即可自动生效
    （模块导入时自动调用 install()）。
"""

import sys


class _SafeWriter:
    """代理原始流，仅在编码失败时降级为 backslashreplace。"""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        try:
            return self._stream.write(text)
        except UnicodeEncodeError:
            enc = getattr(self._stream, "encoding", None) or "ascii"
            safe = text.encode(enc, errors="backslashreplace").decode(enc, errors="replace")
            return self._stream.write(safe)

    def flush(self):
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install() -> None:
    """
    幂等安装：重复 import 或被 pytest 捕获流时均安全。

    注意：**不**改动 stdout 的编码。既有调用方（含本项目集成测试）常以
    `subprocess.run(text=True)` 按系统 locale（Windows 中文为 GBK）解码子进程
    输出，若强制切 UTF-8 反而会引发 UnicodeDecodeError。这里只做「输出失败时
    转义重试」的兜底；面向 stdout 的 JSON 由各脚本用 ensure_ascii=True 输出，
    保证纯 ASCII、任意编码宿主都能无损还原中文。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and not isinstance(stream, _SafeWriter):
            try:
                setattr(sys, name, _SafeWriter(stream))
            except (AttributeError, TypeError):
                pass


install()
