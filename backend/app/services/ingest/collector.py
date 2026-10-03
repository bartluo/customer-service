"""采集流水线：把原始法规文件收集成待导入的文档列表。

第一版策略：文件投放 + 自动扫描（inbox 模式）。

为什么这样设计：
  · 官方站点（税务总局、中国政府网）的反爬与版权条款各不相同，
    现在锁定某个站点风险大。先把"文件到手 → 入库"的链路打通，
    采集源后面逐个对接。
  · 采集源做成 Collector 协议，新增一个站点只需实现 collect()，
    流水线、解析器、入库逻辑全部不动。

编码现实：国内政府网站仍有大量 GBK 编码页面，因此读取必须多编码兜底。
"""

from __future__ import annotations
import json
import pathlib
import re
from typing import Protocol

from app.domain.tax_types import TAX_TYPES, detect_tax_types
from app.domain.effect_status import from_official_aging
from app.services.ingest.importer import DocumentInput

# 支持的扩展名。.txt 与 .md 是文本；.htm/.html 需要额外清洗（后续接采集时用）
SUPPORTED_SUFFIXES = {".txt", ".md", ".text"}


class Collector(Protocol):
    """采集源协议。新增一个采集源只需实现 collect()。"""

    def collect(self) -> list[DocumentInput]:
        """返回一批待导入文档。"""

        ...


def read_text_file(path: pathlib.Path) -> str:
    """读取文本文件，按 UTF-8 → GBK → 宽松解码依次尝试。

    为什么不用单一编码：UTF-8 编码的 GBK 文件会抛异常，
    而 GBK 编码的 UTF-8 文件会解出乱码。逐个尝试最稳。
    """

    raw = path.read_bytes()
    for encoding in ("utf-8", "gbk", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 理论上到不了这里：latin-1 能解任意字节序列
    return raw.decode("utf-8", errors="replace")


class InboxCollector:
    """目录投放采集器：扫描一个目录，返回待导入文档。"""

    def __init__(self, directory: pathlib.Path, domain_id: str = "finance_tax") -> None:
        self.directory = pathlib.Path(directory)
        self.domain_id = domain_id

    def collect(self) -> list[DocumentInput]:
        if not self.directory.exists():
            raise FileNotFoundError(f"采集目录不存在：{self.directory}")
        if not self.directory.is_dir():
            raise NotADirectoryError(f"采集路径不是目录：{self.directory}")

        documents: list[DocumentInput] = []
        for path in sorted(self.directory.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            if path.name.startswith("."):
                continue
            if path.name.startswith("_"):
                # 下划线开头的是清单之类的辅助文件，不是法规
                continue
            text = read_text_file(path)
            # 来源留痕：优先用 sidecar 元数据里的官方来源 URL；
            # 没有 sidecar 时退回 file:// 路径，至少保证"来源可追溯"
            sidecar = _read_sidecar(path)
            source_url = sidecar.get("source_url") or path.resolve().as_uri()
            tax_types = _guess_tax_types(
                sidecar.get("collection"),
                text,
                tax_policy=sidecar.get("tax_policy"),
                explicit=sidecar.get("tax_types"),
                title=sidecar.get("title") or path.stem.split("_", 1)[-1],
            )
            documents.append(
                DocumentInput(
                    filename=str(path.relative_to(self.directory)),
                    text=text,
                    source_url=source_url,
                    source_file_key=f"finance_tax/inbox/{path.stem}",
                    tax_types=tax_types,
                    # 官方"时效性"翻译成本项目效力状态：
                    # 官方标"全文废止"的法规不能进库就当现行有效，
                    # 否则检索会把废止条文当依据引出来。
                    effect_status=from_official_aging(sidecar.get("effect_status_hint")),
                )
            )
        return documents


def _read_sidecar(path: pathlib.Path) -> dict:
    """读取同名 .meta.json 元数据文件（清洗脚本生成）。

    为什么用 sidecar 而不是把元数据写进正文：正文里塞 URL 会被条文切分器
    当成正文一行，切出无意义的"知识单元"。元数据与正文分开更干净。
    """

    sidecar_path = path.with_suffix(".meta.json")
    if not sidecar_path.exists():
        # 文件名形如 "010_标题.txt"，sidecar 名为 "010_标题.meta.json"
        sidecar_path = path.with_name(path.stem + ".meta.json")
    if not sidecar_path.exists():
        return {}
    try:
        return json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# 税种名单统一放在 app/domain/tax_types.py，采集与检索共用一份
KNOWN_TAX_TYPES = TAX_TYPES

# 旧版清洗脚本留下的 collection 标记（cn_vat / cn_cit 等）→ 税种
_COLLECTION_MAP = {
    "cn_vat": "增值税",
    "cn_vat_admin": "增值税",
    "cn_vat_import": "增值税",
    "cn_export_refund": "增值税",
    "cn_invoice": "增值税",
    "cn_surcharge": "城市维护建设税",
    "cn_stamp_duty": "印花税",
    "cn_cit": "企业所得税",
    "cn_tax_admin": None,  # 见下：这批文件必须靠 tax_policy，不能一律算增值税
    "cn_accounting": "会计",
    "cn_accounting_law": "会计",
    "cn_small_business": "增值税",
    "cn_rd_superdeduction": "企业所得税",
    "finance_regulations": "会计",
}


def tax_types_from_tax_policy(tax_policy: str | None) -> list[str]:
    """把官方"税费类型"字段翻译成本项目的税种。

    官方格式形如 `税收政策-个人所得税`、`税收政策-其他税收政策`、`其他`。
    取最后一段（税种名），只在命中已知口径时才返回，认不出就交给关键词兜底——
    宁可少标，也不要标错。标错会让"按税种过滤"的检索把对的法规排除掉。
    """

    if not tax_policy:
        return []
    tail = tax_policy.split("-")[-1].strip()
    candidates = [part.strip() for part in re.split(r"[、,，/和及]", tail) if part.strip()]
    found: list[str] = []
    for candidate in candidates or [tail]:
        for known in KNOWN_TAX_TYPES:
            if candidate == known or known in candidate:
                if known not in found:
                    found.append(known)
                break
    return found


def _guess_tax_types(
    collection: str | None,
    text: str,
    tax_policy: str | None = None,
    explicit: list[str] | None = None,
    title: str | None = None,
) -> list[str]:
    """推断适用税种，用于结构化与语义检索过滤。

    优先级（越靠前越可信，都是"有才用、没有就往下退"，绝不硬猜）：
      1. sidecar 里直接写明的 tax_types（人工校对过的值）
      2. 官方"税费类型"字段 tax_policy —— 网站自己标的，最权威
      3. 清洗脚本留下的 collection 标记（cn_vat / cn_cit 等）
      4. 标题里的税种名
      5. 正文里出现次数最多的税种（要求至少出现 2 次）

    为什么必须有第 2 条：采集脚本给这一批文件统一写了栏目分类码
    `cn_tax_admin`，旧映射把它一律解释成"增值税"，
    结果是《个人所得税法》被打上"增值税"标签，
    用户问个税时语义检索按税种过滤，反而把这部法律排除掉了。

    为什么第 4、5 条分开：一份长公告正文里会顺带提到很多税种，
    直接扫正文会打出四五个标签（见过"企业所得税/增值税/消费税/会计"这种），
    过滤时反而把不相关的条文也放进来。标题更精确，正文只在标题没线索时才用，
    而且要求出现 2 次以上、最多取 2 个。
    """

    if explicit:
        return [item for item in explicit if item]

    from_official = tax_types_from_tax_policy(tax_policy)
    title_types = detect_tax_types(title) if title else []

    if from_official:
        # 官方分类与标题识别可能落在不同维度，取并集。
        # 典型例子：增值税专用发票类公告，官方分类是"增值税"，
        # 但用户是照着"发票"来问的（"发票丢了怎么办"）。
        # 过滤是"命中任一标签即可"，多一个维度只会提高召回，
        # 不会把本来该命中的法规排除掉；只取官方的那个维度反而会漏。
        merged = list(from_official)
        for name in title_types:
            if name not in merged:
                merged.append(name)
        return merged

    if collection and _COLLECTION_MAP.get(collection):
        return [_COLLECTION_MAP[collection]]  # type: ignore[list-item]

    if title_types:
        return title_types[:3]

    # 正文兜底：只保留出现过 2 次以上的，最多取 2 个
    counts = {name: text.count(name) for name in detect_tax_types(text)}
    ranked = sorted(
        (name for name, count in counts.items() if count >= 2),
        key=lambda name: -counts[name],
    )
    return ranked[:2]


def collect_from_directory(directory: pathlib.Path, domain_id: str = "finance_tax") -> list[DocumentInput]:
    """便捷函数：扫描目录并返回待导入文档列表。"""

    return InboxCollector(directory, domain_id=domain_id).collect()
