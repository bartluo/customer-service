"""筹划空间生成、量化测算与多方案对比。

三段对应技术方案 4.10 流水线的第 ②④⑥ 段：

  ② 筹划空间生成（三路来源）
     路径 A 政策适用型 —— 可享受但未享受的税收优惠
     路径 B 结构与安排型 —— 主体架构 / 业务模式 / 资产投资 / 区域
     路径 C 时点与节奏型 —— 收入确认、费用归属、扣除时点
  ④ 量化测算 —— **必须调用计算引擎**，不允许模型估金额
  ⑥ 多方案对比输出 —— 对比表 + 落地步骤 + 材料清单 + 组合建议

一条硬约束贯穿全篇：**测算算不出来就说算不出来**。
宁愿在对比表里标"需补充数据"，也不能给一个看起来精确、其实靠猜的数字——
用户会拿这个数去决策。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.calculation import get_engine
from app.calculation.engine import SurchargeInput, VatInput
from app.models.planning import PlanningTechnique
from app.planning.profile import CompanyProfile
from app.planning.techniques import TechniqueLibrary, TechniqueMatch

# 手法类别 → 生成路径。技术方案 4.10 的三条路径。
PATH_BY_CATEGORY: dict[str, str] = {
    "政策适用型": "A",
    "主体架构型": "B",
    "业务模式型": "B",
    "资产与投资型": "B",
    "区域型": "B",
    "时点安排型": "C",
    "递延型": "C",
}

PATH_NAMES = {"A": "政策适用型", "B": "结构与安排型", "C": "时点与节奏型"}

# 实施成本 → 排序权重（对比表里要能按"性价比"排）
COST_ORDER = {"低": 1, "中": 2, "高": 3}


@dataclass
class Measurement:
    """一个方案的量化测算结果。"""

    computable: bool = False
    before: Decimal | None = None  # 筹划前税负
    after: Decimal | None = None  # 筹划后税负
    saving: Decimal | None = None  # 节税额
    tax_type: str = ""
    steps: list[dict] = field(default_factory=list)
    note: str = ""  # 不可测算时的原因

    def to_dict(self) -> dict:
        def money(value: Decimal | None) -> str | None:
            return str(value) if value is not None else None

        return {
            "computable": self.computable,
            "tax_type": self.tax_type,
            "before": money(self.before),
            "after": money(self.after),
            "saving": money(self.saving),
            "steps": list(self.steps),
            "note": self.note,
        }


@dataclass
class CandidatePlan:
    """一个候选筹划方案（输出单位）。"""

    code: str
    name: str
    path: str  # A / B / C
    category: str
    risk_level: str
    mechanism: str = ""
    actions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    cost: str = ""
    citations: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    abuse_boundary: list[str] = field(default_factory=list)
    rejected_cases: list[str] = field(default_factory=list)
    measurement: Measurement = field(default_factory=Measurement)
    # 信息不足时缺什么（对应手法匹配的第三态）
    missing: list[str] = field(default_factory=list)

    @property
    def path_name(self) -> str:
        return PATH_NAMES.get(self.path, self.path)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "path": self.path,
            "path_name": self.path_name,
            "category": self.category,
            "risk_level": self.risk_level,
            "mechanism": self.mechanism,
            "actions": list(self.actions),
            "steps": list(self.steps),
            "evidence": list(self.evidence),
            "cost": self.cost,
            "citations": list(self.citations),
            "preconditions": list(self.preconditions),
            "abuse_boundary": list(self.abuse_boundary),
            "rejected_cases": list(self.rejected_cases),
            "measurement": self.measurement.to_dict(),
            "missing": list(self.missing),
        }


class PlanningSpaceGenerator:
    """三路生成候选方案，并做量化测算。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        # 出方案只认专家确认过的手法。draft 素材可以用于内部推演，
        # 但要在方案上标出来——否则"示例素材"会变成对客户的承诺。
        self.library = TechniqueLibrary(session, include_draft=True)
        self.engine = get_engine()

    # ------------------------------------------------------------------
    def generate(
        self, profile: CompanyProfile, *, include_unconfirmed: bool = True
    ) -> list[CandidatePlan]:
        """生成候选方案。

        只输出"适用"与"信息不足"两类：
          · 适用 → 正常出方案；
          · 信息不足 → **也出方案**，但把缺什么写清楚。
            （筹划里"信息不足"太常见了，直接丢掉等于什么都没给；
              标出来用户才知道补什么。）
          · 不适用 → 不出。
        """

        plans: list[CandidatePlan] = []
        for match in self.library.match(profile):
            if match.matched is False:
                continue
            if not include_unconfirmed and match.technique.review_state != "published":
                continue
            plans.append(self._to_plan(match, profile))
        # 有测算的排前面，同等情况风险低的排前面（对比表读起来更顺）
        plans.sort(key=lambda item: (not item.measurement.computable, COST_ORDER.get(item.cost, 9)))
        return plans

    # ------------------------------------------------------------------
    def _to_plan(self, match: TechniqueMatch, profile: CompanyProfile) -> CandidatePlan:
        technique: PlanningTechnique = match.technique
        playbook = technique.playbook or {}
        plan = CandidatePlan(
            code=technique.code,
            name=technique.name,
            path=PATH_BY_CATEGORY.get(technique.category, "B"),
            category=technique.category,
            risk_level=technique.risk_level,
            mechanism=technique.mechanism,
            actions=list(playbook.get("actions") or []),
            steps=list(playbook.get("steps") or []),
            evidence=list(playbook.get("evidence") or []),
            cost=playbook.get("cost", ""),
            citations=list(technique.citations or []),
            preconditions=[item.description for item in match.checks],
            abuse_boundary=list(technique.abuse_boundary or []),
            rejected_cases=list(technique.rejected_cases or []),
            missing=match.missing,
        )
        plan.measurement = self.measure(plan, technique, profile)
        return plan

    # ------------------------------------------------------------------
    def measure(
        self, plan: CandidatePlan, technique: PlanningTechnique, profile: CompanyProfile
    ) -> Measurement:
        """量化测算：筹划前税负 → 筹划后税负 → 节税额。

        **数字全部来自计算引擎。** 算不出来就明说为什么——
        例如"加计扣除需要研发费用金额，画像里没有"。
        """

        spec = technique.measure or {}
        kind = spec.get("kind")
        if not kind:
            return Measurement(note="该手法未配置可量化测算口径，需人工测算")

        if kind == "exempt":
            return self._measure_vat_exempt(profile)
        if kind == "halve":
            return self._measure_surcharge_halve(profile)
        if kind == "small_low_profit":
            return self._measure_small_low_profit(profile)
        if kind in {"rd_super_deduction", "equipment_one_off"}:
            return Measurement(
                note=(
                    "该优惠按实际发生额加计/扣除，画像里没有相应金额，"
                    "需补充研发费用或设备购置金额后才能测算"
                )
            )
        return Measurement(note=f"暂不支持的测算口径：{kind}")

    def _measure_vat_exempt(self, profile: CompanyProfile) -> Measurement:
        amount = profile.annual_revenue or profile.revenue
        if amount is None:
            return Measurement(note="缺销售额，无法测算增值税免税效果")
        before = self.engine.calc_vat(
            VatInput(
                taxpayer_type=profile.taxpayer_type,
                business_type=profile.main_income_source or profile.business_model,
                sales_amount=amount,
            )
        )
        before_payable = before.payable
        result = Measurement(
            computable=True,
            before=before_payable,
            after=Decimal("0.00"),
            saving=before_payable,
            tax_type="增值税",
            steps=[step.to_dict() for step in before.steps],
            note="按确实未达起征点、且全部为普通发票业务测算；开具专用发票的部分不适用免税",
        )
        return result

    def _measure_surcharge_halve(self, profile: CompanyProfile) -> Measurement:
        amount = profile.annual_revenue or profile.revenue
        if amount is None:
            return Measurement(note="缺销售额，无法测算附加税费减半效果")
        vat = self.engine.calc_vat(
            VatInput(
                taxpayer_type=profile.taxpayer_type,
                business_type=profile.main_income_source or profile.business_model,
                sales_amount=amount,
            )
        )
        if vat.payable <= 0:
            return Measurement(note="按现有数据测算增值税为 0，附加税费也随之不产生")
        full = self.engine.calc_surcharges(
            SurchargeInput(vat_payable=vat.payable, location=profile.region)
        )
        halved = self.engine.calc_surcharges(
            SurchargeInput(
                vat_payable=vat.payable, location=profile.region, taxpayer_type="小规模纳税人"
            )
        )
        return Measurement(
            computable=True,
            before=full.payable,
            after=halved.payable,
            saving=full.payable - halved.payable,
            tax_type="附加税费",
            steps=[step.to_dict() for step in halved.steps],
            note="按小规模纳税人减半口径测算；其他减免是否可叠加需按当地口径确认",
        )

    def _measure_small_low_profit(self, profile: CompanyProfile) -> Measurement:
        income = profile.taxable_income or profile.profit
        if income is None:
            return Measurement(note="缺应纳税所得额，无法测算小微优惠效果")
        normal = self.engine.calc_cit(income, small_low_profit=False)
        discounted = self.engine.calc_cit(income, small_low_profit=True)
        return Measurement(
            computable=True,
            before=normal.payable,
            after=discounted.payable,
            saving=normal.payable - discounted.payable,
            tax_type="企业所得税",
            steps=[step.to_dict() for step in discounted.steps],
            note="需同时满足年应纳税所得额 ≤300 万、从业人数 ≤300 人、资产总额 ≤5000 万",
        )


def build_comparison(plans: list[CandidatePlan]) -> dict:
    """多方案对比表（技术方案 4.13 的形态）。

    对比表要回答的是"我该先做哪个"，所以除了效果，还要有风险、成本、建议，
    以及**组合提示**（哪些能叠加、哪些互斥）——只给一个方案清单等于没帮用户做决策。
    """

    rows: list[dict[str, Any]] = []
    for plan in plans:
        measurement = plan.measurement
        rows.append(
            {
                "code": plan.code,
                "name": plan.name,
                "path": plan.path,
                "path_name": plan.path_name,
                "saving": str(measurement.saving) if measurement.saving is not None else None,
                "saving_text": (
                    f"约 {measurement.saving} 元/年"
                    if measurement.saving is not None
                    else f"待补数据（{measurement.note}）"
                ),
                "risk_level": plan.risk_level,
                "cost": plan.cost or "未评估",
                "citations": list(plan.citations),
                "preconditions": list(plan.preconditions),
                "advice": _advice_for(plan),
                "missing": list(plan.missing),
            }
        )
    # 按节税额从高到低，未测算的放最后
    rows.sort(key=lambda row: (row["saving"] is None, _neg(row["saving"])))
    return {
        "rows": rows,
        "combinations": _combination_advice(plans),
    }


def _neg(value: str | None) -> Decimal:
    """排序辅助：节税额大的排前面。"""

    try:
        return -Decimal(value or "0")
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _advice_for(plan: CandidatePlan) -> str:
    if plan.missing:
        return "先补齐所需信息后再评估"
    if plan.risk_level == "green" and plan.cost == "低":
        return "推荐优先实施"
    if plan.risk_level == "green":
        return "可实施，注意实施成本"
    if plan.risk_level == "yellow":
        return "建议先与主管税务机关沟通确认口径"
    return "高风险，仅进专家复核，不直接实施"


def _combination_advice(plans: list[CandidatePlan]) -> list[str]:
    """组合提示。"""

    notes: list[str] = []
    categories = {plan.category for plan in plans}
    if "政策适用型" in categories and "时点安排型" in categories:
        notes.append("政策适用型与合规的时点安排可以叠加：先确认优惠资格，再优化确认时点")
    if "主体架构型" in categories:
        notes.append("主体架构调整影响面最大，建议在其他方案落地且稳定运行后再评估")
    if "区域型" in categories:
        notes.append("区域型方案通常与主体架构型互斥或强相关，需一并评估，不要分开实施")
    notes.append("所有方案的实际适用口径以主管税务机关认定为准；高风险方案须经专家复核")
    return notes
