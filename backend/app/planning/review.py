"""专家复核台（技术方案 4.13）。

规格要求的能力，逐条对应到实现：

| 能力 | 实现 |
|---|---|
| 一屏看清方案 | `ReviewItem.to_dict()` 把画像 + 方案 + 依据 + 测算 + 风险点打平成一个对象 |
| 快速判定 | `decide()` 三个动作：放行 / 修改后放行 / 驳回并说明原因 |
| 一键修正 | `decide()` 传 `changes` 记录"改前 / 改后"，不覆盖原始快照 |
| 反馈入库 | 驳回与修正自动写进待转评测的反馈；**手法库的负样本不自动改**（见下） |
| 工作量可视 | `workload()` 给出待办量、平均处理时长、按时率 |

一条刻意不做的自动化：
  **驳回原因不会自动写进手法库的"被否案例"。**
  手法库是专家资产，自动追加会让未经确认的判断混进权威数据里。
  系统只把它整理成"待转清单"，由专家决定要不要收进手法库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.planning import PlanningReview
from app.planning.profile import CompanyProfile
from app.planning.space import CandidatePlan

# 复核动作（与 risk_rules.yaml 的 review_desk.actions 对应）
ACTION_APPROVE = "approve"
ACTION_APPROVE_WITH_CHANGES = "approve_with_changes"
ACTION_REJECT = "reject"
ACTIONS = (ACTION_APPROVE, ACTION_APPROVE_WITH_CHANGES, ACTION_REJECT)

STATUS_BY_ACTION = {
    ACTION_APPROVE: "approved",
    ACTION_APPROVE_WITH_CHANGES: "approved_with_changes",
    ACTION_REJECT: "rejected",
}


@dataclass
class ReviewItem:
    """一屏看清用的一条待办。"""

    review: PlanningReview
    profile: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)

    @property
    def risk_level(self) -> str:
        return self.review.risk_level

    def to_dict(self) -> dict:
        return {
            "id": self.review.id,
            "question": self.review.question,
            "risk_level": self.review.risk_level,
            "status": self.review.status,
            "technique_code": self.review.technique_code,
            "delivered_to_user": self.review.delivered_to_user,
            "profile": self.profile,
            "plan": self.plan,
        }

    def explain(self) -> str:
        plan = self.plan
        measurement = plan.get("measurement") or {}
        lines = [
            f"[{self.review.risk_level}] {plan.get('name', '')}"
            f"（{plan.get('path_name', '')}）　状态：{self.review.status}",
            f"  问题：{self.review.question or '（未记录）'}",
            f"  是否已发给用户：{'是' if self.review.delivered_to_user else '否'}",
            f"  依据：{'、'.join(plan.get('citations') or []) or '无'}",
        ]
        if measurement.get("computable"):
            lines.append(
                f"  测算：{measurement.get('tax_type')} "
                f"{measurement.get('before')} → {measurement.get('after')}，"
                f"节税 {measurement.get('saving')}"
            )
        elif measurement.get("note"):
            lines.append(f"  测算：不可测算——{measurement['note']}")
        if plan.get("preconditions"):
            lines.append(f"  前置条件：{'；'.join(plan['preconditions'])}")
        if plan.get("abuse_boundary"):
            lines.append(f"  滥用边界：{'；'.join(plan['abuse_boundary'])}")
        if plan.get("rejected_cases"):
            lines.append(f"  被否案例：{'；'.join(plan['rejected_cases'])}")
        if self.review.note:
            lines.append(f"  复核意见：{self.review.note}")
        return "\n".join(lines)


class ReviewDesk:
    """复核台的读写。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    def enqueue(
        self,
        plan: CandidatePlan,
        *,
        profile: CompanyProfile | None = None,
        question: str = "",
        delivered_to_user: bool = False,
    ) -> PlanningReview:
        """把方案推入复核队列。"""

        review = PlanningReview(
            question=question[:2000],
            profile=profile.to_dict() if profile else {},
            plan=plan.to_dict(),
            technique_code=plan.code,
            risk_level=plan.risk_level,
            delivered_to_user=delivered_to_user,
            status="pending",
        )
        self.session.add(review)
        self.session.commit()
        return review

    def pending(self, *, limit: int = 100) -> list[ReviewItem]:
        rows = self.session.execute(
            select(PlanningReview)
            .where(PlanningReview.status == "pending")
            .order_by(PlanningReview.created_at)
            .limit(limit)
        ).scalars().all()
        return [ReviewItem(review=row, profile=row.profile, plan=row.plan) for row in rows]

    def get(self, review_id: str) -> ReviewItem | None:
        row = self.session.get(PlanningReview, review_id)
        if row is None:
            return None
        return ReviewItem(review=row, profile=row.profile, plan=row.plan)

    # ------------------------------------------------------------------
    def decide(
        self,
        review_id: str,
        *,
        action: str,
        reviewer: str,
        note: str = "",
        changes: dict[str, Any] | None = None,
    ) -> PlanningReview:
        """给出复核结论。三个动作对应规格里的三个按钮。"""

        if action not in ACTIONS:
            raise ValueError(f"未知复核动作：{action}（可选：{' / '.join(ACTIONS)}）")
        row = self.session.get(PlanningReview, review_id)
        if row is None:
            raise LookupError(f"复核任务不存在：{review_id}")
        if row.status != "pending":
            raise ValueError(f"该任务已处理过（当前状态：{row.status}）")
        if action == ACTION_REJECT and not note.strip():
            # 驳回必须说明原因：理由是它转成评测用例与负样本的唯一输入
            raise ValueError("驳回必须填写原因")

        row.status = STATUS_BY_ACTION[action]
        row.reviewer = reviewer
        row.note = note
        row.changes = changes or {}
        row.decided_at = datetime.now(timezone.utc)
        self.session.commit()
        return row

    # ------------------------------------------------------------------
    def workload(self, *, sla_hours: int = 24) -> dict:
        """工作量可视：待办量、平均处理时长、按时率。"""

        pending = self.session.execute(
            select(func.count(PlanningReview.id)).where(PlanningReview.status == "pending")
        ).scalar() or 0
        decided = self.session.execute(
            select(PlanningReview).where(PlanningReview.decided_at.is_not(None))
        ).scalars().all()
        durations = [
            (row.decided_at - row.created_at).total_seconds() / 3600
            for row in decided
            if row.decided_at and row.created_at
        ]
        on_time = [item for item in durations if item <= sla_hours]
        return {
            "pending": int(pending),
            "decided": len(decided),
            "avg_hours": round(sum(durations) / len(durations), 2) if durations else None,
            "on_time_rate": round(len(on_time) / len(durations), 3) if durations else None,
            "sla_hours": sla_hours,
        }

    def overdue(self, *, sla_hours: int = 24) -> list[PlanningReview]:
        """超时未处理的任务。规格要求超时"自动降级标注"，不静默放行。"""

        deadline = datetime.now(timezone.utc) - timedelta(hours=sla_hours)
        return list(
            self.session.execute(
                select(PlanningReview).where(
                    PlanningReview.status == "pending",
                    PlanningReview.created_at < deadline,
                )
            ).scalars().all()
        )

    # ------------------------------------------------------------------
    def feedback_for_eval(self) -> list[dict]:
        """反馈入库（"反馈入库"能力）。

        把"被驳回"与"被修改后放行"的案例整理成**待转清单**：
          · 驳回 → 策划的负样本（评测集用）
          · 修改后放行 → 系统判断与专家判断的差异（校准验证器松紧）

        **不自动写进手法库。** 手法库是专家资产，自动追加会让未经确认的判断
        混进权威数据里；这里只整理，由专家决定收不收。
        """

        rows = self.session.execute(
            select(PlanningReview)
            .where(PlanningReview.status.in_(("rejected", "approved_with_changes")))
            .order_by(PlanningReview.decided_at.desc())
        ).scalars().all()
        items: list[dict] = []
        for row in rows:
            items.append(
                {
                    "review_id": row.id,
                    "status": row.status,
                    "question": row.question,
                    "technique_code": row.technique_code,
                    "risk_level": row.risk_level,
                    "reviewer": row.reviewer,
                    "reason": row.note,
                    "changes": row.changes,
                    "suggested_use": (
                        "转入评测集作为负样本"
                        if row.status == "rejected"
                        else "用于校准风险分级与验证器松紧"
                    ),
                }
            )
        return items
