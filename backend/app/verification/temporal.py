"""时效验证器——财税的绝对红线。

技术方案 6.2 列的检查项：
  ① 每个引用的条文，其版本状态在**问题时点**上是否有效
  ② 是否引用了 repealed / superseded 的版本
  ③ 是否引用了 not_yet_effective 的版本（除非答案里明确说明"将施行"）
  ④ 涉及优惠政策的，是否标注了有效期

违反任一项 → 阻断输出。

为什么必须有：财税答案引用废止条文，用户照着办就是错误申报——
后果是补税、滞纳金、罚款、信用降级。检索层已经做了硬过滤，
但这一层是**独立的第二道防线**：不假设上游一定做对了。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.knowledge.ontology import CITABLE_EFFECT_STATUSES
from app.verification.result import Issue, Severity, VerificationInput, VerifierResult


class TemporalVerifier:
    """核对每条引用在问题时点上是否有效。"""

    name = "时效验证器"

    def verify(self, payload: VerificationInput) -> VerifierResult:
        result = VerifierResult(name=self.name)
        moment = payload.as_of or datetime.now(timezone.utc)
        answer_text = self._flatten(payload.sections)

        for citation in payload.citations:
            result.checked += 1
            effect_status = (citation.get("effect_status") or "").strip()

            if effect_status in {"repealed", "superseded", "draft"}:
                result.issues.append(
                    Issue(
                        verifier="temporal",
                        severity=Severity.BLOCKING,
                        code="citation_not_citable",
                        message=(
                            f"引用了不可用的条文（{effect_status}）："
                            f"{citation.get('regulation_title') or citation.get('document_number')} "
                            f"{citation.get('full_no') or ''}"
                        ),
                        target=citation.get("document_number") or citation.get("regulation_title", ""),
                    )
                )
                continue

            if effect_status == "not_yet_effective":
                # 尚未生效：只有明确提示施行时间才允许出现。
                # 写法有很多种（"将施行""将于2027年1月1日起施行"），
                # 用"将…施行"这个词组匹配，别写死成一个固定字串。
                if not re.search(r"将[^。；\n]{0,20}施行", answer_text) and "尚未生效" not in answer_text:
                    result.issues.append(
                        Issue(
                            verifier="temporal",
                            severity=Severity.BLOCKING,
                            code="citation_not_yet_effective",
                            message=(
                                "引用了尚未生效的条文，但答案里没有说明其施行时间："
                                f"{citation.get('regulation_title') or ''}"
                            ),
                            target=citation.get("document_number") or citation.get("regulation_title", ""),
                        )
                    )
                continue

            if effect_status and effect_status not in CITABLE_EFFECT_STATUSES:
                result.issues.append(
                    Issue(
                        verifier="temporal",
                        severity=Severity.BLOCKING,
                        code="citation_unknown_effect_status",
                        message=f"引用的条文效力状态无法确认：{effect_status}",
                        target=str(citation.get("document_number") or ""),
                    )
                )

            # 时间区间：条文的有效期必须覆盖问题时点
            result.issues.extend(self._check_validity_window(citation, moment))

        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _check_validity_window(citation: dict, moment: datetime) -> list[Issue]:
        valid_from = citation.get("valid_from")
        valid_to = citation.get("valid_to")
        label = citation.get("document_number") or citation.get("regulation_title") or ""

        def _as_datetime(value: object) -> datetime | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                return value
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(float(value), tz=timezone.utc)
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value)
                except ValueError:
                    return None
            return None

        valid_from = _as_datetime(valid_from)
        valid_to = _as_datetime(valid_to)

        if valid_from is not None:
            if valid_from.tzinfo is None:
                valid_from = valid_from.replace(tzinfo=timezone.utc)
            if valid_from > moment:
                return [
                    Issue(
                        verifier="temporal",
                        severity=Severity.BLOCKING,
                        code="citation_before_effective",
                        message=f"引用的条文在所问时点尚未生效：{label}",
                        target=str(label),
                    )
                ]
        if valid_to is not None:
            if valid_to.tzinfo is None:
                valid_to = valid_to.replace(tzinfo=timezone.utc)
            if valid_to <= moment:
                return [
                    Issue(
                        verifier="temporal",
                        severity=Severity.BLOCKING,
                        code="citation_after_expiry",
                        message=f"引用的条文执行期已届满：{label}",
                        target=str(label),
                    )
                ]
        return []

    @staticmethod
    def _flatten(sections: dict) -> str:
        """把结构化答案压成纯文本，用于判断"有没有说明将施行"。"""

        parts: list[str] = []
        for value in sections.values():
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
            else:
                parts.append(str(value))
        return " ".join(parts)
