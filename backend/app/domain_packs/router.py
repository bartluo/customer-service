"""域路由：判断一个问题属于哪个域。

用"关键词 + 规则"，不依赖 LLM：确定性高、可测试、零成本。
接入 LLM 分类后，本模块保留为兜底与校验（LLM 说不确定时走这里）。

设计要点：
  · 租户级开关：租户没订阅的域不参与路由
  · 排除优先于包含：exclude 命中直接否掉，避免"其他领域问题里出现'税'字"被误判
  · 打分而非硬匹配：多个域都命中时按分数排序，都不够高就要求澄清
"""

from __future__ import annotations
from dataclasses import dataclass

from app.domain_packs.loader import DomainPack

# 确认阈值：分数达到且明显领先其他域才直接选定
SCORE_CONFIDENT = 3.0
# 澄清阈值：达到这个分但不够明确时，向用户澄清
SCORE_AMBIGUOUS = 1.0
# 领先幅度：最高分必须比第二名高出这么多，否则算"分数接近"
SCORE_MARGIN = 2.0


@dataclass(frozen=True)
class RouteResult:
    """路由结果。"""

    domain_id: str | None
    confidence: float
    scores: dict[str, float]
    reason: str
    needs_clarification: bool = False
    clarification_hint: str = ""


def _score_pack(pack: DomainPack, question: str) -> float:
    """给单个域包打分。命中越多、分值越高。"""

    routing = pack.manifest.routing
    score = 0.0
    # 排除优先：命中 exclude 直接归零，不看其他信号
    for term in routing.exclude:
        if term in question:
            return 0.0
    for term in routing.keywords:
        if term in question:
            score += 1.0
    for term in routing.include:
        if term in question:
            # include 是强信号，给更高权重
            score += 2.0
    return score


def route(
    question: str,
    packs: list[DomainPack],
    *,
    tenant_domains: list[str] | None = None,
) -> RouteResult:
    """把问题路由到域。

    tenant_domains 为 None 表示不做租户过滤（内部任务）；
    传空列表表示该租户没订阅任何域，什么都不匹配。
    """

    candidates = [pack for pack in packs if pack.is_active]
    if tenant_domains is not None:
        allowed = set(tenant_domains)
        candidates = [pack for pack in candidates if pack.domain_id in allowed]

    if not candidates:
        return RouteResult(
            domain_id=None,
            confidence=0.0,
            scores={},
            reason="没有可用的域（可能该租户未订阅任何域，或域包均未启用）",
        )

    scores = {pack.domain_id: _score_pack(pack, question) for pack in candidates}
    best_id = max(scores, key=lambda key: scores[key])
    best_score = scores[best_id]

    if best_score <= 0:
        return RouteResult(
            domain_id=None,
            confidence=0.0,
            scores=scores,
            reason="没有任何域的关键词命中",
        )

    # 多个域分数接近 → 不确定，应向用户澄清而不是猜
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    runner_up = ordered[1][1] if len(ordered) > 1 else 0.0
    # 只有一个候选域时不存在"分数接近"，直接按最高分判断
    margin_ok = len(ordered) == 1 or best_score - runner_up >= SCORE_MARGIN
    if best_score >= SCORE_CONFIDENT and margin_ok:
        return RouteResult(
            domain_id=best_id,
            confidence=min(1.0, best_score / 6.0),
            scores=scores,
            reason=f"{best_id} 得分 {best_score}，领先第二名 {best_score - runner_up} 分"
            if len(ordered) > 1
            else f"唯一可用域 {best_id}，得分 {best_score}",
        )

    # 没到确认线，但第二名几乎为 0：实际只有这一个域匹配，不该让用户澄清
    if len(ordered) == 1 or runner_up < 1.0:
        return RouteResult(
            domain_id=best_id,
            confidence=min(1.0, best_score / 6.0),
            scores=scores,
            reason=f"仅 {best_id} 有匹配（得分 {best_score}，其余域为 0）",
        )

    if best_score >= SCORE_AMBIGUOUS:
        hint = "、".join(f"{did}({score})" for did, score in ordered if score > 0)
        return RouteResult(
            domain_id=None,
            confidence=best_score / 6.0,
            scores=scores,
            reason=f"多个域分数接近：{hint}",
            needs_clarification=True,
            clarification_hint="您是想问财税政策，还是其他方面的问题？",
        )

    return RouteResult(
        domain_id=None,
        confidence=0.0,
        scores=scores,
        reason=f"最高分 {best_score} 未达澄清线 {SCORE_AMBIGUOUS}",
        needs_clarification=True,
        clarification_hint="请补充说明您想解决的具体问题",
    )
