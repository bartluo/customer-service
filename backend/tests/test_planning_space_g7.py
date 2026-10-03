"""第二批测试：筹划空间生成 / 量化测算 / 多方案对比。

这一批的验收重点是两条：
  · 方案能按三条路径生成出来，信息不足的手法也要给（并标出缺什么）；
  · **数字全部来自计算引擎，算不出来就明说**——对比表里给一个假的精确数，
    用户会拿它去决策。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.planning import PlanningTechnique
from app.planning.profile import CompanyProfile
from app.planning.space import (
    PATH_BY_CATEGORY,
    CandidatePlan,
    Measurement,
    PlanningSpaceGenerator,
    build_comparison,
)
from app.planning.techniques import TechniqueMatch


def _profile(**overrides) -> CompanyProfile:
    base = dict(
        entity_type="有限公司",
        taxpayer_type="小规模纳税人",
        industry="软件和信息技术服务业",
        region="市区",
        employees=20,
        annual_revenue=Decimal("900000"),
        revenue=Decimal("900000"),
        profit=Decimal("300000"),
        taxable_income=Decimal("300000"),
        total_assets=Decimal("2000000"),
        goal="降低税负",
        risk_preference="稳健",
        flexible={"can_change_contract": True, "can_change_entity": False, "can_change_timing": True},
        business_purpose="客户要求按月结算",
        business_benefit="现金回流稳定，续约率提升",
        evidence_available=["服务合同"],
        business_authentic=True,
    )
    base.update(overrides)
    return CompanyProfile(**base)


def _technique(code: str, category: str, measure: dict, **overrides) -> PlanningTechnique:
    base = dict(
        code=code,
        name=f"{code} 手法",
        category=category,
        conditions=[],
        citations=[],
        mechanism="m",
        risk_level="green",
        abuse_boundary=["b"],
        rejected_cases=[],
        review_state="draft",
        playbook={},
        measure=measure,
    )
    base.update(overrides)
    return PlanningTechnique(**base)


@pytest.fixture()
def generator() -> PlanningSpaceGenerator:
    """不连库的生成器：只测生成与测算逻辑。"""

    from app.calculation import get_engine

    instance = PlanningSpaceGenerator.__new__(PlanningSpaceGenerator)
    instance.engine = get_engine()
    return instance


# ---------------------------------------------------------------------------
# 筹划空间生成
# ---------------------------------------------------------------------------


def test_category_maps_to_three_paths() -> None:
    """三类路径的归属不能乱：手法类别决定走哪条路。"""

    assert PATH_BY_CATEGORY["政策适用型"] == "A"
    assert PATH_BY_CATEGORY["主体架构型"] == "B"
    assert PATH_BY_CATEGORY["资产与投资型"] == "B"
    assert PATH_BY_CATEGORY["区域型"] == "B"
    assert PATH_BY_CATEGORY["时点安排型"] == "C"


def test_plan_carries_actions_steps_evidence(generator: PlanningSpaceGenerator) -> None:
    """方案要带得动落地信息，否则用户拿到的是"一个想法"而不是"一件事"。"""

    technique = _technique(
        "t1",
        "政策适用型",
        {"tax": "vat", "kind": "exempt"},
        playbook={
            "actions": ["核对销售额"],
            "steps": ["统计", "申报"],
            "evidence": ["销售明细"],
            "cost": "低",
        },
    )
    match = TechniqueMatch(technique=technique, matched=True, checks=[])
    plan = generator._to_plan(match, _profile())
    assert plan.path == "A"
    assert plan.actions == ["核对销售额"]
    assert plan.steps == ["统计", "申报"]
    assert plan.evidence == ["销售明细"]
    assert plan.cost == "低"


def test_plan_records_missing_info(generator: PlanningSpaceGenerator) -> None:
    """信息不足的方案照样要给，但必须标出缺什么。"""

    from app.planning.techniques import ConditionCheck

    technique = _technique("t2", "主体架构型", {})
    match = TechniqueMatch(
        technique=technique,
        matched=None,
        checks=[ConditionCheck("主体架构可以调整", None, "缺信息")],
    )
    plan = generator._to_plan(match, _profile())
    assert plan.missing == ["主体架构可以调整"]


# ---------------------------------------------------------------------------
# 量化测算：数字来自引擎，算不出来就说清楚
# ---------------------------------------------------------------------------


def test_measure_vat_exempt(generator: PlanningSpaceGenerator) -> None:
    tech = _technique("vat_exempt", "政策适用型", {"tax": "vat", "kind": "exempt"})
    result = generator.measure(CandidatePlan("x", "x", "A", "政策适用型", "green"), tech, _profile())
    assert result.computable is True
    # 90 万/年 → 按征收率 3% 简易计税，应纳税额 900000/1.03*3% ≈ 26213.59
    assert result.after == Decimal("0.00")
    assert result.saving == result.before
    assert result.steps, "测算要带可展示的计算步骤"


def test_measure_surcharge_halve(generator: PlanningSpaceGenerator) -> None:
    tech = _technique("half", "政策适用型", {"tax": "surcharges", "kind": "halve"})
    result = generator.measure(CandidatePlan("x", "x", "A", "政策适用型", "green"), tech, _profile())
    assert result.computable is True
    # 减半之后应恰好是原来的一半
    assert result.saving == result.before - result.after
    assert result.after * 2 == result.before or abs(result.after * 2 - result.before) <= Decimal("0.02")


def test_measure_small_low_profit(generator: PlanningSpaceGenerator) -> None:
    """30 万应纳税所得额：25% → 5% 实际税负，节税 6 万。"""

    tech = _technique("cit", "政策适用型", {"tax": "cit", "kind": "small_low_profit"})
    result = generator.measure(CandidatePlan("x", "x", "A", "政策适用型", "green"), tech, _profile())
    assert result.computable is True
    assert result.before == Decimal("75000.00")
    assert result.after == Decimal("15000.00")
    assert result.saving == Decimal("60000.00")


def test_measure_returns_reason_when_not_computable(generator: PlanningSpaceGenerator) -> None:
    """算不出来必须说明为什么，而不是给个空值或零。"""

    tech = _technique("rd", "政策适用型", {"tax": "cit", "kind": "rd_super_deduction"})
    result = generator.measure(CandidatePlan("x", "x", "A", "政策适用型", "green"), tech, _profile())
    assert result.computable is False
    assert result.saving is None
    assert "研发费用" in result.note

    empty = _technique("none", "时点安排型", {})
    result2 = generator.measure(CandidatePlan("x", "x", "C", "时点安排型", "yellow"), empty, _profile())
    assert result2.computable is False
    assert result2.note


def test_measure_needs_revenue(generator: PlanningSpaceGenerator) -> None:
    tech = _technique("vat_exempt", "政策适用型", {"tax": "vat", "kind": "exempt"})
    profile = _profile(annual_revenue=None, revenue=None)
    result = generator.measure(CandidatePlan("x", "x", "A", "政策适用型", "green"), tech, profile)
    assert result.computable is False
    assert "销售额" in result.note


# ---------------------------------------------------------------------------
# 多方案对比
# ---------------------------------------------------------------------------


def _plan(code: str, saving: str | None, risk: str = "green", cost: str = "低") -> CandidatePlan:
    measurement = Measurement()
    if saving is None:
        measurement = Measurement(computable=False, note="需人工测算")
    else:
        measurement = Measurement(
            computable=True,
            before=Decimal(saving) * 2,
            after=Decimal(saving),
            saving=Decimal(saving),
            tax_type="增值税",
        )
    return CandidatePlan(
        code=code,
        name=f"方案{code}",
        path="A",
        category="政策适用型",
        risk_level=risk,
        cost=cost,
        measurement=measurement,
    )


def test_comparison_sorted_by_saving_desc() -> None:
    table = build_comparison([_plan("a", "1000"), _plan("b", "5000"), _plan("c", "2000")])
    savings = [row["saving"] for row in table["rows"]]
    assert savings == ["5000", "2000", "1000"]


def test_unmeasured_plans_go_last() -> None:
    """没测算的放最后，并说明待补什么。"""

    table = build_comparison([_plan("a", None), _plan("b", "1000")])
    assert table["rows"][0]["code"] == "b"
    assert "待补数据" in table["rows"][1]["saving_text"]


def test_comparison_row_has_advice_and_risk() -> None:
    table = build_comparison([_plan("a", "1000", risk="yellow", cost="中")])
    row = table["rows"][0]
    assert row["risk_level"] == "yellow"
    assert row["cost"] == "中"
    assert "沟通" in row["advice"]


def test_red_plan_advice_is_review_only() -> None:
    """🔴 方案的建议必须是"仅进专家复核"，不能写成"可实施"（雏形）。"""

    table = build_comparison([_plan("a", "1000", risk="red")])
    assert "专家复核" in table["rows"][0]["advice"]
    assert "推荐优先实施" not in table["rows"][0]["advice"]


def test_comparison_includes_combination_advice() -> None:
    """只给方案清单不够，还要提示哪些能叠加、哪些要一起评估。"""

    plans = [_plan("a", "1000")]
    plans.append(
        CandidatePlan(
            code="b", name="方案b", path="B", category="主体架构型", risk_level="yellow",
            cost="高", measurement=Measurement(computable=False, note="需人工测算"),
        )
    )
    table = build_comparison(plans)
    notes = " ".join(table["combinations"])
    assert "主体架构" in notes
    assert "税务机关认定" in notes
