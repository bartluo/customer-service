"""计算结果的数据结构。

为什么不是"返回一个金额"：
  财税场景用户要的是"这个数怎么来的"。答案模板里有一段"计算过程"，
  前端要分步展示（公式 → 代入 → 结果），审核专家也要能逐行核对。
  所以引擎的返回值是一串步骤，不是单个数字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class CalcStep:
    """一个计算步骤：公式 + 代入 + 结果 + 依据。"""

    title: str  # 步骤名，如"销项税额"
    formula: str  # 公式，如"不含税销售额 × 适用税率"
    substitution: str  # 代入，如"1,000,000.00 × 13%"
    result: Decimal
    citation: str = ""  # 依据条款

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "formula": self.formula,
            "substitution": self.substitution,
            "result": str(self.result),
            "citation": self.citation,
        }


@dataclass
class CalcResult:
    """一次计算的完整结果。"""

    tax_type: str  # vat / surcharges
    method: str = ""  # general / simplified
    payable: Decimal = Decimal("0.00")  # 应纳税额
    steps: list[CalcStep] = field(default_factory=list)
    # 边界提示与待确认事项。临界值、优惠是否适用、口径不确定都放这里。
    notes: list[str] = field(default_factory=list)
    # 用到了哪些条款
    citations: list[str] = field(default_factory=list)
    # 优惠是否已计入
    discount_applied: str | None = None
    # 本次计算实际套用了哪些优惠政策（名称 + 依据条款）。
    # 为什么要单独留一份：用户要的是"先看政策再按政策算"，
    # 答案里必须能说清"这 0 元是怎么来的、依据哪一条"。
    preferences: list[dict] = field(default_factory=list)
    # 规则版本：事后能追溯"当时的答案是按哪版规则算的"
    rule_version: str = ""

    def add_step(
        self,
        title: str,
        formula: str,
        substitution: str,
        result: Decimal,
        citation: str = "",
    ) -> CalcStep:
        step = CalcStep(title, formula, substitution, result, citation)
        self.steps.append(step)
        if citation and citation not in self.citations:
            self.citations.append(citation)
        return step

    def to_dict(self) -> dict:
        return {
            "tax_type": self.tax_type,
            "method": self.method,
            "payable": str(self.payable),
            "steps": [step.to_dict() for step in self.steps],
            "notes": list(self.notes),
            "citations": list(self.citations),
            "discount_applied": self.discount_applied,
            "preferences": list(self.preferences),
            "rule_version": self.rule_version,
        }

    def explain(self) -> str:
        """给命令行/日志看的文字版过程。"""

        lines = [
            f"税种：{self.tax_type}" + (f"（{self.method}）" if self.method else ""),
            f"规则版本：{self.rule_version}",
        ]
        for index, step in enumerate(self.steps, start=1):
            from app.calculation.money import money_text

            lines.append(f"  {index}. {step.title}：{step.formula}")
            lines.append(f"     代入 {step.substitution} = {money_text(step.result)}")
            if step.citation:
                lines.append(f"     依据：{step.citation}")
        if self.discount_applied:
            lines.append(f"已适用优惠：{self.discount_applied}")
        lines.append(f"应纳税额：{money_text(self.payable)}")
        for note in self.notes:
            lines.append(f"提示：{note}")
        return "\n".join(lines)
