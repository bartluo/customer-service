"""合规验证器。

查两件事：
  1. **免责声明是否齐全**。删掉免责段的答案直接阻断——
     它是模板强制渲染的，如果它没了，说明答案不是从模板出来的，
     或者被中途改过，两种情况都不能发给用户。
  2. **有没有越界表述**。财税服务不能替税务机关下结论、不能承诺结果、
     不能给出违法操作建议。这类表述一旦出现必须拦下来。

为什么"越界表述"用规则而不是模型判：
  这类词是有限的、已知的（"保证""一定可以""包过""绝对"），
  规则表可审计、可维护、零成本、结果可复现。
"""

from __future__ import annotations

import re

from app.verification.result import Issue, Severity, VerificationInput, VerifierResult

# 越界表述：替税务机关下定论、承诺结果、暗示可以规避监管
_OVERREACH_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"保证(?:可以|能|通过|成功)", "promise_result", "承诺结果（“保证…”）"),
    (r"一定能(?:通过|成功)|绝对(?:可以|能|没问题)", "absolute_claim", "绝对化表述（“一定能/绝对…”）"),
    (r"包(?:过|通过|搞定)", "guarantee_pass", "承诺包过"),
    (r"不用交税|可以不交税|无需缴纳", "evasion_hint", "可能被理解为规避纳税义务（“不用交税”）"),
    (r"税务机关(?:一定|肯定|必然)(?:会|不会)", "speak_for_authority", "替税务机关下结论"),
    (r"最终(?:以|按)我们(?:说的|为准)", "override_authority", "凌驾于主管机关认定之上"),
)


class ComplianceVerifier:
    """检查免责声明与表述边界。"""

    name = "合规验证器"

    def verify(self, payload: VerificationInput) -> VerifierResult:
        result = VerifierResult(name=self.name)
        text = self._flatten(payload.sections)

        # ① 免责声明
        result.checked += 1
        if "disclaimer" not in payload.sections:
            result.issues.append(
                Issue(
                    verifier="compliance",
                    severity=Severity.BLOCKING,
                    code="disclaimer_missing",
                    message="答案缺少免责说明段。免责声明由模板强制渲染，缺了就说明答案不是按模板生成的",
                    target="disclaimer",
                )
            )
        else:
            disclaimer = str(payload.sections.get("disclaimer") or "")
            if "税务机关" not in disclaimer and "以主管" not in disclaimer:
                result.issues.append(
                    Issue(
                        verifier="compliance",
                        severity=Severity.BLOCKING,
                        code="disclaimer_incomplete",
                        message="免责说明没有包含“以主管税务机关认定为准”这类必要表述",
                        target=disclaimer[:40],
                    )
                )

        # ② 越界表述
        for pattern, code, label in _OVERREACH_PATTERNS:
            match = re.search(pattern, text)
            if match:
                result.checked += 1
                result.issues.append(
                    Issue(
                        verifier="compliance",
                        severity=Severity.BLOCKING,
                        code=code,
                        message=f"答案出现越界表述：{label}（原文“{match.group(0)}”）",
                        target=match.group(0),
                    )
                )
        return result

    @staticmethod
    def _flatten(sections: dict) -> str:
        parts: list[str] = []
        for value in sections.values():
            if isinstance(value, list):
                for item in value:
                    parts.append(item.get("text", str(item)) if isinstance(item, dict) else str(item))
            else:
                parts.append(str(value))
        return " ".join(parts)
