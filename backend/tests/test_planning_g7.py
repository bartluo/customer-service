"""筹划引擎测试（第一批）。

这一层最严重的失败模式是**给出违法方案**，所以测试的重点不是"能不能生成方案"，
而是"该拒的有没有拒、该拦的有没有拦、该提示的有没有提示"。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.planning import CompanyProfile, LegalityChecker, RedLineMatcher
from app.planning.profile import ProfileError  # noqa: F401  （保留：画像取值的错误类型）
from app.planning.techniques import (
    TechniqueLibrary,
    load_technique_file,
    parse_citation,
)


@pytest.fixture(scope="module")
def matcher() -> RedLineMatcher:
    return RedLineMatcher()


def _full_profile(**overrides) -> CompanyProfile:
    base = dict(
        entity_type="有限公司",
        taxpayer_type="小规模纳税人",
        goal="降低税负",
        risk_preference="稳健",
        flexible={"can_change_contract": True},
        business_purpose="客户要求按月结算，现金回流更稳定",
        business_benefit="降低客户资金压力，提高续约率",
        evidence_available=["服务合同", "履约记录"],
        business_authentic=True,
    )
    base.update(overrides)
    return CompanyProfile(**base)


# ---------------------------------------------------------------------------
# 企业画像
# ---------------------------------------------------------------------------


def test_profile_reports_missing_required_fields() -> None:
    """缺什么就说什么，不替用户假设。"""

    missing = " ".join(CompanyProfile().missing_required())
    assert "组织形式" in missing
    assert "纳税人身份" in missing
    assert "筹划目标" in missing
    assert "风险偏好" in missing
    assert "可调整弹性" in missing
    assert "业务真实性" in missing


def test_full_profile_has_nothing_missing() -> None:
    assert _full_profile().missing_required() == []


def test_profile_rejects_invalid_enum() -> None:
    problems = CompanyProfile(entity_type="随便写的", risk_preference="非常激进").validate()
    assert len(problems) == 2


def test_profile_rejects_negative_amount() -> None:
    problems = CompanyProfile(revenue=Decimal("-1")).validate()
    assert any("不能为负数" in item for item in problems)


# ---------------------------------------------------------------------------
# 红线清单：8 类全部要能拦住，并且给出替代路径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("能不能帮我虚开发票冲成本", "false_invoice"),
        ("这笔收入我不想入账", "conceal_income"),
        ("帮我做假账少交税", "false_declaration"),
        ("把我们包装成高新技术企业骗优惠", "fraud_preference"),
        ("签两份合同，一份给税务局看", "dual_contract"),
        ("在税收洼地注册个空壳公司开票", "shell_business"),
        ("虚构土地出让金来少缴土增税", "false_land"),
        ("通过关联交易定价把利润转到低税率公司", "transfer_price"),
    ],
)
def test_red_lines_are_detected(matcher: RedLineMatcher, text: str, code: str) -> None:
    hits = matcher.match(text)
    assert any(hit.code == code for hit in hits), f"{text} 应命中 {code}，实际 {[h.code for h in hits]}"


@pytest.mark.parametrize("text", ["虚开发票", "隐瞒收入不申报", "做假账"])
def test_red_line_hit_always_has_alternative(matcher: RedLineMatcher, text: str) -> None:
    """拒绝时必须给替代路径——只拒绝不给路，等于把用户推向更野的中介。"""

    hits = matcher.match(text)
    assert hits
    for hit in hits:
        assert hit.alternative, f"{hit.code} 缺少替代路径"


def test_normal_question_is_not_blocked(matcher: RedLineMatcher) -> None:
    """正常咨询不能被误拦——一有风吹草动就拒绝，等于把客户赶走。"""

    assert matcher.match("我想了解一下小微企业有什么税收优惠") == []
    assert matcher.match("怎么合理安排开票时间") == []
    assert matcher.match("我想降低公司税负，有什么合规办法") == []


def test_planning_switch_is_off_by_default(matcher: RedLineMatcher) -> None:
    """筹划功能总开关在资质主体落实前必须保持关闭。"""

    assert matcher.planning_enabled is False


# ---------------------------------------------------------------------------
# 四道合法性检查：每道都能拦
# ---------------------------------------------------------------------------


def test_basis_check_fails_without_citation() -> None:
    checker = LegalityChecker(session=None)
    report = checker.check(_full_profile(), citations=[])
    basis = next(item for item in report.checks if item.code == "basis")
    assert basis.passed is False
    assert basis.fatal is True
    assert report.risk_level == "red"


def test_red_line_check_fails_and_is_fatal(matcher: RedLineMatcher) -> None:
    checker = LegalityChecker(session=None, matcher=matcher)
    report = checker.check(
        _full_profile(),
        request_text="我想虚开发票把成本做上去",
        citations=[{"document_number": "x", "effect_status": "effective"}],
    )
    red = next(item for item in report.checks if item.code == "red_line")
    assert red.passed is False
    assert red.fatal is True
    assert report.risk_level == "red"
    assert report.fatal is True


def test_anti_avoidance_check_fails_without_business_purpose() -> None:
    """用户答不上"为什么做这件事"，本身就是风险信号——不替他编。"""

    checker = LegalityChecker(session=None)
    profile = _full_profile(business_purpose=None, business_benefit=None, evidence_available=[])
    report = checker.check(profile, citations=[{"document_number": "x", "effect_status": "effective"}])
    anti = next(item for item in report.checks if item.code == "anti_avoidance")
    assert anti.passed is False
    assert len(anti.reasons) == 3  # 三道问句各一条
    # 反避税不过不"作废"，但风险升到 🟡
    assert report.risk_level == "yellow"


def test_substance_check_requires_user_confirmation() -> None:
    """业务真实性只能由用户确认，系统不能替他确认。"""

    checker = LegalityChecker(session=None)
    profile = _full_profile(business_authentic=None)
    report = checker.check(
        profile, citations=[{"document_number": "x", "effect_status": "effective"}]
    )
    substance = next(item for item in report.checks if item.code == "substance")
    assert substance.passed is False
    assert substance.fatal is True
    assert report.risk_level == "red"


def test_all_four_checks_pass_for_clean_case(matcher: RedLineMatcher) -> None:
    checker = LegalityChecker(session=None, matcher=matcher)
    report = checker.check(
        _full_profile(),
        citations=[{"document_number": "x", "effect_status": "effective"}],
    )
    assert report.passed is True
    assert report.risk_level == "green"
    assert [item.code for item in report.checks] == [
        "basis",
        "red_line",
        "anti_avoidance",
        "substance",
    ]


# ---------------------------------------------------------------------------
# 风险分级与输出控制
# ---------------------------------------------------------------------------


def test_aggressive_preference_raises_to_yellow(matcher: RedLineMatcher) -> None:
    checker = LegalityChecker(session=None, matcher=matcher)
    report = checker.check(
        _full_profile(risk_preference="进取"),
        citations=[{"document_number": "x", "effect_status": "effective"}],
    )
    assert report.risk_level == "yellow"


def test_red_level_is_review_only(matcher: RedLineMatcher) -> None:
    """🔴 方案不直达用户，只进专家复核台。"""

    config = matcher.risk_level_config("red")
    assert config["action"] == "review_only"
    assert config["require_review"] is True


def test_yellow_level_requires_prompt_and_review(matcher: RedLineMatcher) -> None:
    config = matcher.risk_level_config("yellow")
    assert config["action"] == "output_with_prompt"
    assert config["require_review"] is True
    assert config["prompt"]


def test_green_level_outputs_directly(matcher: RedLineMatcher) -> None:
    config = matcher.risk_level_config("green")
    assert config["action"] == "output"
    assert config["require_review"] is False


# ---------------------------------------------------------------------------
# 筹划手法库：六字段齐备、匹配三态
# ---------------------------------------------------------------------------


def test_technique_file_has_six_required_fields() -> None:
    items = load_technique_file()
    assert len(items) >= 6
    for item in items:
        for field_name in (
            "conditions",
            "citations",
            "mechanism",
            "risk_level",
            "abuse_boundary",
            "rejected_cases",
        ):
            assert field_name in item, f"{item['code']} 缺字段 {field_name}"
        assert item["abuse_boundary"], f"{item['code']} 的滥用边界不能为空——那是最有价值的一条"


def test_parse_citation_distinguishes_number_and_title() -> None:
    """手法库里的依据有两种写法，解析错了依据检查会把正确手法判废。"""

    number = parse_citation("财政部 税务总局公告2023年第19号")
    assert number["document_number"] == "财政部 税务总局公告2023年第19号"
    assert number["regulation_title"] is None

    titled = parse_citation("中华人民共和国增值税法 第二十三条")
    assert titled["regulation_title"] == "中华人民共和国增值税法"
    assert titled["full_no"] == "第二十三条"

    # 法规名本身带空格时，不能按第一个空格切
    long_title = parse_citation("财政部 税务总局关于中小微企业设备器具所得税税前扣除有关政策的公告")
    assert long_title["regulation_title"].startswith("财政部 税务总局关于")
    assert long_title["full_no"] is None


def test_technique_library_matches_three_states() -> None:
    """匹配是三态：适用 / 不适用 / 信息不足。把"信息不足"当成不适用会误导用户。"""

    from app.planning.techniques import TechniqueMatch
    from app.models.planning import PlanningTechnique

    technique = PlanningTechnique(
        code="t",
        name="测试手法",
        category="政策适用型",
        conditions=[
            {"field": "taxpayer_type", "op": "=", "value": "小规模纳税人", "description": "小规模"},
            {"field": "employees", "op": "<=", "value": 300, "description": "人数不超 300"},
        ],
        citations=["x"],
        mechanism="m",
        risk_level="green",
        abuse_boundary=["b"],
        rejected_cases=[],
        review_state="draft",
    )
    library = TechniqueLibrary.__new__(TechniqueLibrary)  # 不连库，只测匹配逻辑

    matched = library.match_one(_full_profile(employees=10), technique)
    assert matched.matched is True

    violated = library.match_one(_full_profile(taxpayer_type="一般纳税人", employees=10), technique)
    assert violated.matched is False
    assert violated.violated

    unknown = library.match_one(_full_profile(employees=None), technique)
    assert unknown.matched is None
    assert "人数不超 300" in unknown.missing


def test_technique_library_excludes_draft_when_excluding_drafts() -> None:
    """手法素材未经专家确认前以 draft 入库，出方案时只认 published。"""

    from app.planning.techniques import TechniqueLibrary

    class _Empty:
        def execute(self, *_args, **_kwargs):
            class _Result:
                def scalars(self):
                    return self

                def all(self):
                    return []

            return _Result()

    library = TechniqueLibrary(_Empty(), include_draft=False)  # type: ignore[arg-type]
    assert library.all() == []
