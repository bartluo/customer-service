"""门禁（技术方案 11.4 与 9.4）。

三道闸：
    闸门 1 离线回归 → 一票否决项必须全通过
    闸门 2 影子流量 → 新旧答案对比，专家抽检
    闸门 3 灰度放量 5% → 20% → 50% → 100%

任一阶段触发（效力错误 / 计算错误 / 投诉激增）→ 立即回滚。

**一票否决的含义**：该项只要有一条不通过，整批发布阻断，
不允许"整体指标达标所以放行"。这条最容易在实现时被写软——
做成"加权打分、总分够就放行"，红线就会被平均数稀释掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.evaluation.runner import EvalRunReport
from app.models.eval import CASE_INSUFFICIENT, CASE_REPEALED_TRAP, GateDecision

GATE_OFFLINE = "offline"  # 闸门 1：离线回归
GATE_SHADOW = "shadow"  # 闸门 2：影子流量
GATE_CANARY = "canary"  # 闸门 3：灰度放量

# 灰度阶梯（技术方案 11.4）
CANARY_STEPS = (0.05, 0.20, 0.50, 1.00)

# 一票否决项：财税域（技术方案 8.3）
# key 是指标名，value 是要求的阈值——100% 就是 1.0
VETO_METRICS: dict[str, tuple[str, float]] = {
    "废止陷阱泄漏数": ("引用了已废止条款", 0.0),
}

# 常规阈值（不达标也阻断，但属于"质量不够"而非"红线"）
THRESHOLDS: dict[str, float] = {
    "通过率": 0.85,
    "Recall@5": 0.80,
}


@dataclass
class GateResult:
    """一次门禁判定。"""

    gate: str = GATE_OFFLINE
    decision: str = "blocked"  # released / blocked
    veto_items: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    failed_cases: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def released(self) -> bool:
        return self.decision == "released"

    def to_dict(self) -> dict:
        return {
            "gate": self.gate,
            "decision": self.decision,
            "released": self.released,
            "veto_items": list(self.veto_items),
            "reasons": list(self.reasons),
            "failed_cases": list(self.failed_cases),
            "metrics": dict(self.metrics),
        }

    def explain(self) -> str:
        lines = [f"门禁（{self.gate}）：{'放行' if self.released else '阻断'}"]
        if self.veto_items:
            lines.append("  一票否决：")
            for item in self.veto_items:
                lines.append(f"    ⛔ {item}")
        for reason in self.reasons:
            lines.append(f"  · {reason}")
        if self.failed_cases:
            lines.append(f"  失败用例明细（{len(self.failed_cases)} 条）：")
            for item in self.failed_cases[:5]:
                lines.append(f"    - [{item.get('case_type')}] {item.get('question', '')[:36]}")
                lines.append(f"        {item.get('detail', '')}")
        return "\n".join(lines)


class GateKeeper:
    """按评测结果判门禁。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    def evaluate(
        self, report: EvalRunReport, *, gate: str = GATE_OFFLINE, release_ref: str = ""
    ) -> GateResult:
        result = GateResult(gate=gate, metrics=dict(report.metrics))

        # ① 一票否决项：不参与打分，直接阻断
        for metric_name, (label, threshold) in VETO_METRICS.items():
            value = report.metrics.get(metric_name)
            if value is None:
                continue
            if value > threshold:
                result.veto_items.append(f"{label}：{value} 条（要求 0 条）")

        # ② 常规阈值
        for metric_name, threshold in THRESHOLDS.items():
            value = report.metrics.get(metric_name)
            if value is None:
                continue
            if value < threshold:
                result.reasons.append(
                    f"{metric_name} 未达标：{value}（要求 ≥ {threshold}）"
                )

        # ③ 失败明细（按题型排：红线类排前面，便于优先看）
        order = {CASE_REPEALED_TRAP: 0, CASE_INSUFFICIENT: 1}
        result.failed_cases = sorted(
            (item.to_dict() for item in report.failures),
            key=lambda item: order.get(item["case_type"], 9),
        )

        blocked = bool(result.veto_items) or bool(result.reasons)
        result.decision = "blocked" if blocked else "released"
        self._persist(result, report, release_ref)
        return result

    # ------------------------------------------------------------------
    def _persist(self, result: GateResult, report: EvalRunReport, release_ref: str) -> None:
        self.session.add(
            GateDecision(
                gate=result.gate,
                decision=result.decision,
                veto_items=result.veto_items,
                reasons=result.reasons,
                release_ref=release_ref,
                score=float(report.metrics.get("通过率") or 0.0),
            )
        )
        self.session.commit()

    # ------------------------------------------------------------------
    def canary_stage(self, gate: str, ratio: float) -> float:
        """灰度阶梯：只允许按 5% → 20% → 50% → 100% 逐级放量。"""

        if gate != GATE_CANARY:
            return ratio
        for step in CANARY_STEPS:
            if ratio <= step:
                return step
        return CANARY_STEPS[-1]

    def rollback(self, decision_id: str, *, reason: str = "") -> GateDecision:
        """回滚：把已放行的门禁结论标记为回滚。

        真正把版本切回去由 CI/CD 负责（接入）；这里负责**留痕**——
        没有留痕的回滚，事后查不出"当时为什么回滚、影响了哪些版本"。
        """

        row = self.session.get(GateDecision, decision_id)
        if row is None:
            raise LookupError(f"门禁记录不存在：{decision_id}")
        row.rolled_back = True
        row.decision = "rolled_back"
        row.reasons = list(row.reasons or []) + [
            f"回滚于 {datetime.now(timezone.utc).isoformat(timespec='seconds')}：{reason or '未说明原因'}"
        ]
        self.session.commit()
        return row

    def latest(self, *, gate: str | None = None) -> GateDecision | None:
        from sqlalchemy import select

        statement = select(GateDecision).order_by(GateDecision.created_at.desc()).limit(1)
        if gate:
            statement = (
                select(GateDecision)
                .where(GateDecision.gate == gate)
                .order_by(GateDecision.created_at.desc())
                .limit(1)
            )
        return self.session.execute(statement).scalars().first()
