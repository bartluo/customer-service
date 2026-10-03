"""验证编排与失败处理。

处理流程完全按技术方案 6.3：

    验证失败
       ├─ 可自动修正（数字笔误） → 修正后重新验证 → 通过则输出
       ├─ 需重新生成（引用了废止条款） → 带约束重走一次（最多 2 次）
       └─ 无法恢复 → 降级输出：说明"已找到政策但需人工确认"，附候选条款

**关键原则：宁可说"不确定"，也不输出未通过验证的结论。**

落成代码之后就是两条硬规则：
  · 有 blocking 问题 → 这条答案绝不出门；
  · 修正/重试之后仍然 blocking → 降级，并把"为什么不敢给结论"写清楚。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy.orm import Session

from app.verification.calculation import CalculationVerifier
from app.verification.citation import CitationVerifier
from app.verification.compliance import ComplianceVerifier
from app.verification.result import (
    Issue,
    Severity,
    VerificationInput,
    VerificationReport,
)
from app.verification.temporal import TemporalVerifier

logger = logging.getLogger(__name__)

MAX_RETRIES = 2  # 技术方案 6.3：最多重试 2 次

DEGRADED_JUDGEMENT = (
    "已找到与您的问题相关的政策，但这条答案没有通过自动验证（原因见下方），"
    "因此不直接给出结论。请核对下列依据，或转人工由审核专家确认。"
)


@dataclass
class VerificationOutcome:
    """验证 + 修复之后的最终交付内容。"""

    report: VerificationReport
    sections: dict = field(default_factory=dict)
    citations: list[dict] = field(default_factory=list)
    calculation: dict | None = None
    degraded: bool = False


class AnswerVerifier:
    """跑四类验证器，并按 7.3 处理失败。"""

    def __init__(self, session: Session, *, record: bool = True) -> None:
        self.session = session
        self.verifiers = (
            CitationVerifier(session),
            TemporalVerifier(),
            CalculationVerifier(),
            ComplianceVerifier(),
        )
        self.record = record

    # ------------------------------------------------------------------
    def verify(self, payload: VerificationInput) -> VerificationReport:
        report = VerificationReport()
        for verifier in self.verifiers:
            try:
                report.results.append(verifier.verify(payload))
            except Exception as exc:  # noqa: BLE001 - 单个验证器出错不能拖垮整条答案
                logger.exception("验证器 %s 执行失败", verifier.name)
                from app.verification.result import VerifierResult

                report.results.append(
                    VerifierResult(
                        name=verifier.name,
                        issues=[
                            Issue(
                                verifier=verifier.name,
                                severity=Severity.BLOCKING,
                                code="verifier_error",
                                message=f"验证器执行出错，无法确认答案是否可靠：{exc}",
                            )
                        ],
                    )
                )
        return report

    def verify_and_repair(
        self,
        payload: VerificationInput,
        *,
        rebuild: Callable[[VerificationInput], None] | None = None,
    ) -> VerificationOutcome:
        """验证 → 修正/重新生成 → 必要时降级。"""

        outcome = VerificationOutcome(
            report=VerificationReport(),
            sections=dict(payload.sections),
            citations=list(payload.citations),
            calculation=payload.calculation,
        )
        applied_fix = False

        for attempt in range(MAX_RETRIES + 1):
            report = self.verify(payload)
            outcome.report = report
            outcome.report.retries = attempt

            if report.passed:
                # 只存在可修正问题（例如数字与引擎不一致）→ 修正数字后重新验证
                fixable = [i for i in report.issues if i.severity is Severity.FIXABLE]
                if fixable and attempt < MAX_RETRIES:
                    self._apply_fixes(payload, fixable)
                    applied_fix = True
                    outcome.calculation = payload.calculation
                    outcome.sections = dict(payload.sections)
                    continue
                # 修正过就是 fixed（哪怕这一轮已经没有问题了）——
                # 不记下来的话，"答案被改过"这件事就丢了，复核时看不出差别。
                outcome.report.outcome = "fixed" if applied_fix else "passed"
                self._save(payload, outcome.report)
                return outcome

            # 有 blocking：能重新生成就重来一次（带约束）
            if rebuild is not None and attempt < MAX_RETRIES:
                dropped = self._drop_bad_citations(payload, report.blocking)
                if not dropped:
                    break  # 问题不在引用上，重生成也没用
                try:
                    rebuild(payload)
                except Exception:  # noqa: BLE001
                    logger.exception("重新生成失败")
                    break
                outcome.sections = dict(payload.sections)
                outcome.citations = list(payload.citations)
                outcome.calculation = payload.calculation
                continue
            break

        # 走到这里说明修不回来了 → 降级输出
        outcome.degraded = True
        outcome.report.outcome = "degraded"
        outcome.sections = self._degraded_sections(payload, outcome.report)
        self._save(payload, outcome.report)
        return outcome

    # ------------------------------------------------------------------
    @staticmethod
    def _apply_fixes(payload: VerificationInput, fixes: list[Issue]) -> None:
        """把可修正的问题就地改掉：数字用引擎复算的结果替换。"""

        if not payload.calculation:
            return
        fix_map = {issue.target: issue.fix for issue in fixes if issue.fix}
        for block_name in ("vat", "surcharges"):
            block = payload.calculation.get(block_name)
            if not isinstance(block, dict):
                continue
            for step in block.get("steps") or []:
                key = f"{'增值税' if block_name == 'vat' else '附加税费'}·{step.get('title')}"
                if key in fix_map:
                    step["result"] = fix_map[key]
            for label, key in (("增值税应纳税额", "vat"), ("附加税费合计", "surcharges")):
                if block_name == key and label in fix_map:
                    block["payable"] = fix_map[label]

        # 汇总段落也要跟着改，否则答案里两处数字自相矛盾
        sections = payload.sections
        if sections.get("calculation"):
            steps: list[dict] = []
            for block_name in ("vat", "surcharges"):
                block = payload.calculation.get(block_name) or {}
                steps.extend(block.get("steps") or [])
            if steps:
                sections["calculation"] = steps

    @staticmethod
    def _drop_bad_citations(payload: VerificationInput, blocking: list[Issue]) -> bool:
        """把验证不过的引用剔掉，返回是否真的剔掉了东西。"""

        targets = {issue.target for issue in blocking if issue.target}
        if not targets:
            return False
        before = len(payload.citations)
        payload.citations = [
            item
            for item in payload.citations
            if not (
                (item.get("document_number") or item.get("regulation_title") or "") in targets
                or f"{item.get('regulation_title')} {item.get('full_no')}".strip() in targets
            )
        ]
        return len(payload.citations) < before

    @staticmethod
    def _degraded_sections(payload: VerificationInput, report: VerificationReport) -> dict:
        reasons = "；".join(issue.message for issue in report.blocking[:3])
        return {
            "judgement": f"{DEGRADED_JUDGEMENT}\n未通过原因：{reasons}",
            "basis": [
                {"text": item.get("document_number") or item.get("regulation_title") or ""}
                for item in payload.citations
            ],
            "disclaimer": payload.sections.get("disclaimer", ""),
        }

    # ------------------------------------------------------------------
    def _save(self, payload: VerificationInput, report: VerificationReport) -> None:
        """写验证记录。写失败不能让答案发不出去。"""

        if not self.record:
            return
        try:
            from app.models.verification import VerificationRecord

            self.session.add(
                VerificationRecord(
                    question=payload.question[:2000],
                    outcome=report.outcome,
                    passed=report.passed,
                    retries=report.retries,
                    detail=report.to_dict(),
                    issue_codes=sorted({issue.code for issue in report.issues}),
                )
            )
            self.session.commit()
        except Exception:  # noqa: BLE001
            self.session.rollback()
            logger.exception("验证记录写入失败（不影响答案输出）")
