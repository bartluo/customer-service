"""问答与引用接口。

对应技术方案 12.4 的对外接口：
    POST /api/v1/qa/ask          业务系统调用问答
    GET  /api/v1/citations/{id}  查询引用条款的完整信息与原文

这一层**只做协议转换**：把请求交给 `AnswerPipeline`（问答主链路），
把结果整理成前端与外部系统能直接用的结构。任何判断逻辑都不写在这里——
写在这里就意味着"接口调用"和"命令行调用"会走出两套结果。

三条与合规直接相关的约定：
  · 免责声明取自模板渲染结果，不从请求或模型来，所以它不可能缺席；
  · 每次回答都写一条审计留痕，返回 trace_id 供事后追查；
  · 回答里带"知识截至时间"，用户能看出依据有多新。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select

from app.dependencies import CurrentPrincipal, DatabaseSession, require_permission
from app.domain.permissions import CITATION_VIEW, FEEDBACK_SUBMIT, QA_ASK
from app.knowledge.ontology import CITABLE_EFFECT_STATUSES
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
from app.reasoning import AnswerPipeline
from app.schemas.qa import (
    AnswerBody,
    AskRequest,
    AskResponse,
    CitationDetail,
    CitationOut,
    ComplianceOut,
    FeedbackRequest,
    FeedbackResponse,
)
from app.services import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/qa", tags=["问答"])
citations_router = APIRouter(prefix="/v1/citations", tags=["引用"])


def _knowledge_as_of(session, domain_id: str) -> datetime | None:
    """知识截至时间 = 库里现行有效法规的最后更新时间。

    为什么不取"索引构建时间"：用户关心的是"这些依据有多新"，
    即法规内容本身的更新时间。索引重建时间只说明索引动作，不说明知识新旧。
    """

    return session.execute(
        select(func.max(Regulation.updated_at)).where(
            Regulation.domain_id == domain_id,
            Regulation.review_state == "published",
            Regulation.effect_status.in_(CITABLE_EFFECT_STATUSES),
        )
    ).scalar()


def _disclaimer_of(answer: AnswerBody | None) -> str:
    """从渲染结果里取免责声明。取不到时给出兜底文案，绝不返回空。"""

    if answer:
        for section in answer.sections:
            if section.key == "disclaimer" and isinstance(section.content, str) and section.content:
                return section.content
    return "本回答依据现行有效政策生成，具体口径以主管税务机关认定为准。"


def _next_questions(answer: AnswerBody | None) -> list[str]:
    """追问模板下，把"需要您补充的信息"拆成一条条问题，方便界面渲染。"""

    if not answer:
        return []
    for section in answer.sections:
        if section.key == "judgement" and isinstance(section.content, str):
            text = section.content
            if "请先确认：" in text:
                tail = text.split("请先确认：", 1)[1]
                return [item for item in tail.split("；") if item.strip()]
    return []


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="提问（六段式结构化回答）",
    dependencies=[Depends(require_permission(QA_ASK))],
)
def ask(
    payload: AskRequest,
    session: DatabaseSession,
    principal: CurrentPrincipal,
) -> AskResponse:
    """走完整问答主链路，返回结构化答案 + 引用 + 计算过程 + 合规信息。"""

    trace_id = uuid.uuid4().hex
    pipeline = AnswerPipeline(session, top_n=payload.top_n)
    try:
        result = pipeline.answer(
            payload.question,
            tax_type=payload.tax_type,
            as_of=payload.as_of,
            verify=True,
        )
    except Exception as exc:  # noqa: BLE001 - 单次问答失败不该 500 掉整个服务
        logger.exception("问答执行失败 question=%s", payload.question[:60])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"问答服务暂时不可用：{type(exc).__name__}",
        ) from exc

    payload_dict = result.to_dict()
    answer = AnswerBody(**payload_dict["answer"]) if payload_dict.get("answer") else None
    citations = [CitationOut(**item) for item in payload_dict["citations"]]

    compliance = ComplianceOut(
        disclaimer=_disclaimer_of(answer),
        knowledge_as_of=_knowledge_as_of(session, payload.domain_id),
        answer_generated_at=datetime.now(timezone.utc),
        trace_id=trace_id,
        verified=result.verification is not None,
        refusal_reason=result.refusal_reason,
    )

    # 留痕：谁、什么时候、问了什么、引用了哪些条文。
    # 问题原文截断到 200 字——审计日志是审计用的，不是问答历史库。
    audit.record(
        session,
        action="qa.ask",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=principal.tenant_id,
        target_type="qa_trace",
        target_id=trace_id,
        detail={
            "question": payload.question[:200],
            "intent": result.intent,
            "template_id": result.template_id,
            "refused": result.refused,
            "citation_count": len(citations),
            "as_of": payload.as_of.isoformat() if payload.as_of else None,
        },
    )
    session.commit()

    return AskResponse(
        question=result.question,
        intent=result.intent,
        template_id=result.template_id,
        answer=answer,
        citations=citations,
        facts=payload_dict["facts"],
        applicability=payload_dict.get("applicability"),
        calculation=payload_dict.get("calculated"),
        verification=payload_dict.get("verification"),
        refused=result.refused,
        degraded=list(result.degraded),
        compliance=compliance,
        next_questions=_next_questions(answer),
    )


@router.post(
    "/feedback",
    response_model=FeedbackResponse,
    summary="提交答案反馈",
    dependencies=[Depends(require_permission(FEEDBACK_SUBMIT))],
)
def submit_feedback(
    payload: FeedbackRequest,
    session: DatabaseSession,
    principal: CurrentPrincipal,
) -> FeedbackResponse:
    """记录反馈；明确"答得不对"的反馈同时记进知识缺口。

    为什么不自动改知识：用户说"不对"不等于知识错了（可能是问法不同）。
    系统只把它变成缺口线索交给人复核，符合定下的"只发现不自动入库"。
    """

    audit.record(
        session,
        action="qa.feedback",
        actor_id=principal.user_id,
        actor_username=principal.username,
        tenant_id=principal.tenant_id,
        target_type="qa_trace",
        target_id=payload.trace_id,
        detail={
            "helpful": payload.helpful,
            "reason": payload.reason[:500],
            "has_correction": bool(payload.corrected_answer),
        },
    )

    recorded_gap = False
    if not payload.helpful:
        from app.evolution.gaps import record_gap

        record_gap(
            session,
            question=payload.reason or f"（无说明的差评）trace={payload.trace_id}",
            reason="user_feedback",
        )
        recorded_gap = True

    session.commit()
    return FeedbackResponse(
        trace_id=payload.trace_id,
        accepted=True,
        message="反馈已记录，会进入知识缺口清单由专家复核" if recorded_gap else "反馈已记录",
    )


@citations_router.get(
    "/{article_version_id}",
    response_model=CitationDetail,
    summary="引用详情（原文 + 效力状态 + 生效区间）",
    dependencies=[Depends(require_permission(CITATION_VIEW))],
)
def citation_detail(article_version_id: str, session: DatabaseSession) -> CitationDetail:
    """点开引用卡片看的内容。

    废止条文也要能查到（否则历史回答里的引用点开就是 404），
    但必须把 repealed=True 与废止依据一并返回——界面上要显眼地标出来，
    不能让用户以为它还有效。
    """

    row = session.execute(
        select(RegulationArticleVersion, RegulationArticle, Regulation)
        .join(RegulationArticle, RegulationArticle.id == RegulationArticleVersion.article_id)
        .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
        .where(RegulationArticleVersion.id == article_version_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="引用不存在")

    version, article, regulation = row
    return CitationDetail(
        article_version_id=version.id,
        article_id=article.id,
        regulation_id=regulation.id,
        regulation_title=regulation.title,
        document_number=regulation.document_number,
        issuer=regulation.issuer,
        hierarchy_level=regulation.hierarchy_level,
        level_code=article.level_code,
        full_no=article.full_no,
        heading_path=article.heading_path,
        content=version.content,
        effect_status=version.effect_status,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
        source_url=version.source_url,
        regulation_source_url=regulation.source_url,
        repealed=version.effect_status not in CITABLE_EFFECT_STATUSES,
        repeal_basis=version.repeal_basis,
        amend_basis=version.amend_basis,
    )
