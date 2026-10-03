"""问答与引用的接口数据结构（技术方案 7.3 / 12.4）。

两个刻意的设计：
  1. **结构化返回，不返回一坨文字**。答案按模板分成段落，每段带
     `renderer`（text / citation_list / formula_steps / ordered_steps /
     bullet_list / fixed_text），前端按 renderer 决定怎么画。
     让前端去解析自然语言拼格式，迟早会解析错。
  2. **引用与合规信息与答案平级**。引用不是答案的一部分——
     界面上它要能单独点开，导出的底稿也要单独附依据。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """一次提问。"""

    question: str = Field(min_length=1, max_length=2000, description="用户问题原文")
    domain_id: str = Field(default="finance_tax", max_length=64)
    tax_type: str | None = Field(default=None, max_length=40, description="强制指定税种")
    as_of: datetime | None = Field(
        default=None,
        description="按某一时点回答（默认现在）。用于「去年的政策是怎么规定的」这类问题",
    )
    top_n: int = Field(default=6, ge=1, le=20, description="参与判定的候选条文数")


class AnswerSection(BaseModel):
    """答案的一段。"""

    key: str
    title: str
    renderer: str
    content: object = None


class AnswerBody(BaseModel):
    """按模板渲染出的答案。"""

    template_id: str
    ok: bool = Field(description="必填段是否齐全。false 表示生成失败，调用方应看 refusal_reason")
    failure_reason: str = ""
    missing_required: list[str] = Field(default_factory=list)
    sections: list[AnswerSection] = Field(default_factory=list)


class CitationOut(BaseModel):
    """一条可点开的依据。"""

    label: str
    identifier: str
    document_number: str | None = None
    full_no: str | None = None
    regulation_title: str | None = None
    issuer: str | None = None
    hierarchy_level: str | None = None
    level_code: str | None = None
    effect_status: str
    content: str
    source_url: str | None = None
    tax_types: list[str] = Field(default_factory=list)
    regulation_id: str | None = None
    article_version_id: str | None = None
    valid_from_ts: int | None = None
    valid_to_ts: int | None = None
    traceable: bool = Field(description="能否定位到「哪部法规 + 哪一条」")
    score: float = 0.0
    routes: list[str] = Field(default_factory=list)
    rerank_reason: str | None = None


class ComplianceOut(BaseModel):
    """合规展示信息。每次回答都必须带。"""

    disclaimer: str = Field(description="免责声明。由模板固定文案渲染，不依赖模型")
    knowledge_as_of: datetime | None = Field(
        default=None, description="知识截至时间：库里现行有效法规的最后更新时间"
    )
    answer_generated_at: datetime
    trace_id: str = Field(description="本次问答的留痕编号，可在审计日志中查到")
    verified: bool = Field(description="是否经过验证层检查")
    refusal_reason: str = ""


class AskResponse(BaseModel):
    """一次问答的完整结果。"""

    question: str
    intent: str
    template_id: str
    answer: AnswerBody | None = None
    citations: list[CitationOut] = Field(default_factory=list)
    facts: dict = Field(default_factory=dict)
    applicability: dict | None = None
    calculation: dict | None = None
    verification: dict | None = None
    refused: bool = False
    degraded: list[str] = Field(default_factory=list)
    compliance: ComplianceOut
    next_questions: list[str] = Field(
        default_factory=list, description="需要用户补充的信息（追问时才有）"
    )


class CitationDetail(BaseModel):
    """引用详情：点开引用卡片时看的内容。"""

    article_version_id: str
    article_id: str
    regulation_id: str
    regulation_title: str
    document_number: str | None = None
    issuer: str | None = None
    hierarchy_level: str | None = None
    level_code: str | None = None
    full_no: str
    heading_path: str | None = None
    content: str
    effect_status: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_url: str | None = None
    regulation_source_url: str | None = None
    repealed: bool = Field(description="该条文是否已被废止（废止时必须显示，不能只显示正文）")
    repeal_basis: str | None = None
    amend_basis: str | None = None


class FeedbackRequest(BaseModel):
    """答案反馈（留痕 / 缺口发现的输入）。"""

    trace_id: str = Field(min_length=1, max_length=64)
    helpful: bool
    reason: str = Field(default="", max_length=1000)
    corrected_answer: str = Field(default="", max_length=4000)


class FeedbackResponse(BaseModel):
    trace_id: str
    accepted: bool
    message: str
