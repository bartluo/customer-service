"""守住一个已经踩过的坑：这类配置文件必须保持纯 ASCII。

背景：`alembic upgrade head` 在中文 Windows 上会直接崩，报
`UnicodeDecodeError: 'charmap' codec can't decode byte 0x8d`。
原因是 Alembic 与 logging.fileConfig 读取 ini 时用"本地编码"（本机是 cp1252），
而不是 UTF-8；文件里只要有中文字节就会解码失败。

同理，pip 读 requirements.txt 也先按本地编码解，中文注释会出现乱码或警告。

所以：这些文件里的注释一律写英文，中文说明写在 .md 文档里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# 会被"本地编码"读取的配置文件（相对仓库根目录）
ASCII_ONLY_FILES = [
    "backend/alembic.ini",
    "backend/requirements.txt",
    "backend/pytest.ini",
    "embedding/requirements.txt",
    "embedding/requirements-heavy.txt",
]


@pytest.mark.parametrize("relative_path", ASCII_ONLY_FILES)
def test_config_file_is_ascii_only(relative_path: str) -> None:
    path = REPO_ROOT / relative_path
    if not path.exists():
        pytest.skip(f"{relative_path} 不存在，跳过")

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):  # 带 BOM 的 UTF-8 不在此问题范围
        pytest.skip(f"{relative_path} 带 BOM，另作处理")

    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        offset = exc.start
        snippet = raw[max(0, offset - 20): offset + 20].decode("utf-8", errors="replace")
        pytest.fail(
            f"{relative_path} 含非 ASCII 字节（位置 {offset}）：…{snippet}…\n"
            f"该文件由 Alembic / pip 以本地编码读取，请把中文注释改成英文。"
        )
