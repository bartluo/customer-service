"""脚本控制台编码修复（Windows 专用）。

本机（Windows 中文环境 + cp1252 控制台）直接 `python scripts/xxx.py` 时，
打印中文会抛 UnicodeEncodeError：'charmap' codec can't encode characters。
这不是脚本的 bug，是控制台默认编码不是 UTF-8。

处理方式：脚本开头 import 本模块，把 stdout/stderr 强制切成 UTF-8，
并让无法编码的字符降级替换而不是抛异常——验证脚本应该报告结果，
不应该因为"某个字打不出来"就整个崩掉。

用法（放在其他 import 之前）：
    import _console  # noqa: F401
"""

from __future__ import annotations

import sys


def force_utf8() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


force_utf8()
