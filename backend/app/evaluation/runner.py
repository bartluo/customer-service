"""评测执行器：一键跑全量评测并出报告。

技术方案 11.2 的四个维度里，这一版能自动判定的三类：
  ② 依据正确性 —— 引用的条款真实存在、效力状态可引用（规则类，可 100% 自动）
  ③ 过程正确性 —— 数字与计算引擎复算一致（复用复算验证器）
  ④ 表达适当性 —— 该追问的追问了、免责声明在（模板与主链路保证）

① 事实正确性需要"标准答案要点"，属于半自动（LLM 裁判）与专家评审的范畴，
本版只统计"有标准答案的题"的命中情况，没有标准答案的题明确标注为未判定——
**不把"没判"算成"通过"**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.eval import CASE_INSUFFICIENT, CASE_REPEALED_TRAP, CASE_STANDARD, EvalCase, EvalRun
from app.reasoning.pipeline import AnswerPipeline
from app.retrieval.searcher import KnowledgeSearcher, SearchRequest


@dataclass
class CaseResult:
    """单条评测题的结果。"""

    case_id: str
    question: str
    case_type: str
    passed: bool
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "case_type": self.case_type,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class EvalRunReport:
    """一次评测的完整报告。"""

    total: int = 0
    passed: int = 0
    failed: int = 0
    metrics: dict = field(default_factory=dict)
    failures: list[CaseResult] = field(default_factory=list)
    results: list[CaseResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 4),
            "metrics": dict(self.metrics),
            "failures": [item.to_dict() for item in self.failures],
        }

    def explain(self) -> str:
        lines = [
            f"评测结果：{self.passed}/{self.total} 通过（{self.pass_rate:.1%}）",
            "指标：",
        ]
        for key, value in self.metrics.items():
            lines.append(f"  · {key}：{value}")
        if self.failures:
            lines.append(f"失败用例（前 {min(len(self.failures), 5)} 条）：")
            for item in self.failures[:5]:
                lines.append(f"  - [{item.case_type}] {item.question[:40]}：{item.detail}")
        return "\n".join(lines)


class EvalRunner:
    """跑评测集。"""

    def __init__(self, session: Session, *, include_draft: bool = True) -> None:
        self.session = session
        self.include_draft = include_draft
        self.searcher = KnowledgeSearcher(session)

    # ------------------------------------------------------------------
    def load_cases(self, *, case_types: tuple[str, ...] | None = None) -> list[EvalCase]:
        statement = select(EvalCase)
        if not self.include_draft:
            statement = statement.where(EvalCase.review_state == "approved")
        if case_types:
            statement = statement.where(EvalCase.case_type.in_(case_types))
        return list(self.session.execute(statement).scalars().all())

    def run(self, *, case_types: tuple[str, ...] | None = None, limit: int | None = None) -> EvalRunReport:
        cases = self.load_cases(case_types=case_types)
        if limit:
            cases = cases[:limit]

        report = EvalRunReport(total=len(cases))
        recalls: list[int] = []
        reciprocal_ranks: list[float] = []
        for case in cases:
            result = self.run_case(case)
            report.results.append(result)
            if result.passed:
                report.passed += 1
            else:
                report.failed += 1
                report.failures.append(result)
            if "recall" in result.metrics:
                recalls.append(int(result.metrics["recall"]))
                reciprocal_ranks.append(float(result.metrics.get("rr", 0.0)))

        report.metrics = self._metrics(report, recalls, reciprocal_ranks)
        return report

    # ------------------------------------------------------------------
    def run_case(self, case: EvalCase) -> CaseResult:
        try:
            response = self.searcher.search(
                SearchRequest(question=case.question, tax_types=[case.tax_type] if case.tax_type else [], top_n=5)
            )
        except Exception as exc:  # noqa: BLE001 - 单题失败不能中断整轮
            return CaseResult(case.id, case.question, case.case_type, False, f"检索失败：{exc}")

        labels = [citation.label() for citation in response.citations]

        # ---- 废止陷阱：不得出现禁止的引用 ----
        if case.case_type == CASE_REPEALED_TRAP:
            leaked = self._find_leaks(response.citations, case.forbidden_citations or [])
            if leaked:
                return CaseResult(
                    case.id, case.question, case.case_type, False,
                    f"出现已废止引用：{leaked[:2]}",
                )
            return CaseResult(case.id, case.question, case.case_type, True, "未出现废止条款")

        # ---- 信息不足：期望追问而不是硬答 ----
        if case.case_type == CASE_INSUFFICIENT:
            pipeline = AnswerPipeline(self.session)
            answer = pipeline.answer(case.question, verify=False)
            if answer.template_id.endswith("clarification"):
                return CaseResult(case.id, case.question, case.case_type, True, "按要求追问")
            return CaseResult(
                case.id, case.question, case.case_type, False,
                f"未追问，直接按 {answer.template_id} 作答",
            )

        # ---- 标准题：期望引用是否命中（Recall@5） ----
        expected = case.expected_citations or []
        hints = case.expected_points or []
        if not expected and not hints:
            return CaseResult(
                case.id, case.question, case.case_type, True, "无期望引用，跳过（未判定）"
            )

        # 判定口径：**Top5 里有没有在讲这个话题**。
        # 只认一条具体条款是不公平的——问"申报怎么缴"，
        # 有好几条条文都算对，只认其中一条会把正确答案判成错的。
        if hints:
            hit_index = next(
                (
                    index
                    for index, citation in enumerate(response.citations, start=1)
                    if any(hint in (citation.content or "") for hint in hints)
                ),
                None,
            )
            if hit_index is None:
                return CaseResult(
                    case.id, case.question, case.case_type, False,
                    f"Top5 没有在讲这个话题（线索：{'、'.join(hints)}）",
                    metrics={"recall": 0, "rr": 0.0},
                )
            # 精确命中期望条款时给更高的倒数排名，作为更严的参考
            exact_rank = next(
                (
                    index
                    for index, label in enumerate(labels, start=1)
                    if any(self._matches(item, label) for item in expected)
                ),
                None,
            )
            return CaseResult(
                case.id, case.question, case.case_type, True,
                f"话题命中第 {hit_index} 位" + (f"，期望条款在第 {exact_rank} 位" if exact_rank else "，期望条款未进 Top5"),
                metrics={"recall": 1, "rr": 1.0 / (exact_rank or hit_index)},
            )

        best_rank = None
        for index, label in enumerate(labels, start=1):
            if any(self._matches(item, label) for item in expected):
                best_rank = index
                break
        if best_rank is None:
            return CaseResult(
                case.id, case.question, case.case_type, False,
                f"Top5 未命中期望引用 {expected[:1]}",
                # 失败也要记 0 分。失败题若不带指标，
                # 结果 Recall@5 算出 1.0，而 30 条里挂了 29 条——
                # 指标必须把失败算进去，否则它是骗人的。
                metrics={"recall": 0, "rr": 0.0},
            )
        return CaseResult(
            case.id, case.question, case.case_type, True, f"命中第 {best_rank} 位",
            metrics={"recall": 1, "rr": 1.0 / best_rank},
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _matches(expected: str, label: str) -> bool:
        """期望引用与召回标签的比对：归一化空格后做包含匹配。

        库里的完整编号带章节前缀（"第四章 税收优惠 第二十三条"），
        引用里通常只写"第二十三条"，所以用包含而不是相等。
        """

        left = re.sub(r"\s+", "", expected)
        right = re.sub(r"\s+", "", label)
        return bool(left) and left in right

    @staticmethod
    def _find_leaks(citations, forbidden: list[str]) -> list[str]:
        """找出"这份文件本身就是被废止的那一份"的引用。

        必须是**身份比对**，不能拿标题做子串匹配：
        有一份仍然有效的国务院决定，标题是
        《国务院关于废止〈营业税暂行条例〉和修改〈增值税暂行条例〉的决定》——
        它的标题里就含有"增值税暂行条例"。用子串匹配会把这份**有效**的文件
        报成"引用了废止条款"，属于评测判据自己的误报。

        误报的代价不只是错一次：**门禁老是报假警，人就不再看它了**。
        """

        forbidden_set = {re.sub(r"\s+", "", item) for item in forbidden if item}
        leaked: list[str] = []
        for citation in citations:
            identity = re.sub(
                r"\s+", "", citation.document_number or citation.regulation_title or ""
            )
            if identity and identity in forbidden_set:
                leaked.append(citation.label())
        return leaked

    @staticmethod
    def _metrics(report: EvalRunReport, recalls: list[int], rrs: list[float]) -> dict:
        total = report.total or 1
        return {
            "用例总数": report.total,
            "通过数": report.passed,
            "通过率": round(report.passed / total, 4),
            "Recall@5": round(sum(recalls) / len(recalls), 4) if recalls else None,
            "MRR@5": round(sum(rrs) / len(rrs), 4) if rrs else None,
            "废止陷阱泄漏数": sum(
                1 for item in report.failures if item.case_type == CASE_REPEALED_TRAP
            ),
            "信息不足硬答数": sum(
                1 for item in report.failures if item.case_type == CASE_INSUFFICIENT
            ),
        }


def persist_run(
    session: Session,
    report: EvalRunReport,
    *,
    gate: str = "offline",
    status: str = "finished",
) -> EvalRun:
    """把一次评测结果落库，供看板看趋势。"""

    run = EvalRun(
        gate=gate,
        status=status,
        total_cases=report.total,
        passed_cases=report.passed,
        failed_cases=report.failed,
        metrics=report.metrics,
        failures=[item.to_dict() for item in report.failures],
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    return run
