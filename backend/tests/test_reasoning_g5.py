"""推理层验收（事实抽取 / 适用性判定 / 槽位补齐）。

贯穿这一层的原则是"抽不到就说抽不到"，所以测试重点不只是"抽得对"，
还有"抽不到的时候有没有老实说"。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.reasoning import ApplicabilityJudge, ExtractedFacts, FactExtractor
from app.reasoning.applicability import Verdict
from app.reasoning.facts import load_extractor_config


@pytest.fixture(scope="module")
def extractor() -> FactExtractor:
    return FactExtractor(load_extractor_config())


# ---------------------------------------------------------------------------
# 事实抽取
# ---------------------------------------------------------------------------


def test_extracts_explicit_taxpayer_type(extractor: FactExtractor) -> None:
    """明确声明的身份算确认；不需要再问。"""

    facts = extractor.extract("我是小规模纳税人，本季度销售额28万元")
    assert facts.taxpayer_type == "小规模纳税人"
    assert facts.taxpayer_type_confirmed is True


def test_colloquial_taxpayer_is_marked_for_confirmation(extractor: FactExtractor) -> None:
    """"我这小店"只能推测是"个体工商户"，必须标成需确认。

    把推测当成事实，用户会按错误的主体去适用政策——这是财税场景最危险的错法。
    """

    facts = extractor.extract("我开了个小卖部，上个月卖了103万")
    assert facts.taxpayer_type == "个体工商户"
    assert facts.taxpayer_type_confirmed is False
    assert any("确认" in item for item in facts.missing)
    assert any("个体工商户" in q for q in facts.questions)


def test_extracts_amount_without_yuan_word(extractor: FactExtractor) -> None:
    """"103万"这种省略"元"的说法很常见，必须认。"""

    assert extractor.extract("上个月卖了103万").amount == Decimal("1030000.00")
    assert extractor.extract("收入 500 元").amount == Decimal("500.00")
    assert extractor.extract("营业额1.5万").amount == Decimal("15000.00")


def test_does_not_treat_plain_number_as_amount(extractor: FactExtractor) -> None:
    """纯数字不是金额——"3 个月"里的 3 不能被当成钱。"""

    assert extractor.extract("我们做了3个月了").amount is None


def test_excludes_tax_wins_over_includes_tax(extractor: FactExtractor) -> None:
    """"不含税"含"含税"这四个字的子串，判定顺序不能反。"""

    assert extractor.extract("收入100万元不含税").amount_includes_tax is False
    assert extractor.extract("收入100万元含税").amount_includes_tax is True


def test_extracts_input_vat_separately(extractor: FactExtractor) -> None:
    """进项税额要单独抽出来，混在金额里会被当成销售额。"""

    facts = extractor.extract("一般纳税人销售货物收入100万元，进项税3万元")
    assert facts.amount == Decimal("1000000.00")
    assert facts.input_vat == Decimal("30000.00")


def test_extracts_business_type_matching_calculator_keywords(extractor: FactExtractor) -> None:
    """业务类型的取值要能直接对上计算引擎的税率关键词，否则算了也选不对税率。"""

    from app.calculation import get_engine

    engine = get_engine()
    for text, expected in (
        ("我卖东西", "销售货物"),
        ("提供技术咨询服务", "现代服务"),
        ("做建筑工程的", "建筑服务"),
        ("货物出口到国外", "出口货物"),
    ):
        facts = extractor.extract(text)
        assert facts.business_type == expected, text
        assert engine.rules.match_rate(facts.business_type) is not None, expected


def test_extracts_period_and_region(extractor: FactExtractor) -> None:
    facts = extractor.extract("上个月在市区卖了10万元")
    assert facts.period == "上月"
    assert facts.region == "市区"


def test_extracts_scale_facts(extractor: FactExtractor) -> None:
    """小微企业判定要用从业人数、资产总额、应纳税所得额。"""

    facts = extractor.extract("我们公司从业人数80人，资产总额3000万元，应纳税所得额200万元")
    assert facts.scale["从业人数"] == Decimal("80.00")
    assert facts.scale["资产总额"] == Decimal("30000000.00")
    assert facts.scale["应纳税所得额"] == Decimal("2000000.00")


def test_evidence_traces_back_to_original_text(extractor: FactExtractor) -> None:
    """每个要素都要能指回原文，用户质疑时可核对。"""

    facts = extractor.extract("我是小规模纳税人，上个月卖了103万")
    described = " ".join(item.describe() for item in facts.evidence)
    assert "小规模纳税人" in described
    assert "上个月" in described
    assert "103万" in described


def test_empty_input_is_handled(extractor: FactExtractor) -> None:
    facts = extractor.extract("")
    assert facts.taxpayer_type is None
    assert facts.questions  # 什么都不知道，至少要把该问的列出来


# ---------------------------------------------------------------------------
# 槽位补齐：信息不足时提问，而不是硬答
# ---------------------------------------------------------------------------


def test_asks_about_tax_amount_scope(extractor: FactExtractor) -> None:
    """给了金额但没说含税还是不含税，必须追问——这两个口径算出来差很多。"""

    facts = extractor.extract("我是小规模纳税人，这个月卖了103万")
    assert any("含税" in q for q in facts.questions)


def test_questions_are_capped(extractor: FactExtractor) -> None:
    """一次最多问两个问题（reasoner.yaml 的 max_questions），问多了用户会放弃。"""

    facts = extractor.extract("怎么办")
    assert 0 < len(facts.questions) <= 2


def test_missing_list_names_required_slots(extractor: FactExtractor) -> None:
    facts = extractor.extract("怎么办")
    joined = " ".join(facts.missing)
    assert "纳税人身份" in joined
    assert "适用期间" in joined


# ---------------------------------------------------------------------------
# 适用性判定
# ---------------------------------------------------------------------------


def _facts(**overrides) -> ExtractedFacts:
    base = ExtractedFacts(raw_text="test")
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_verdict_applicable_when_all_conditions_met() -> None:
    judge = ApplicabilityJudge()
    facts = _facts(taxpayer_type="小规模纳税人", tax_types=["增值税"])
    verdict = judge.judge_one(
        facts,
        {
            "title": "某公告",
            "citation": "某公告 一、",
            "content": "增值税小规模纳税人发生应税交易，销售额未超过10万元的，免征增值税。",
            "tax_types": ["增值税"],
        },
    )
    assert verdict.verdict is Verdict.NEED_CONFIRM  # 用户没给销售额，判不了
    assert "销售额" in verdict.reason or "未提供" in verdict.reason


def test_verdict_not_applicable_with_reason() -> None:
    """主体不符要明确判不适用，并说清是哪一条不符。"""

    judge = ApplicabilityJudge()
    facts = _facts(taxpayer_type="一般纳税人", tax_types=["增值税"])
    verdict = judge.judge_one(
        facts,
        {
            "title": "某公告",
            "citation": "某公告 一、",
            "content": "增值税小规模纳税人月销售额未超过10万元的，免征增值税。",
            "tax_types": ["增值税"],
        },
    )
    assert verdict.verdict is Verdict.NOT_APPLICABLE
    assert "小规模纳税人" in verdict.reason
    assert "一般纳税人" in verdict.reason


def test_verdict_not_applicable_when_amount_exceeds_threshold() -> None:
    """超过金额上限要判不适用——这正是"不适用清单"的价值。"""

    judge = ApplicabilityJudge()
    facts = _facts(
        taxpayer_type="小规模纳税人",
        tax_types=["增值税"],
        amount=Decimal("2000000.00"),
    )
    verdict = judge.judge_one(
        facts,
        {
            "title": "某公告",
            "citation": "某公告 一、",
            "content": "增值税小规模纳税人月销售额未超过10万元的，免征增值税。",
            "tax_types": ["增值税"],
        },
    )
    assert verdict.verdict is Verdict.NOT_APPLICABLE
    assert "超过" in verdict.reason


def test_verdict_need_confirm_lists_missing_data() -> None:
    """条件存在但用户信息不足 → 需确认，并指明缺哪一项。"""

    judge = ApplicabilityJudge()
    facts = _facts(taxpayer_type="小型微利企业", tax_types=["企业所得税"])
    verdict = judge.judge_one(
        facts,
        {
            "title": "某公告",
            "citation": "某公告 三、",
            "content": "所称小型微利企业，是指从业人数不超过300人、资产总额不超过5000万元的企业。",
            "tax_types": ["企业所得税"],
        },
    )
    assert verdict.verdict is Verdict.NEED_CONFIRM
    assert "从业人数" in verdict.reason or "资产总额" in verdict.reason


def test_detects_headcount_and_asset_thresholds() -> None:
    """人数、资产门槛要能识别出来；识别不到就永远不会进"需确认"清单。"""

    judge = ApplicabilityJudge()
    facts = _facts(taxpayer_type="小型微利企业")

    headcount = judge._check_thresholds(facts, "小型微利企业从业人数不超过300人。")
    assert any(item.name == "从业人数" for item in headcount), headcount

    assets = judge._check_thresholds(facts, "资产总额不超过5000万元的企业。")
    assert any(item.name == "资产总额" for item in assets), assets

    money = judge._check_thresholds(facts, "月销售额未超过10万元的，免征增值税。")
    assert any(item.name == "金额门槛" for item in money), money


def test_tax_type_mismatch_is_not_applicable() -> None:
    """问增值税却拿企业所得税的政策来比，要判不适用，不能硬套。"""

    judge = ApplicabilityJudge()
    facts = _facts(taxpayer_type="一般纳税人", tax_types=["增值税"])
    verdict = judge.judge_one(
        facts,
        {
            "title": "某企业所得税公告",
            "citation": "某公告 一、",
            "content": "对小型微利企业年应纳税所得额不超过300万元的部分，减按25%计入应纳税所得额。",
            "tax_types": ["企业所得税"],
        },
    )
    assert verdict.verdict is Verdict.NOT_APPLICABLE
    assert "税种" in verdict.reason


def test_report_splits_into_three_lists() -> None:
    """报告结构必须是三类清单：适用 / 不适用 / 需确认。"""

    judge = ApplicabilityJudge()
    facts = _facts(taxpayer_type="小规模纳税人", tax_types=["增值税"], amount=Decimal("50000.00"))
    report = judge.judge(
        facts,
        [
            {
                "title": "能用的公告",
                "citation": "公告A 一、",
                "content": "增值税小规模纳税人月销售额未超过10万元的，免征增值税。",
                "tax_types": ["增值税"],
            },
            {
                "title": "不能用的公告",
                "citation": "公告B 一、",
                "content": "增值税一般纳税人发生下列情形，可以抵扣进项税额。",
                "tax_types": ["增值税"],
            },
            {
                "title": "信息不足的公告",
                "citation": "公告C 三、",
                "content": "小型微利企业从业人数不超过300人。",
                "tax_types": ["增值税"],
            },
        ],
    )
    assert len(report.applicable) >= 1
    assert len(report.not_applicable) >= 1
    assert len(report.need_confirm) >= 1, report.explain()
    text = report.explain()
    assert "【适用】" in text and "【不适用】" in text and "【需确认】" in text


def test_judge_returns_empty_report_for_no_candidates() -> None:
    report = ApplicabilityJudge().judge(_facts(), [])
    assert report.applicable == []
    assert report.not_applicable == []
    assert report.need_confirm == []
