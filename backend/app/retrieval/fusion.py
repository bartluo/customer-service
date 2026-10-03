"""RRF 融合与加权。

三路召回各有所长，谁也不能单独当答案：
  · 结构化  查文号、条款号、税种，精确但召不回想表达
  · 语义    查"意思"，但对"财税〔2019〕39号"这种精确串不敏感
  · 术语扩展 把"小规模"补成"小规模纳税人"，覆盖面更宽

为什么用 RRF（倒数排名融合）而不是加权分数相加：
  结构化走 SQL 排序、语义走向量相似度，两种分数量纲完全不同
  （一个是"第几行"，一个是 0~1 的余弦值），直接相加等于随便挑一个乘系数。
  RRF 只看名次不看分数，天然跨系统可比。

公式：score(d) = Σ_route  weight_route / (k + rank_route(d))
k=60 是 RRF 的标准常数，作用是压平头部差距——
第 1 名和第 2 名的分差不会大到让其他路的第 2 名完全没机会。
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# 来源 retrieval.yaml 的 fusion.weights
DEFAULT_WEIGHTS = {"structured": 1.0, "semantic": 0.8, "glossary_expansion": 0.5}
DEFAULT_K = 60

# 来源 retrieval.yaml 的 boosts（影响排序，不影响是否召回）
DEFAULT_BOOSTS = {
    "exact_document_number": 0.5,
    "recent_amendment": 0.2,
    "local_normative_penalty": -0.2,
}


def _item_key(item: Any) -> str | None:
    """从三种可能的结果类型里取出统一标识。

    三路的返回类型不同：结构化给 StructuredHit（对象），
    语义给 Qdrant payload（字典），术语扩展给自己的结果。
    统一到 article_version_id 才能把它们合并到同一条上——
    这是融合能不能work的唯一关键点。
    """

    if item is None:
        return None
    if isinstance(item, dict):
        return item.get("article_version_id") or item.get("version_id")
    return getattr(item, "article_version_id", None) or getattr(item, "version_id", None)


def item_payload(item: Any) -> dict:
    """把任意一路的结果统一成字典，方便后续重排和拼装答案。"""

    if isinstance(item, dict):
        return item
    return {
        "article_version_id": getattr(item, "article_version_id", None),
        "article_id": getattr(item, "article_id", None),
        "regulation_id": getattr(item, "regulation_id", None),
        "content": getattr(item, "content", ""),
        "full_no": getattr(item, "full_no", ""),
        "article_no": getattr(item, "article_no", ""),
        "level_code": getattr(item, "level_code", ""),
        "heading_path": getattr(item, "heading_path", None),
        "document_number": getattr(item, "document_number", None),
        "regulation_title": getattr(item, "regulation_title", ""),
        "issuer": getattr(item, "issuer", None),
        "hierarchy_level": getattr(item, "hierarchy_level", ""),
        "effect_status": getattr(item, "effect_status", ""),
        "valid_from": getattr(item, "valid_from", None),
        "valid_to": getattr(item, "valid_to", None),
        "tax_types": getattr(item, "tax_types", []) or [],
        "applies_to": getattr(item, "applies_to", []) or [],
        "source_url": getattr(item, "source_url", None),
    }


def _item_valid_from(item: Any) -> datetime | None:
    if isinstance(item, FusedHit):
        value = item.payload.get("valid_from") or item.payload.get("valid_from_ts")
    else:
        value = item_payload(item).get("valid_from")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, datetime):
        return value
    return None


@dataclass
class FusionWeights:
    """三路权重，可被域包 retrieval.yaml 覆盖。"""

    structured: float = 1.0
    semantic: float = 0.8
    glossary_expansion: float = 0.5
    k: int = DEFAULT_K
    boosts: dict = field(default_factory=lambda: dict(DEFAULT_BOOSTS))

    def weight_of(self, route: str) -> float:
        return float(getattr(self, route, 0.0))


@dataclass
class FusedHit:
    """融合后的一条结果。"""

    key: str
    payload: dict
    score: float
    routes: list[str] = field(default_factory=list)
    ranks: dict = field(default_factory=dict)
    boost_detail: dict = field(default_factory=dict)

    def citation(self) -> str:
        number = self.payload.get("document_number") or self.payload.get("regulation_title") or ""
        full_no = self.payload.get("full_no") or ""
        return f"{number} {full_no}".strip()


def reciprocal_rank_fusion(
    route_results: dict[str, list[Any]],
    weights: FusionWeights | None = None,
) -> list[FusedHit]:
    """把多路结果按 RRF 融合成一条排名。"""

    weights = weights or FusionWeights()
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    routes: dict[str, list[str]] = {}
    ranks: dict[str, dict] = {}

    for route, items in route_results.items():
        weight = weights.weight_of(route)
        if weight <= 0:
            continue
        for rank, item in enumerate(items, start=1):
            key = _item_key(item)
            if not key:
                logger.debug("%s 路返回了缺少标识的条目，已跳过", route)
                continue
            scores[key] = scores.get(key, 0.0) + weight / (weights.k + rank)
            if key not in payloads:
                payloads[key] = item_payload(item)
            routes.setdefault(key, []).append(route)
            ranks.setdefault(key, {})[route] = rank

    # RRF 分数是量级很小的正数（1/61 起），直接当最终排序分会让
    # 后续的 boost（0.2/0.5 量级）完全压过它，所以按最大值缩放到 0~1。
    # 注意是"除以最大值"而不是"减最小值再除以极差"：
    # 减最小值会把排在最后的条文压成 0 分，任何一条 boost 都能把它顶到第一，
    # 那不是加权，是排序被 boost 接管了。除以最大值保持名次间的比例关系不变。
    if not scores:
        return []
    max_score = max(scores.values())

    hits: list[FusedHit] = []
    for key, raw in scores.items():
        base = raw / max_score if max_score > 0 else 0.0
        hits.append(
            FusedHit(
                key=key,
                payload=payloads[key],
                score=base,
                routes=sorted(set(routes[key])),
                ranks=ranks[key],
            )
        )

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits


def apply_boosts(
    hits: list[FusedHit],
    query_document_number: str | None,
    weights: FusionWeights | None = None,
    as_of: datetime | None = None,
) -> list[FusedHit]:
    """按 retrieval.yaml 的 boosts 加权（影响排序，不影响是否召回）。"""

    weights = weights or FusionWeights()
    boosts = weights.boosts
    reference = as_of or datetime.now(timezone.utc)

    for hit in hits:
        detail: dict[str, float] = {}
        score = hit.score

        # 文号精确命中：用户直接问的就是这份文件
        if query_document_number and hit.payload.get("document_number") == query_document_number:
            score += boosts.get("exact_document_number", 0.0)
            detail["exact_document_number"] = boosts.get("exact_document_number", 0.0)

        # 近期修订优先：12 个月内修订的排在前面
        valid_from = _item_valid_from(hit)
        if valid_from is not None:
            age_days = (reference - valid_from).days
            if 0 <= age_days <= 365:
                score += boosts.get("recent_amendment", 0.0)
                detail["recent_amendment"] = boosts.get("recent_amendment", 0.0)

        # 地方文件降权：存在上位法时，地方规范性文件不能当主要依据
        if hit.payload.get("hierarchy_level") == "local_normative":
            score += boosts.get("local_normative_penalty", 0.0)
            detail["local_normative_penalty"] = boosts.get("local_normative_penalty", -0.2)

        hit.score = max(score, 0.0)
        hit.boost_detail = detail

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits
