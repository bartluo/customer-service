"""筹划接口的数据结构。

筹划与问答最大的不同：**输出是一组方案，不是一个答案**。
所以这里的返回结构是"对比表 + 每个方案的详情"，而不是一段文字。

两条不可动摇的约定（ADR-0015）：
  · 🔴 高风险方案**不出现在给用户的结果里**，只在复核台可见；
  · 每个方案都带 `abuse_boundary`（滥用边界）——只讲怎么省税、不讲做到哪一步
    就变成违规，等于把风险留给用户自己撞。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlanningAnalyzeRequest(BaseModel):
    """提交企业画像，生成筹划空间。"""

    profile: dict = Field(description="企业画像。字段见 GET /v1/planning/profile-schema")
    request_text: str = Field(
        default="", max_length=2000, description="用户原始诉求（用于红线与反避税检查）"
    )
    enqueue_review: bool = Field(
        default=True,
        description="是否把需要人工复核的方案推入复核台（高风险方案只能走复核台）",
    )


class PlanOut(BaseModel):
    """一个候选方案。"""

    code: str
    name: str
    path: str
    path_name: str
    category: str
    risk_level: str
    mechanism: str = ""
    actions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    cost: str = ""
    citations: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    abuse_boundary: list[str] = Field(default_factory=list)
    rejected_cases: list[str] = Field(default_factory=list)
    measurement: dict = Field(default_factory=dict)
    missing: list[str] = Field(default_factory=list)


class ComparisonOut(BaseModel):
    """多方案对比表。"""

    rows: list[dict] = Field(default_factory=list)
    combinations: list[str] = Field(default_factory=list)


class PlanningAnalyzeResponse(BaseModel):
    """一次筹划分析的结果。"""

    profile: dict
    missing_required: list[str] = Field(description="画像还缺哪些关键信息，缺了不能出方案")
    planning_enabled: bool = Field(description="筹划总开关状态")
    plans: list[PlanOut] = Field(default_factory=list, description="可发给用户的方案（🟡🟢）")
    withheld: list[PlanOut] = Field(
        default_factory=list, description="🔴 高风险方案，不发给用户，只在复核台可见"
    )
    comparison: ComparisonOut
    legality: dict = Field(default_factory=dict, description="四道合法性检查的结论")
    risk_level: str = ""
    notes: list[str] = Field(default_factory=list)
    created_reviews: int = Field(default=0, description="本次推入复核台的条数")
    disclaimer: str = ""


class ReviewDecisionRequest(BaseModel):
    """复核决定。"""

    action: str = Field(description="approve / approve_with_changes / reject")
    note: str = Field(default="", max_length=2000)
    risk_level: str | None = Field(
        default=None, description="修改后放行时，把风险等级改为 green / yellow / red"
    )


class ReviewItemOut(BaseModel):
    id: str
    question: str
    risk_level: str
    status: str
    technique_code: str | None = None
    delivered_to_user: bool
    profile: dict = Field(default_factory=dict)
    plan: dict = Field(default_factory=dict)


class ReviewListResponse(BaseModel):
    total: int
    items: list[ReviewItemOut] = Field(default_factory=list)


class ReviewWorkloadResponse(BaseModel):
    """复核工作量：待办、已处理、平均时长、按时率、超时清单。"""

    pending: int
    decided: int
    # 还没有任何已处理记录时，平均时长与按时率是"未知"，不是 0——
    # 写成 0 会让人以为"处理得飞快"，那是假的。
    avg_hours: float | None = None
    sla_hours: int
    on_time_rate: float | None = None
    overdue: list[ReviewItemOut] = Field(default_factory=list)


class FeedbackForEvalResponse(BaseModel):
    """待转评测集的反馈清单（专家的"被否案例"素材来源）。"""

    total: int
    items: list[dict] = Field(default_factory=list)
