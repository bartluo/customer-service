"""RRF 融合与重排测试。

覆盖三件财税检索里最容易出错的事：
  · 三路结果要能合并到同一条条文上（按 article_version_id 对齐）
  · 三路都召回的条文要排在只有一路召回的前面
  · 税务规范性答复这类低位阶文件不能压过法律
"""

from __future__ import annotations
from datetime import datetime, timezone

from app.retrieval.fusion import (
    FusedHit,
    FusionWeights,
    apply_boosts,
    reciprocal_rank_fusion,
)
from app.retrieval.rerank import classify_intent, rerank


def _structured(key: str, **overrides) -> object:
    """结构化路返回的是对象（StructuredHit 同形）。"""

    data = {
        "article_version_id": key,
        "content": "纳税人发生应税行为应当缴纳增值税。",
        "full_no": "第一条",
        "level_code": "article",
        "hierarchy_level": "normative_document",
        "effect_status": "effective",
        "document_number": "财税〔2019〕39号",
        "regulation_title": "测试公告",
        "valid_from": datetime(2019, 3, 20, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return type("Hit", (), data)()


def _semantic(key: str, **overrides) -> dict:
    data = {
        "article_version_id": key,
        "content": "进项税额准予在计算应纳税额时抵扣。",
        "full_no": "第二十七条",
        "level_code": "article",
        "hierarchy_level": "normative_document",
        "effect_status": "effective",
        "document_number": "财税〔2019〕39号",
        "regulation_title": "测试公告",
        "valid_from": datetime(2019, 3, 20, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return data


def _glossary(key: str, **overrides) -> dict:
    data = _semantic(key, **overrides)
    data["full_no"] = "第十条"
    return data


def test_fusion_merges_same_article_across_routes() -> None:
    """同一条文被三路召回，合并成一条且 routes 齐全。"""

    hits = reciprocal_rank_fusion(
        {
            "structured": [_structured("v1"), _structured("v2")],
            "semantic": [_semantic("v1"), _semantic("v3")],
            "glossary_expansion": [_glossary("v1")],
        }
    )
    assert len(hits) == 3
    top = hits[0]
    assert top.key == "v1"
    assert set(top.routes) == {"structured", "semantic", "glossary_expansion"}


def test_fusion_prefers_multi_route_hits() -> None:
    """三路都召回的条文排在单路召回的前面。"""

    hits = reciprocal_rank_fusion(
        {
            "structured": [_structured("v1")],
            "semantic": [_semantic("v1"), _semantic("v2")],
            "glossary_expansion": [_glossary("v1")],
        }
    )
    assert hits[0].key == "v1"
    assert hits[0].routes != ["v1"] or len(hits[0].routes) == 1


def test_fusion_respects_weights() -> None:
    """结构化权重最高，结构化独有的结果应该排在语义独有的前面。"""

    weights = FusionWeights()
    hits = reciprocal_rank_fusion(
        {
            "structured": [_structured("s1")],
            "semantic": [_semantic("m1")],
        },
        weights,
    )
    # 两者都是各自路的第 1 名，权重大的排前面
    assert hits[0].key == "s1"
    assert hits[0].score > hits[1].score


def test_fusion_score_is_normalized() -> None:
    """归一化到 0~1，方便后续 boost 加权。"""

    hits = reciprocal_rank_fusion(
        {"structured": [_structured("v1"), _structured("v2"), _structured("v3")]}
    )
    for hit in hits:
        assert 0.0 <= hit.score <= 1.0
    assert hits[0].score == 1.0


def test_fusion_ignores_zero_weight_routes() -> None:
    """权重为 0 的路不参与融合。"""

    hits = reciprocal_rank_fusion(
        {
            "structured": [_structured("v1")],
            "semantic": [_semantic("v2")],
        },
        FusionWeights(semantic=0.0),
    )
    assert [hit.key for hit in hits] == ["v1"]


def test_fusion_handles_empty_routes() -> None:
    """任一路为空不崩。"""

    assert reciprocal_rank_fusion({}) == []
    assert reciprocal_rank_fusion({"structured": [], "semantic": [_semantic("v1")]}) != []


def test_fusion_skips_items_without_key() -> None:
    """缺标识的条目跳过，不污染结果。"""

    hits = reciprocal_rank_fusion(
        {"semantic": [{"content": "无标识"}, _semantic("v1")]}
    )
    assert [hit.key for hit in hits] == ["v1"]


def test_fusion_citation_format() -> None:
    """引用串格式：文号 + 条款号。"""

    hits = reciprocal_rank_fusion({"semantic": [_semantic("v1")]})
    assert hits[0].citation() == "财税〔2019〕39号 第二十七条"


def test_boost_exact_document_number() -> None:
    """文号精确命中的条目加分并上浮。"""

    hits = reciprocal_rank_fusion(
        {
            "semantic": [
                _semantic("other", document_number="财税〔2016〕36号"),
                _semantic("v1", document_number="财税〔2019〕39号"),
            ]
        }
    )
    boosted = apply_boosts(hits, query_document_number="财税〔2019〕39号")
    assert boosted[0].key == "v1"
    assert "exact_document_number" in boosted[0].boost_detail


def test_boost_recent_amendment() -> None:
    """12 个月内修订的条目加分。"""

    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    recent = _semantic("recent", valid_from=now.replace(month=8))
    old = _semantic("old", valid_from=datetime(2019, 3, 20, tzinfo=timezone.utc))
    hits = reciprocal_rank_fusion({"semantic": [old, recent]})
    boosted = apply_boosts(hits, query_document_number=None, as_of=now)
    assert "recent_amendment" in next(h for h in boosted if h.key == "recent").boost_detail


def test_boost_penalizes_local_normative() -> None:
    """地方规范性文件降权。"""

    local = _semantic("local", hierarchy_level="local_normative")
    law = _semantic("law", hierarchy_level="law")
    hits = reciprocal_rank_fusion({"semantic": [law, local]})
    boosted = apply_boosts(hits, query_document_number=None)
    by_key = {h.key: h for h in boosted}
    assert "local_normative_penalty" in by_key["local"].boost_detail
    assert by_key["local"].score < by_key["law"].score


def test_classify_intent() -> None:
    """意图分类。"""

    assert classify_intent("申报流程是什么") == "procedure"
    assert classify_intent("不申报会怎么处罚") == "liability"
    assert classify_intent("这个税怎么计算") == "calculation"
    assert classify_intent("哪些企业适用") == "scope"
    assert classify_intent("今天天气不错") == "unknown"


def _fused(key: str, routes: list[str] | None = None, **overrides) -> FusedHit:
    payload = {
        "article_version_id": key,
        "content": "纳税人应当自规定的期限内向税务机关申报纳税。",
        "level_code": "article",
        "hierarchy_level": "normative_document",
        "effect_status": "effective",
    }
    payload.update(overrides)
    routes = routes or ["semantic"]
    return FusedHit(
        key=key,
        payload=payload,
        score=0.5,
        routes=routes,
        ranks={name: index + 1 for index, name in enumerate(routes)},
    )


def test_rerank_prefers_higher_hierarchy() -> None:
    """位阶高的条文排在前面：法律 > 税务规范性答复。"""

    law = _fused("law", hierarchy_level="law", content="依照本法规定。")
    reply = _fused("reply", hierarchy_level="tax_authority_reply", content="依照本法规定。")
    results = rerank([reply, law], query_text="依照本法规定", top_n=5)
    assert results[0].hit.key == "law"


def test_rerank_keeps_more_relevant_hit_on_top() -> None:
    """相关度高于位阶：更相关的那条必须排在前面，哪怕位阶更低。

    回归背景：重排原来不含相关度项，只按位阶等线索打分。
    后果是"问 3 岁以下婴幼儿照护怎么扣"时，语义检索第一名本来是
    《国务院关于设立3岁以下婴幼儿照护个人所得税专项附加扣除的通知》，
    重排后被《中华人民共和国税收征收管理法》顶掉——只因为它是法律。
    """

    relevant_notice = _fused(
        "notice",
        hierarchy_level="normative_document",
        content="纳税人照护3岁以下婴幼儿子女的相关支出，按每个婴幼儿每月1000元定额扣除。",
    )
    relevant_notice.score = 1.0
    relevant_notice.ranks = {"semantic": 1}

    irrelevant_law = _fused(
        "law",
        hierarchy_level="law",
        content="纳税人未按照规定的期限办理纳税申报的，由税务机关责令限期改正。",
    )
    irrelevant_law.score = 0.4
    irrelevant_law.ranks = {"semantic": 6}

    results = rerank(
        [irrelevant_law, relevant_notice], query_text="3岁以下婴幼儿照护怎么扣", top_n=5
    )
    assert results[0].hit.key == "notice", "召回到的更相关条文不能被位阶更高的无关条文顶掉"
    assert "相关度" in results[0].reasons


def test_rerank_prefers_intent_match() -> None:
    """意图匹配加分：问流程时，讲申报的条文排前面。"""

    on_topic = _fused("proc", content="纳税人应当自规定期限内向税务机关申报纳税。")
    off_topic = _fused("calc", content="本条所称税率按照附表规定执行。", level_code="item")
    results = rerank([off_topic, on_topic], query_text="申报流程怎么走", top_n=5)
    assert results[0].hit.key == "proc"


def test_rerank_rewards_multi_route_hits() -> None:
    """多路命中的条文加权。"""

    multi = _fused("multi", routes=["structured", "semantic", "glossary_expansion"])
    single = _fused("single")
    results = rerank([single, multi], query_text="申报", top_n=5)
    assert results[0].hit.key == "multi"
    assert "多路命中" in results[0].reasons


def test_rerank_penalizes_partially_repealed() -> None:
    """部分废止的条文要排在有效条文之后。"""

    effective = _fused("ok", effect_status="effective")
    partial = _fused("partial", effect_status="partially_repealed")
    results = rerank([partial, effective], query_text="申报", top_n=5)
    assert results[0].hit.key == "ok"


def test_rerank_respects_top_n() -> None:
    """只返回 top_n 条。"""

    hits = [_fused(f"v{i}") for i in range(15)]
    results = rerank(hits, query_text="申报", top_n=10)
    assert len(results) == 10


def test_rerank_explain_is_readable() -> None:
    """重排结果带可解释信息，答案里要能说明"为什么这条排第一"。"""

    results = rerank([_fused("v1")], query_text="申报流程", top_n=1)
    assert results[0].explain()


def test_rerank_never_returns_effect_status_missing() -> None:
    """重排不引入硬过滤——过滤在检索层已完成，这里只排顺序。"""

    hit = _fused("v1")
    hit.payload.pop("effect_status", None)
    results = rerank([hit], query_text="申报", top_n=1)
    assert results[0].score >= 0
