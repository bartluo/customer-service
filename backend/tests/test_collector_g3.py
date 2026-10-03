"""采集流水线测试。

采集采用"文件投放 + 自动导入"模式：人工或采集脚本把法规文件放进
inbox 目录，系统扫描、解析、入库。采集源做成可插拔接口（Collector 协议），
后续接官方网站采集时只需新增一个实现，不改流水线。

为什么这样设计：官方站点反爬策略与版权条款各不相同，先把入库链路打通，
采集源可以后续逐个对接，不阻塞验收。
"""

from __future__ import annotations
import pathlib

import pytest

from app.services.ingest.collector import (
    InboxCollector,
    collect_from_directory,
    read_text_file,
)


SAMPLE = """财政部 税务总局公告2020年第23号

关于延续实施应对疫情部分税费优惠政策的公告

第一条 为支持新冠肺炎疫情纾困，本次出台的增值税小规模纳税人优惠政策适用至2021年12月31日。
"""


def test_read_text_file_handles_utf8(tmp_path: pathlib.Path) -> None:
    """UTF-8 中文文本能正常读取。"""

    path = tmp_path / "a.txt"
    path.write_text(SAMPLE, encoding="utf-8")

    content = read_text_file(path)
    assert "财政部 税务总局公告2020年第23号" in content


def test_read_text_file_handles_gbk(tmp_path: pathlib.Path) -> None:
    """GBK 编码的 txt 也能读。国内很多政府网站仍用 GBK。"""

    path = tmp_path / "gbk.txt"
    path.write_bytes(SAMPLE.encode("gbk"))

    content = read_text_file(path)
    assert "关于延续实施应对疫情部分税费优惠政策的公告" in content


def test_read_text_file_falls_back_to_latin1(tmp_path: pathlib.Path) -> None:
    """未知编码不崩，退回宽松解码，总比导入失败强。"""

    path = tmp_path / "weird.txt"
    path.write_bytes(b"\xff\xfe hello")
    content = read_text_file(path)
    assert isinstance(content, str)


def test_collect_from_directory_picks_supported_files(tmp_path: pathlib.Path) -> None:
    """只挑支持的扩展名，忽略其它文件。"""

    (tmp_path / "a.txt").write_text(SAMPLE, encoding="utf-8")
    (tmp_path / "b.md").write_text(SAMPLE, encoding="utf-8")
    (tmp_path / "c.png").write_bytes(b"\x89PNG")
    (tmp_path / "skip.me").write_text(SAMPLE, encoding="utf-8")

    collector = InboxCollector(tmp_path)
    documents = collector.collect()
    names = [d.filename for d in documents]

    assert "a.txt" in names
    assert "b.md" in names
    assert "c.png" not in names
    assert "skip.me" not in names


def test_collect_from_directory_records_source_url(tmp_path: pathlib.Path) -> None:
    """来源留痕：file:// 路径要记进 source_url，满足合规要求。"""

    (tmp_path / "a.txt").write_text(SAMPLE, encoding="utf-8")
    documents = collect_from_directory(tmp_path)
    assert documents[0].source_url is not None
    assert documents[0].source_url.startswith("file://")


def test_collect_from_empty_directory_returns_empty(tmp_path: pathlib.Path) -> None:
    """空目录不报错。"""

    assert collect_from_directory(tmp_path) == []


def test_collect_from_missing_directory_raises(tmp_path: pathlib.Path) -> None:
    """目录不存在要明确报错，而不是静默返回空。"""

    with pytest.raises(FileNotFoundError):
        collect_from_directory(tmp_path / "nope")
