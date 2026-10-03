"""计算引擎验收。

验收门 G5 的第一条：**20 道计算题全对**。这里就是那 20 道，外加附加税费 10 道。

为什么用表驱动写：财税计算题的期望值必须逐题写死——
如果期望值也是"再算一遍"得来的，测试就等于什么都没验证。
每道题的期望值都是按条文口径手工算出来的。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.calculation import get_engine
from app.calculation.engine import SurchargeInput, VatInput
from app.calculation.money import rate as to_rate, yuan


@pytest.fixture(scope="module")
def engine():
    return get_engine()


# ---------------------------------------------------------------------------
# 增值税：20 道典型题
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        # ---- 一般计税：销项税额 - 进项税额 ----
        ("13%货物，不含税100万，进项3万", dict(taxpayer_type="一般纳税人", sales_amount="1000000", business_type="销售货物", input_vat="30000"), "100000.00"),
        ("13%货物，含税113万，进项3万", dict(taxpayer_type="一般纳税人", sales_amount="1130000", amount_includes_tax=True, business_type="销售货物", input_vat="30000"), "100000.00"),
        ("9%交通运输，不含税200万，进项5万", dict(taxpayer_type="一般纳税人", sales_amount="2000000", business_type="交通运输服务", input_vat="50000"), "130000.00"),
        ("9%建筑服务，含税109万，进项2万", dict(taxpayer_type="一般纳税人", sales_amount="1090000", amount_includes_tax=True, business_type="建筑服务", input_vat="20000"), "70000.00"),
        ("6%现代服务，不含税50万，进项5000", dict(taxpayer_type="一般纳税人", sales_amount="500000", business_type="技术咨询服务", input_vat="5000"), "25000.00"),
        ("6%生活服务，含税106万，无进项", dict(taxpayer_type="一般纳税人", sales_amount="1060000", amount_includes_tax=True, business_type="生活服务"), "60000.00"),
        ("13%销售货物，销项小于进项→留抵", dict(taxpayer_type="一般纳税人", sales_amount="100000", business_type="销售货物", input_vat="20000"), "0.00"),
        ("13%销售货物，无进项", dict(taxpayer_type="一般纳税人", sales_amount="100000", business_type="销售货物"), "13000.00"),
        ("出口货物零税率", dict(taxpayer_type="一般纳税人", sales_amount="500000", business_type="出口货物"), "0.00"),
        ("直接给销项税额与进项税额", dict(taxpayer_type="一般纳税人", output_vat="130000", input_vat="80000"), "50000.00"),
        ("直接指定税率9%", dict(taxpayer_type="一般纳税人", sales_amount="100000", rate="0.09"), "9000.00"),
        # ---- 简易计税：销售额 × 征收率 ----
        #
        # 2026-10-02 按反馈改口径：**先看政策（含优惠），再按政策算税**。
        # 旧用例记的是"照 3% 硬算"，把两条优惠全漏了：
        #   · 财政部 税务总局公告2023年第19号 一、：月销售额未超过10万元免征
        #   · 同公告 二、：适用3%征收率的应税销售收入减按1%征收
        # 所以下面这几个期望值都变了（原值写在注释里）。
        ("小规模不含税100万→减按1%", dict(taxpayer_type="小规模纳税人", sales_amount="1000000", period_scope="month"), "10000.00"),  # 原 30000.00
        # 含税 103 万按 1% 换算：1,030,000 ÷ 1.01 = 1,019,801.98 → ×1% = 10,198.02（原 30000.00）
        ("小规模含税103万→减按1%", dict(taxpayer_type="小规模纳税人", sales_amount="1030000", amount_includes_tax=True, period_scope="month"), "10198.02"),
        # 不含税 10 万 = 免征标准含本数 → 免征（原 3000.00）
        ("小规模不含税10万→未超标准免征", dict(taxpayer_type="小规模纳税人", sales_amount="100000", period_scope="month"), "0.00"),
        # 含税 3090 元 → 不含税 3,059.41 → 远低于 10 万 → 免征（原 90.00）
        ("小规模含税3090元→免征", dict(taxpayer_type="小规模纳税人", sales_amount="3090", amount_includes_tax=True, period_scope="month"), "0.00"),
        ("小规模指定征收率3%（显式）", dict(taxpayer_type="小规模纳税人", sales_amount="200000", rate="0.03", period_scope="month"), "6000.00"),
        # ---- 期间口径决定用哪一档免征标准（2026-10-02 定的规则）----
        # 含税 25 万：按季度看 247,524.75 ≤ 30 万 → 免征
        ("小规模季度25万含税→按季免征", dict(taxpayer_type="小规模纳税人", sales_amount="250000", amount_includes_tax=True, period_scope="quarter"), "0.00"),
        # 同一笔钱按月度看 247,524.75 > 10 万 → 应税，3% 减按 1% → 2,475.25
        ("小规模月度25万含税→按月应税", dict(taxpayer_type="小规模纳税人", sales_amount="250000", amount_includes_tax=True, period_scope="month"), "2475.25"),
        # ---- 精度与边界 ----
        ("四舍五入到分（13%×1234.56）", dict(taxpayer_type="一般纳税人", sales_amount="1234.56", business_type="销售货物"), "160.49"),
        # 113.13 ÷ 1.13 = 100.1150… → 不含税销售额四舍五入到 100.12
        # 100.12 × 13% = 13.0156 → 应纳税额四舍五入到 13.02
        ("小数进位（含税113.13）", dict(taxpayer_type="一般纳税人", sales_amount="113.13", amount_includes_tax=True, business_type="销售货物"), "13.02"),
        # ---- 算不出来时必须说清楚，而不是给个数 ----
        ("主体未确认→不给数", dict(sales_amount="1000000"), "0.00"),
        ("业务类型认不出→不给数", dict(taxpayer_type="一般纳税人", sales_amount="1000000", business_type="卖一种说不清的东西"), "0.00"),
    ],
)
def test_vat_cases(engine, name: str, payload: dict, expected: str) -> None:
    result = engine.calc_vat(VatInput(**payload))
    assert str(result.payable) == expected, f"{name}：期望 {expected}，实际 {result.payable}"


def test_small_scale_exemption_is_really_applied(engine) -> None:
    """政策写了金额门槛，就要真的算进去，不能只当一句提示。

    回归背景（原话）："应该是先查看税收政策，包括优惠政策，
    然后按照政策计算税款。" 规则文件里若只写"免征标准以现行公告为准"
    不写金额，引擎照常按 3% 算出 2,912.62 元，而正确答案是免征。
    """

    result = engine.calc_vat(
        VatInput(
            taxpayer_type="小规模纳税人",
            sales_amount="100000",
            amount_includes_tax=True,
            business_type="销售货物",
        )
    )
    assert str(result.payable) == "0.00"
    assert result.discount_applied and "免征" in result.discount_applied
    assert any("2023年第19号" in step.citation for step in result.steps)
    assert result.preferences and result.preferences[0]["citation"].startswith("财政部")


def test_small_scale_rate_reduction_is_really_applied(engine) -> None:
    """超过免征标准时按 3% 减按 1% 计算，而不是照 3% 算。"""

    result = engine.calc_vat(
        VatInput(
            taxpayer_type="小规模纳税人",
            sales_amount="300000",
            amount_includes_tax=True,
            business_type="销售货物",
            period_scope="month",  # 按月口径：不含税 297,029.70 > 10 万 → 应税
        )
    )
    # 300,000 ÷ 1.01 = 297,029.70 → ×1% = 2,970.30
    assert str(result.payable) == "2970.30"
    assert any("减征优惠" in step.title and "减按" in step.substitution for step in result.steps)


def test_conversion_basis_conflict_is_flagged_not_guessed(engine) -> None:
    """两种换算口径结论不一致时不猜，标"需确认"。

    含税 103,000：按 3% 换算恰好等于 10 万（免征），
    按 1% 换算为 101,980.20（超过 10 万，应税）。
    这种边界情形不猜口径——算错方向会让用户按错误的数申报（ADR-0013）。
    """

    result = engine.calc_vat(
        VatInput(
            taxpayer_type="小规模纳税人",
            sales_amount="103000",
            amount_includes_tax=True,
            business_type="销售货物",
        )
    )
    assert result.steps == []
    assert any("换算口径" in note for note in result.notes)


def test_period_scope_decides_which_exemption_standard_applies(engine) -> None:
    """用户说月收入就按月 10 万判断，说季度收入就按季 30 万判断。

    2026-10-02 定的规则。同一笔"含税 25 万"：
      · 季度口径：不含税 247,524.75 ≤ 30 万 → 免征
      · 月度口径：不含税 247,524.75 > 10 万 → 应税，3% 减按 1%
    "这笔钱是哪个月的还是哪个季度的"会直接改变答案，所以不能含糊处理。
    """

    quarter = engine.calc_vat(
        VatInput(
            taxpayer_type="小规模纳税人",
            sales_amount="250000",
            amount_includes_tax=True,
            business_type="销售货物",
            period_scope="quarter",
        )
    )
    assert str(quarter.payable) == "0.00"
    assert quarter.discount_applied and "季度" in quarter.discount_applied

    month = engine.calc_vat(
        VatInput(
            taxpayer_type="小规模纳税人",
            sales_amount="250000",
            amount_includes_tax=True,
            business_type="销售货物",
            period_scope="month",
        )
    )
    assert str(month.payable) == "2475.25"


def test_period_unknown_and_verdicts_differ_asks_instead_of_guessing(engine) -> None:
    """没说期间、且按月与按季结论不一致时，不猜，直接请用户说明。"""

    result = engine.calc_vat(
        VatInput(
            taxpayer_type="小规模纳税人",
            sales_amount="250000",
            amount_includes_tax=True,
            business_type="销售货物",
        )
    )
    assert result.steps == []
    assert any("按月" in note and "按季" in note for note in result.notes)


def test_vat_general_shows_steps(engine) -> None:
    """计算过程必须能展示：公式 → 代入 → 结果，且带依据。"""

    result = engine.calc_vat(
        VatInput(
            taxpayer_type="一般纳税人",
            sales_amount=Decimal("1000000"),
            business_type="销售货物",
            input_vat=Decimal("30000"),
        )
    )
    titles = [step.title for step in result.steps]
    assert titles == ["销项税额", "应纳税额"]
    assert result.steps[0].formula == "不含税销售额 × 适用税率"
    assert "1,000,000.00" in result.steps[0].substitution
    assert "中华人民共和国增值税法 第十条（一）" in result.steps[0].citation
    assert result.steps[1].citation.startswith("中华人民共和国增值税法 第十四条")


def test_vat_includes_tax_conversion_step(engine) -> None:
    """含税金额要先换算成不含税销售额，这一步也要展示出来。"""

    result = engine.calc_vat(
        VatInput(
            taxpayer_type="一般纳税人",
            sales_amount=Decimal("1130000"),
            amount_includes_tax=True,
            business_type="销售货物",
            input_vat=Decimal("30000"),
        )
    )
    assert result.steps[0].title == "不含税销售额"
    assert result.steps[0].result == Decimal("1000000.00")


def test_vat_credit_balance_is_flagged(engine) -> None:
    """销项小于进项时不能算出负数，要提示留抵。"""

    result = engine.calc_vat(
        VatInput(
            taxpayer_type="一般纳税人",
            sales_amount=Decimal("100000"),
            business_type="销售货物",
            input_vat=Decimal("20000"),
        )
    )
    assert result.payable == Decimal("0.00")
    assert any("留抵" in note for note in result.notes)


def test_vat_requires_taxpayer_type(engine) -> None:
    """主体不明时不给数字——算错方向会让用户按错误的数申报。"""

    result = engine.calc_vat(VatInput(sales_amount=Decimal("1000000")))
    assert result.payable == Decimal("0.00")
    assert result.steps == []
    assert any("纳税人身份" in note for note in result.notes)


def test_vat_unknown_rate_asks_instead_of_guessing(engine) -> None:
    result = engine.calc_vat(
        VatInput(taxpayer_type="一般纳税人", sales_amount=Decimal("1000000"), business_type="说不清")
    )
    assert result.payable == Decimal("0.00")
    assert not result.steps
    assert any("适用税率" in note for note in result.notes)


def test_vat_no_float_error(engine) -> None:
    """用 Decimal 而不是 float：0.1 + 0.2 这类误差在财税场景不可接受。"""

    result = engine.calc_vat(
        VatInput(taxpayer_type="一般纳税人", sales_amount=Decimal("0.10"), rate=Decimal("0.13"))
    )
    assert result.payable == Decimal("0.01")


# ---------------------------------------------------------------------------
# 附加税费：10 道
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "payload", "expected"),
    [
        ("市区，增值税10万，一般纳税人", dict(vat_payable="100000", location="市区"), "12000.00"),
        ("县城，增值税10万", dict(vat_payable="100000", location="县城、镇"), "10000.00"),
        ("其他地区，增值税10万", dict(vat_payable="100000", location="其他"), "6000.00"),
        ("市区，增值税+消费税共10万", dict(vat_payable="80000", consumption_tax_payable="20000", location="市区"), "12000.00"),
        ("市区，增值税为零", dict(vat_payable="0", location="市区"), "0.00"),
        ("小规模减半（市区）", dict(vat_payable="100000", location="市区", taxpayer_type="小规模纳税人"), "6000.00"),
        ("个体工商户减半（县城）", dict(vat_payable="100000", location="县城、镇", taxpayer_type="个体工商户"), "5000.00"),
        ("一般纳税人不减半（对照）", dict(vat_payable="100000", location="市区", taxpayer_type="一般纳税人"), "12000.00"),
        ("市区，增值税3000（小规模减半）", dict(vat_payable="3000", location="市区", taxpayer_type="小规模纳税人"), "180.00"),
        ("地区未确认→按最低档并提示", dict(vat_payable="100000"), "6000.00"),
    ],
)
def test_surcharge_cases(engine, name: str, payload: dict, expected: str) -> None:
    result = engine.calc_surcharges(SurchargeInput(**payload))
    assert str(result.payable) == expected, f"{name}：期望 {expected}，实际 {result.payable}"


def test_surcharge_shows_base_and_items(engine) -> None:
    """附加税费的计税依据是"实际缴纳的增值税+消费税"，不是销售额——这一步必须显示。"""

    result = engine.calc_surcharges(SurchargeInput(vat_payable=Decimal("100000"), location="市区"))
    assert result.steps[0].title == "计税依据"
    assert result.steps[0].result == Decimal("100000.00")
    names = [step.title for step in result.steps]
    assert "城市维护建设税" in names
    assert "教育费附加" in names
    assert "地方教育附加" in names


def test_surcharge_unknown_location_falls_back_with_warning(engine) -> None:
    """地区没确认时用最低档并明确提示，不静默按 7% 算。"""

    result = engine.calc_surcharges(SurchargeInput(vat_payable=Decimal("100000"), location="某个说不清的地方"))
    assert any("纳税人所在地" in note for note in result.notes)


def test_surcharge_discount_requires_taxpayer_type(engine) -> None:
    """主体不明确时不能自动套用优惠——优惠算错方向就是少缴税。"""

    result = engine.calc_surcharges(SurchargeInput(vat_payable=Decimal("100000"), location="市区"))
    assert result.discount_applied is None
    assert result.payable == Decimal("12000.00")


# ---------------------------------------------------------------------------
# 规则加载：配置写错必须在加载期就拦住
# ---------------------------------------------------------------------------


def test_rules_load_from_domain_pack() -> None:
    import pathlib

    from app.calculation import load_rules

    repo = pathlib.Path(__file__).resolve().parents[2]
    rules = load_rules(repo / "domains" / "finance_tax" / "tax_rules.yaml")
    assert rules.domain_id == "finance_tax"
    assert rules.rules_version
    assert len(rules.vat_rates()) >= 4
    assert rules.surcharge_item("urban_maintenance")["name"] == "城市维护建设税"


def test_bad_rate_is_rejected(tmp_path) -> None:
    """税率写成 13（而不是 13%）要当场报错，不能等算错了才发现。"""

    from app.calculation import load_rules
    from app.calculation.rules import RuleSetError

    broken = tmp_path / "bad.yaml"
    broken.write_text(
        """
domain_id: finance_tax
rules_version: "test"
as_of: "2026-01-01"
vat:
  methods:
    general: {name: 一般计税, formula: f, citation: c}
    simplified: {name: 简易计税, formula: f, citation: c}
  rates:
    - rate: "13"
      name: 错误写法
      citation: 某条
  levy_rates:
    - rate: "0.03"
      name: 征收率
      citation: 某条
surcharges:
  items:
    - code: x
      name: X
      rate: "0.01"
      citation: 某条
""",
        encoding="utf-8",
    )
    with pytest.raises(RuleSetError):
        load_rules(broken)


def test_rate_without_citation_is_rejected(tmp_path) -> None:
    """税率必须写明依据条款——没有依据的计算结果不能给用户。"""

    from app.calculation import load_rules
    from app.calculation.rules import RuleSetError

    broken = tmp_path / "no_citation.yaml"
    broken.write_text(
        """
domain_id: finance_tax
rules_version: "test"
as_of: "2026-01-01"
vat:
  methods:
    general: {name: 一般计税, formula: f, citation: c}
    simplified: {name: 简易计税, formula: f, citation: c}
  rates:
    - rate: "0.13"
      name: 没有依据
  levy_rates:
    - rate: "0.03"
      name: 征收率
      citation: 某条
surcharges:
  items:
    - code: x
      name: X
      rate: "0.01"
      citation: 某条
""",
        encoding="utf-8",
    )
    with pytest.raises(RuleSetError):
        load_rules(broken)


def test_money_helpers() -> None:
    """金额工具的口径：四舍五入到分、税率三种写法都能吃。"""

    assert yuan("1.005") == Decimal("1.01")
    assert yuan("1.004") == Decimal("1.00")
    assert to_rate("13%") == Decimal("0.13")
    assert to_rate("0.13") == Decimal("0.13")
    assert to_rate(13) == Decimal("0.13")
