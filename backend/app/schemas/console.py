"""管理后台接口的数据结构。

看板最容易骗人的地方是"缺数据"和"没数据"长得一样：
索引点数为 0 可能是"知识库真的空了"，也可能是"向量库连不上"。
所以凡是依赖外部服务的数字都用 `int | None`，取不到就是 null，
并在 degraded 里写清原因，界面必须区分显示。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ConsoleOverviewResponse(BaseModel):
    """后台概览。"""

    regulations_total: int
    articles_total: int
    index_points: int | None = Field(default=None, description="向量索引点数；null 表示查不到")
    by_effect_status: dict[str, int] = Field(default_factory=dict)
    by_review_state: dict[str, int] = Field(default_factory=dict)
    open_gaps: int
    pending_reviews: int
    latest_evaluation: dict | None = None
    latest_gate_decision: dict | None = None
    planning_enabled: bool
    degraded: list[str] = Field(default_factory=list, description="哪些依赖没读到，为什么")


class ConsoleGapResponse(BaseModel):
    total: int
    items: list[dict] = Field(default_factory=list)


class ConsoleEvaluationResponse(BaseModel):
    runs: list[dict] = Field(default_factory=list)
    decisions: list[dict] = Field(default_factory=list)


class PolicyChangeResponse(BaseModel):
    """政策变化提醒。"""

    digest: dict = Field(default_factory=dict, description="按影响程度排好序的摘要")
    events: list[dict] = Field(default_factory=list, description="原始变更事件")


class AuditLogItem(BaseModel):
    id: int
    occurred_at: datetime
    actor_username: str | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    detail: dict = Field(default_factory=dict)


class AuditLogResponse(BaseModel):
    """审计日志（合规留痕：谁在什么时候做了什么）。"""

    total: int
    items: list[AuditLogItem] = Field(default_factory=list)
