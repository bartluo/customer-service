"""检索硬过滤。

技术方案 5.3 明确要求：财税的效力过滤必须放在检索前，
避免已废止条款占用召回名额。做成"检索前的 Qdrant filter"而不是
"检索后过滤"，原因有两：
  1. 废止条文不会挤占 Top-K 名额——如果检索后再过滤，可能过滤完就空了，
     用户得到的是"没找到"而不是"找到正确的"。
  2. 硬过滤是"不满足直接排除，不是降权"（retrieval.yaml），
     降权处理会让废止条文排在第 20 位，万一 Top-K 拉大就会漏出来。
"""

from __future__ import annotations
from datetime import datetime, timezone

from qdrant_client.models import FieldCondition, Filter, MatchAny, Range

from app.knowledge.ontology import CITABLE_EFFECT_STATUSES


def _timestamp(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def effect_filter_payload() -> list[str]:
    """可引用的效力状态白名单。返回 list 是为了给非Qdrant 检索复用。"""

    return list(CITABLE_EFFECT_STATUSES)


def build_effect_filter(
    domain_id: str | None = "finance_tax",
    effect_statuses: list[str] | None = None,
    review_states: list[str] | None = None,
    tax_types: list[str] | None = None,
    as_of: datetime | None = None,
) -> Filter:
    """构造检索前的硬过滤条件。

    默认就带上三条财税红线：
      · 效力状态在可引用白名单内（排除废止 / 被替代 / 草案）
      · 审查状态为 published（排除待复核）
      · 时点落在 [valid_from, valid_to) 区间内（左闭右开）
    """

    must: list = []

    if domain_id:
        must.append(FieldCondition(key="domain_id", match=MatchAny(any=[domain_id])))

    must.append(
        FieldCondition(
            key="effect_status",
            match=MatchAny(any=effect_statuses or effect_filter_payload()),
        )
    )

    must.append(
        FieldCondition(
            key="review_state",
            match=MatchAny(any=review_states or ["published"]),
        )
    )

    if tax_types:
        # JSONB 数组字段用 MatchAny 匹配数组元素
        tax_conditions = [
            FieldCondition(key="tax_types", match=MatchAny(any=[item])) for item in tax_types
        ]
        must.append(Filter(must=tax_conditions) if len(tax_conditions) > 1 else tax_conditions[0])

    if as_of is not None:
        point = _timestamp(as_of)
        # valid_from <= as_of
        must.append(FieldCondition(key="valid_from_ts", range=Range(lte=point)))
        # valid_to > as_of 或为空（仍在有效期）
        must.append(
            Filter(
                should=[
                    FieldCondition(key="valid_to_ts", range=Range(gt=point)),
                    FieldCondition(key="valid_to_ts", is_null=True),
                ]
            )
        )

    return Filter(must=must)
