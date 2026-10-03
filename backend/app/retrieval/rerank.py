"""重排。

检索解决的是"召回得全不全"，重排解决的是"最该引用的排不排在最前面"。
财税场景的答案质量直接取决于第一条引用对不对，所以这一层不是优化项而是质量项。

本阶段用启发式而不是 Cross-Encoder 重排模型，原因有三：
  1. 财税条文的判断依据是位阶、效力、主题命中这类**结构化线索**，
     启发式在这些线索上比通用语义模型更准也更可控。
  2. 通用重排模型没见过财税术语，"施行日期""计税依据"对它是陌生词。
  3. 每加一个模型就多一个 GPU 依赖，目标是打通链路，不是把效果调到位。
retrieval.yaml 的 rerank.fallback=heuristic 就是这个决定；
未来要换模型，只需替换 HeuristicReranker.score 的实现，接口不变。

位阶先验的数值来源：retrieval.yaml 的 hard_filters 里
"hierarchy_rank <= 4" 意味着第四位阶及以下才可作直接依据，
越靠前（法律 > 行政法规 > 部门规章）越优先。

**但位阶只是微调，不是主项。** 重排的主项必须是检索相关度：
如果按位阶排序，法律永远压过公告，而财税实务里真正回答问题的
往往是总局公告或国务院通知（"3岁以下婴幼儿照护怎么扣"就是典型）。
"""

from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field

from app.retrieval.fusion import FusedHit

logger = logging.getLogger(__name__)

# 位阶 -> 先验分。retrieval.yaml: hierarchy_rank <= 4 才是可引用依据
HIERARCHY_PRIOR = {
    "law": 1.00,
    "administrative_regulation": 0.90,
    "departmental_rule": 0.80,
    "normative_document": 0.75,
    "judicial_interpretation": 0.70,
    "local_normative": 0.50,
    "tax_authority_reply": 0.30,   # 税务规范性答复：只能参考，不能当依据
}

DEFAULT_PRIOR = 0.60

# 主题线索：命中即加分。键是用户问题的意图类型
INTENT_KEYWORDS = {
    "procedure": ["流程", "怎么办", "如何办理", "申报", "材料", "手续", "步骤"],
    "liability": ["处罚", "罚款", "滞纳金", "风险", "责任", "后果"],
    "calculation": ["税率", "计算", "税额", "多少钱", "怎么算", "计税", "减免"],
    "scope": ["哪些", "范围", "适用", "对象", "条件"],
}

# 意图 -> 条文类型线索。命中即认为"这条正好在回答用户的问题"
INTENT_CONTENT_HINTS = {
    "procedure": ["申报", "办理", "报送", "材料", "流程", "期限"],
    "liability": ["处罚", "罚款", "滞纳金", "责令", "没收"],
    "calculation": ["税率", "税额", "计税", "应纳税额", "扣除", "税率表"],
    "scope": ["适用", "范围", "对象", "以下", "不包括"],
}


@dataclass
class RerankWeights:
    """各项打分权重。总和不必为 1——只影响相对顺序。"""

    # 相关度是主项，其余都是"在同样相关的前提下怎么取舍"的微调。
    relevance: float = 0.45
    intent: float = 0.20
    multi_route: float = 0.13
    hierarchy: float = 0.12
    level: float = 0.05
    effect: float = 0.05


@dataclass
class RerankResult:
    """重排结果的一条，带可解释的得分构成。"""

    hit: FusedHit
    score: float
    intent: str
    reasons: dict = field(default_factory=dict)

    def explain(self) -> str:
        parts = [f"{k}={v:+.2f}" for k, v in self.reasons.items() if v]
        return f"{self.intent}：" + ("、".join(parts) if parts else "无加分项")


def classify_intent(text: str) -> str:
    """粗分意图。财税问题就这四类，先不引入分类模型。"""

    if not text:
        return "unknown"
    best_intent = "unknown"
    best_weight = 0
    for intent, keywords in INTENT_KEYWORDS.items():
        # 位置权重：关键词表越靠前说明它对这个意图越专属。
        # "不申报会怎么处罚"同时命中"申报"和"处罚"，
        # 不加位置权重就会按字典顺序误判成 procedure。
        best_local = 0
        for position, keyword in enumerate(keywords):
            if keyword in text:
                best_local = max(best_local, len(keywords) - position)
        if best_local > best_weight:
            best_intent, best_weight = intent, best_local
    return best_intent


class HeuristicReranker:
    """启发式重排器。"""

    def __init__(self, weights: RerankWeights | None = None) -> None:
        self.weights = weights or RerankWeights()

    @staticmethod
    def relevance_of(hit: FusedHit) -> float:
        """相关度：按"在召回里排第几"折算，而不是用融合分。

        为什么不直接用融合分：RRF 的分数是 Σ 1/(60+rank)，天然被压得很平——
        第 1 名 0.0164、第 5 名 0.0154，归一化后只差 6%，
        压不住位阶这类固定加分（法律 vs 公告差 3 个百分点），
        结果还是"法律永远赢"。

        名次才真正表达"这条和问题有多像"：第 1 名 1.00、第 2 名 0.74、
        第 5 名 0.43、第 10 名 0.26。多路命中时取最好名次。
        """

        ranks = [rank for rank in (hit.ranks or {}).values() if rank]
        if not ranks:
            return max(0.0, min(1.0, hit.score))
        best = min(ranks)
        return 1.0 / (1.0 + 0.35 * (best - 1))

    def score(self, hit: FusedHit, intent: str) -> RerankResult:
        payload = hit.payload
        w = self.weights
        reasons: dict[str, float] = {}
        score = 0.0

        # 0) 相关度：召回名次折算（见 relevance_of）。
        #    为什么必须是主项：少了它，重排就退化成"按位阶排序"——
        #    法律永远压过公告，哪怕那条公告才是能回答问题的规定。
        #    实测出现过：问"3岁以下婴幼儿照护怎么扣"，语义检索第一名就是
        #    《国务院关于设立3岁以下婴幼儿照护个人所得税专项附加扣除的通知》，
        #    但重排后第一位变成了《中华人民共和国税收征收管理法》，
        #    只因为它是"法律"、位阶分高。
        relevance = self.relevance_of(hit)
        value = w.relevance * relevance
        score += value
        reasons["相关度"] = value

        # 1) 意图匹配：条文内容里有没有用户问的那个东西
        content = payload.get("content", "") or ""
        hints = INTENT_CONTENT_HINTS.get(intent, [])
        if hints:
            matched = sum(1 for hint in hints if hint in content)
            if matched:
                value = w.intent * min(matched / 2.0, 1.0)
                score += value
                reasons["意图匹配"] = value

        # 2) 位阶先验：法律优于公告，公告优于规范性答复
        hierarchy = payload.get("hierarchy_level", "")
        prior = HIERARCHY_PRIOR.get(hierarchy, DEFAULT_PRIOR)
        value = w.hierarchy * prior
        score += value
        reasons["位阶"] = value

        # 3) 多路命中：两路以上都召回的条文更可信
        route_count = len(hit.routes)
        if route_count >= 2:
            value = w.multi_route * min(route_count / 3.0, 1.0)
            score += value
            reasons["多路命中"] = value

        # 4) 引用粒度：条比款比项更适合作为直接依据
        level_value = {"article": 1.0, "paragraph": 0.6, "item": 0.4}.get(
            payload.get("level_code", ""), 0.5
        )
        value = w.level * level_value
        score += value
        reasons["引用粒度"] = value

        # 5) 效力状态：部分废止的条文要慎用
        effect_value = {"effective": 1.0, "partially_repealed": 0.5}.get(
            payload.get("effect_status", ""), 0.0
        )
        value = w.effect * effect_value
        score += value
        reasons["效力状态"] = value

        return RerankResult(hit=hit, score=round(score, 4), intent=intent, reasons=reasons)

    def rerank(self, hits: list[FusedHit], query_text: str) -> list[RerankResult]:
        intent = classify_intent(query_text)
        results = [self.score(hit, intent) for hit in hits]
        results.sort(key=lambda item: item.score, reverse=True)
        return results


def rerank(hits: list[FusedHit], query_text: str, top_n: int = 10) -> list[RerankResult]:
    """便捷入口。retrieval.yaml 的 rerank.top_n = 10。"""

    return HeuristicReranker().rerank(hits, query_text)[:top_n]
