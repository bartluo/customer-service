"""财税知识库的接口数据结构。"""

from __future__ import annotations
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ImportDocumentRequest(BaseModel):
    """一份待导入的法规文档。"""

    filename: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, description="法规原文纯文本")
    source_url: str | None = Field(
        default=None,
        max_length=1000,
        description="来源 URL。技术方案 10.3 要求来源可追溯；为空则标待复核",
    )
    source_file_key: str | None = Field(default=None, max_length=500)
    tax_types: list[str] = Field(default_factory=list)
    applies_to: list[str] = Field(default_factory=list)


class ImportRequest(BaseModel):
    """批量导入请求。"""

    documents: list[ImportDocumentRequest] = Field(min_length=1, max_length=500)
    domain_id: str = Field(default="finance_tax", max_length=64)


class ImportFailure(BaseModel):
    filename: str
    reason: str


class ReviewItem(BaseModel):
    filename: str
    title: str
    reasons: str


class ImportResponse(BaseModel):
    """导入报告：进度、成功/失败数、失败清单、待复核清单。"""

    total: int
    success: int
    skipped: int
    failed: int
    needs_review: int
    articles: int
    progress_percent: float
    failures: list[ImportFailure] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)


class RegulationSummary(BaseModel):
    id: str
    domain_id: str
    title: str
    document_number: str | None
    issuer: str | None
    hierarchy_level: str
    region_scope: str
    effect_status: str
    review_state: str
    is_draft: bool
    publish_date: datetime | None
    effective_date: datetime | None
    tax_types: list[str]
    applies_to: list[str]
    source_url: str
    retrieved_at: datetime
    version: int


class RegulationListResponse(BaseModel):
    count: int
    items: list[RegulationSummary]


class ArticleVersionSummary(BaseModel):
    id: str
    version: int
    content: str
    valid_from: datetime
    valid_to: datetime | None
    effect_status: str
    repeal_basis: str | None
    amend_basis: str | None
    source_url: str | None


class ArticleNode(BaseModel):
    """条文树节点：条文 + 它的版本列表 + 子节点。"""

    id: str
    level_code: str
    article_no: str
    full_no: str
    heading_path: str | None
    order_index: int
    parent_article_id: str | None
    versions: list[ArticleVersionSummary] = Field(default_factory=list)
    children: list["ArticleNode"] = Field(default_factory=list)


class RegulationDetail(RegulationSummary):
    summary: str | None
    expiry_date: datetime | None
    source_file_key: str | None
    content_hash: str | None
    articles: list[ArticleNode] = Field(default_factory=list)


ArticleNode.model_rebuild()


# ---------- 复核（放行 / 驳回） ----------


class PendingReviewItem(BaseModel):
    """待复核队列里的一条法规，含"为什么在这里"。"""

    id: str
    title: str
    document_number: str | None
    hierarchy_level: str | None
    issuer: str | None
    source_url: str
    reasons: list[str] = Field(default_factory=list)


class PendingReviewResponse(BaseModel):
    count: int
    items: list[PendingReviewItem] = Field(default_factory=list)
    index_stale: bool = Field(
        default=False,
        description="为 True 表示库里有已放行但还没进向量索引的内容，需要重建索引",
    )


class ReviewDecisionRequest(BaseModel):
    """一次复核结论。结论只有放行或驳回两种（修改后放行走"驳回 + 重新导入"）。"""

    action: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000, description="复核意见，进审计日志")


class ReviewDecisionResponse(BaseModel):
    id: str
    title: str
    review_state: str
    previous_state: str
    reasons: list[str] = Field(default_factory=list)
    index_rebuild_scheduled: bool = False
