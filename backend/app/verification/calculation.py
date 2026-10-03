"""计算复算验证器。

检查答案里的数字与计算引擎复算的结果是否一致。

为什么需要它（而不是信任生成端）：
  计算引擎保证"引擎算的没错"，但**答案里的数字不一定来自引擎**——
  可能被模型改写、被中间步骤篡改、或者干脆是上一版答案的残留。
  这一层把答案里的数字重新算一遍，不一致就拦下来。

不一致属于"可自动修正"：直接用引擎的结果替换答案里的数字，
再重新验证一次。数字笔误没必要让用户等一次重新生成。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.calculation import get_engine
from app.calculation.engine import SurchargeInput, VatInput
from app.verification.result import Issue, Severity, VerificationInput, VerifierResult


class CalculationVerifier:
    """用计算引擎复算，与答案里的数字逐一比对。"""

    name = "计算复算验证器"

    def verify(self, payload: VerificationInput) -> VerifierResult:
        result = VerifierResult(name=self.name)
        calculation = payload.calculation
        context = payload.calculation_context
        if not calculation or not context:
            return result  # 没有计算内容，无需复算

        engine = get_engine()
        vat_block = calculation.get("vat") or {}
        if vat_block:
            result.checked += 1
            expected = engine.calc_vat(
                VatInput(
                    taxpayer_type=context.get("taxpayer_type"),
                    business_type=context.get("business_type"),
                    sales_amount=context.get("amount"),
                    amount_includes_tax=bool(context.get("amount_includes_tax")),
                    input_vat=context.get("input_vat"),
                    # 复算必须带上期间口径：免税额按月 10 万还是按季 30 万判断，
                    # 会直接改变应纳税额（复算漏了它就会把"免征"当成"算错"）。
                    period_scope=context.get("period_scope"),
                )
            )
            result.issues.extend(
                self._compare("增值税应纳税额", vat_block.get("payable"), expected.payable)
            )

            surcharge_block = calculation.get("surcharges") or {}
            if surcharge_block:
                result.checked += 1
                expected_surcharge = engine.calc_surcharges(
                    SurchargeInput(
                        vat_payable=expected.payable,
                        location=context.get("region"),
                        taxpayer_type=context.get("taxpayer_type"),
                    )
                )
                result.issues.extend(
                    self._compare(
                        "附加税费合计",
                        surcharge_block.get("payable"),
                        expected_surcharge.payable,
                    )
                )

                # 逐步复算：比对答案里每一步的数字
                expected_steps = {step.title: step.result for step in expected_surcharge.steps}
                for step in surcharge_block.get("steps") or []:
                    title = step.get("title")
                    if title in expected_steps:
                        result.checked += 1
                        result.issues.extend(
                            self._compare(
                                f"附加税费·{title}", step.get("result"), expected_steps[title]
                            )
                        )

            expected_vat_steps = {step.title: step.result for step in expected.steps}
            for step in vat_block.get("steps") or []:
                title = step.get("title")
                if title in expected_vat_steps:
                    result.checked += 1
                    result.issues.extend(
                        self._compare(f"增值税·{title}", step.get("result"), expected_vat_steps[title])
                    )
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _compare(label: str, actual: object, expected: Decimal) -> list[Issue]:
        try:
            value = Decimal(str(actual))
        except (InvalidOperation, TypeError):
            return [
                Issue(
                    verifier="calculation",
                    severity=Severity.BLOCKING,
                    code="calculation_value_missing",
                    message=f"{label}在答案里不是有效数字：{actual!r}",
                    target=label,
                )
            ]
        if value != expected:
            return [
                Issue(
                    verifier="calculation",
                    severity=Severity.FIXABLE,
                    code="calculation_mismatch",
                    message=f"{label}与复算结果不一致：答案写 {value}，复算为 {expected}",
                    target=label,
                    fix=str(expected),
                )
            ]
        return []
