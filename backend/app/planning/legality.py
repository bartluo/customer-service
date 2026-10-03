"""四道合法性检查 + 风险分级。

技术方案 4.12：每个候选方案在输出前必须通过四道检查，**任何一道不过，方案直接作废**。

| 检查 | 内容 | 实现 |
|---|---|---|
| ① 依据检查 | 有条款级依据吗？现行有效吗？ | 复用第 6 章的引用验证器 + 时效验证器 |
| ② 红线检查 | 触碰红线清单了吗？ | 规则匹配（redlines.py） |
| ③ 反避税检查 | 有合理商业目的吗？可能被特别纳税调整吗？ | 三道问句 + 用户填写，不替用户编 |
| ④ 实质检查 | 有真实业务实质吗？有可提供的凭证吗？ | 要求用户确认真实性 + 列证据清单 |

关于第 ③ 道：**系统不替用户"编"商业目的**。
用户提供不了真实的商业理由，这件事本身就是风险信号——
所以这一项空着不是"跳过检查"，而是判为风险。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.planning.profile import CompanyProfile
from app.planning.redlines import RedLineHit, RedLineMatcher
from app.verification import TemporalVerifier, VerificationInput
from app.verification.citation import CitationVerifier


@dataclass
class CheckResult:
    """一道检查的结果。"""

    code: str
    name: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    # 不通过时是否"完全作废"（红线），还是"降级 + 提示"
    fatal: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "passed": self.passed,
            "fatal": self.fatal,
            "reasons": list(self.reasons),
        }


@dataclass
class LegalityReport:
    """四道检查的汇总。"""

    checks: list[CheckResult] = field(default_factory=list)
    red_line_hits: list[RedLineHit] = field(default_factory=list)
    risk_level: str = "green"
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.checks)

    @property
    def fatal(self) -> bool:
        """是否触发一票否决（红线或依据缺失）。"""

        return any(item.fatal and not item.passed for item in self.checks)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "fatal": self.fatal,
            "risk_level": self.risk_level,
            "reasons": list(self.reasons),
            "red_lines": [hit.to_dict() for hit in self.red_line_hits],
            "checks": [item.to_dict() for item in self.checks],
        }

    def explain(self) -> str:
        lines = [f"合法性审查：{'通过' if self.passed else '未通过'}　风险等级：{self.risk_level}"]
        for item in self.checks:
            flag = "通过" if item.passed else ("作废" if item.fatal else "降级")
            lines.append(f"  · {item.name}：{flag}")
            for reason in item.reasons:
                lines.append(f"      {reason}")
        return "\n".join(lines)


class LegalityChecker:
    """对候选方案跑四道检查。"""

    def __init__(self, session: Session | None = None, *, matcher: RedLineMatcher | None = None):
        self.session = session
        self.matcher = matcher or RedLineMatcher()

    # ------------------------------------------------------------------
    def check(
        self,
        profile: CompanyProfile,
        *,
        request_text: str = "",
        plan_text: str = "",
        citations: list[dict] | None = None,
        as_of: datetime | None = None,
    ) -> LegalityReport:
        report = LegalityReport()
        citations = citations or []
        text = " ".join(part for part in (request_text, plan_text) if part)

        report.red_line_hits = self.matcher.match(text)
        report.checks.append(self._check_basis(citations, as_of))
        report.checks.append(self._check_red_lines(report.red_line_hits))
        report.checks.append(self._check_anti_avoidance(profile))
        report.checks.append(self._check_substance(profile))

        report.risk_level = self._grade(profile, report)
        report.reasons = [
            reason for item in report.checks if not item.passed for reason in item.reasons
        ]
        return report

    # ------------------------------------------------------------------
    def _check_basis(self, citations: list[dict], as_of: datetime | None) -> CheckResult:
        """① 依据检查：有没有条款级依据，依据是不是现行有效。"""

        result = CheckResult(code="basis", name="依据检查", passed=True, fatal=True)
        if not citations:
            result.passed = False
            result.reasons.append("方案没有任何条款级依据——没有依据的方案不能给用户")
            return result
        if self.session is None:
            return result  # 没连库时只做数量检查

        payload = VerificationInput(citations=citations, as_of=as_of)
        citation_issues = CitationVerifier(self.session).verify(payload).issues
        temporal_issues = TemporalVerifier().verify(payload).issues
        issues = citation_issues + temporal_issues
        if issues:
            result.passed = False
            result.reasons.extend(issue.message for issue in issues)
        return result

    def _check_red_lines(self, hits: list[RedLineHit]) -> CheckResult:
        """② 红线检查：命中即拒绝。"""

        result = CheckResult(code="red_line", name="红线检查", passed=not hits, fatal=True)
        for hit in hits:
            result.reasons.append(hit.explain())
        return result

    def _check_anti_avoidance(self, profile: CompanyProfile) -> CheckResult:
        """③ 反避税检查：三道问句。用户答不上来就是风险信号。"""

        result = CheckResult(code="anti_avoidance", name="反避税检查", passed=True)
        if not profile.business_purpose:
            result.passed = False
            result.reasons.append(
                "未填写商业理由。需要说明“如果完全没有税收利益，这项安排还会不会做”——"
                "这是判断合理商业目的的第一道问句，空着本身就是风险信号"
            )
        if not profile.business_benefit:
            result.passed = False
            result.reasons.append("未说明这项安排带来的真实经营效益（成本下降、效率提升、市场拓展等）")
        if not profile.evidence_available:
            result.passed = False
            result.reasons.append("未列出可验证的业务证据（合同、履约记录、人员、场地、资金流）")
        return result

    def _check_substance(self, profile: CompanyProfile) -> CheckResult:
        """④ 实质检查：用户必须确认真实业务实质。"""

        result = CheckResult(code="substance", name="实质检查", passed=True, fatal=True)
        if profile.business_authentic is not True:
            result.passed = False
            result.reasons.append(
                "未确认业务真实性。系统不能替用户确认业务是否真实，"
                "这一项不确认就不能出方案"
            )
        return result

    # ------------------------------------------------------------------
    def _grade(self, profile: CompanyProfile, report: LegalityReport) -> str:
        """风险分级（技术方案 4.13）。

        分级规则（越保守越靠前，只要命中一条就按最高的算）：
          🔴 命中红线 / 依据不成立 / 业务真实性未确认
          🟡 反避税三项有缺项 / 风险偏好"进取"
          🟢 其余
        """

        if report.red_line_hits:
            return "red"
        for item in report.checks:
            if not item.passed and item.fatal:
                return "red"
        anti = next((item for item in report.checks if item.code == "anti_avoidance"), None)
        if anti is not None and not anti.passed:
            return "yellow"
        if profile.risk_preference == "进取":
            return "yellow"
        return "green"
