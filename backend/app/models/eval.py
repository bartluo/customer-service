"""评测集、评测执行与门禁记录（技术方案第 11 章）。

三个概念分开存，因为它们的生命周期完全不同：
  · EvalCase  —— 评测题，长期资产，需要专家评审，改一条就是改一个标准；
  · EvalRun   —— 一次执行的结果，每次变更跑一遍，用于看趋势与定位回归；
  · 门禁结论   —— 由 EvalRun 的指标算出来，是"能不能发布"的判据。

`EvalCase.forbidden_citations`（禁止出现的引用）是这套设计的关键：
**"答错"不好自动判定，但"引用了不该引用的东西"可以。**
废止陷阱题就是靠这个字段工作的。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.knowledge import KnowledgeTimestampMixin

# 题型（技术方案 11.1 的"特有硬指标"来源）
CASE_STANDARD = "standard"  # 标准题：期望召回指定条款
CASE_REPEALED_TRAP = "repealed_trap"  # 废止陷阱：不得出现已废止条款
CASE_INSUFFICIENT = "insufficient_info"  # 信息不足：正确行为是追问
CASE_REDLINE_TRAP = "redline_trap"  # 红线陷阱（筹划）：正确行为是拒绝 + 给替代路径

CASE_TYPES = (CASE_STANDARD, CASE_REPEALED_TRAP, CASE_INSUFFICIENT, CASE_REDLINE_TRAP)


class EvalCase(Base, KnowledgeTimestampMixin):
    """一条评测题。"""

    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    domain_id: Mapped[str] = mapped_column(String(64), default="finance_tax", nullable=False, index=True)
    case_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # 期望召回的依据（文号 + 条款号），格式与引用一致
    expected_citations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # 禁止出现的引用。废止陷阱靠它判定——
    # "答错"不好自动判，但"引用了不该引用的东西"可以。
    forbidden_citations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # 期望行为：answer（给答案）/ clarify（追问）/ refuse（拒绝并给替代路径）
    expected_behavior: Mapped[str] = mapped_column(String(20), default="answer", nullable=False)
    tax_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    difficulty: Mapped[str] = mapped_column(String(10), default="medium", nullable=False)
    # 标准答案要点（专家评审时填，用于半自动比对）
    expected_points: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # draft 待专家评审 / approved 已确认
    review_state: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(200), default="", nullable=False)

    __table_args__ = (Index("ix_eval_cases_type_state", "case_type", "review_state"),)


class EvalRun(Base, KnowledgeTimestampMixin):
    """一次评测执行（技术方案 11.3）。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 闸门：offline（离线回归）/ shadow（影子流量）/ canary（灰度放量）
    gate: Mapped[str] = mapped_column(String(20), default="offline", nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), default="running", nullable=False, index=True
    )
    total_cases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed_cases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_cases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 指标：Recall@5 / MRR@5 / 效力状态正确率 / 引用存在性 / 拒答准确率 …
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # 失败明细：哪条题错在哪，供人工定位
    failures: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GateDecision(Base, KnowledgeTimestampMixin):
    """门禁结论（技术方案 11.4）。"""

    __tablename__ = "gate_decisions"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    gate: Mapped[str] = mapped_column(String(20), default="offline", nullable=False, index=True)
    eval_run_id: Mapped[str | None] = mapped_column(PGUUID(as_uuid=False), nullable=True)
    # released（放行）/ blocked（阻断）/ rolled_back（已回滚）
    decision: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # 触发阻断的一票否决项
    veto_items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    reasons: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # 发布版本标识（接 CI 时用得上）
    release_ref: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    rolled_back: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
