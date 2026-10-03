"""管理后台接口。

四个模块：
    overview        概览：知识规模、缺口、评测、复核待办、筹划开关
    knowledge       知识管理：法规与复核状态（明细复用 /api/knowledge/*）
    gaps            缺口清单：哪些问题系统答不好，按出现次数排序
    evaluations     评测报告与门禁结论
    policy-changes  政策变化提醒

一条实现原则：**看板不能用假数据填充**。
依赖不可用（例如向量库挂了）时如实返回 `degraded` 说明，
而不是把索引点数写成 0 —— 0 和"查不到"在界面上长得一样，
但一个是数据、一个是故障，混起来会让人以为知识库空了。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.dependencies import DatabaseSession, require_any_permission
from app.domain.permissions import (
    AUDIT_VIEW,
    INDEX_REBUILD,
    KNOWLEDGE_GAP_HANDLE,
    KNOWLEDGE_REVIEW,
    PLANNING_REVIEW,
    SYSTEM_CONFIG,
)
from app.models.eval import EvalRun, GateDecision
from app.models.evolution import EvolutionEvent, KnowledgeGap
from app.models.knowledge import Regulation, RegulationArticle
from app.models.planning import PlanningReview
from app.schemas.console import (
    AuditLogItem,
    AuditLogResponse,
    ConsoleEvaluationResponse,
    ConsoleGapResponse,
    ConsoleOverviewResponse,
    PolicyChangeResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/console", tags=["管理后台"])

# 概览：管账号的、管知识审核的、看审计的都能打开，各自再多看不了别的
require_console_view = require_any_permission(
    KNOWLEDGE_REVIEW, AUDIT_VIEW, SYSTEM_CONFIG, INDEX_REBUILD
)


@router.get(
    "/overview",
    response_model=ConsoleOverviewResponse,
    summary="后台概览",
    dependencies=[Depends(require_console_view)],
)
def overview(session: DatabaseSession) -> ConsoleOverviewResponse:
    degraded: list[str] = []

    # 法规按效力状态与复核状态分布
    effect_rows = session.execute(
        select(Regulation.effect_status, func.count(Regulation.id)).group_by(
            Regulation.effect_status
        )
    ).all()
    review_rows = session.execute(
        select(Regulation.review_state, func.count(Regulation.id)).group_by(
            Regulation.review_state
        )
    ).all()
    article_count = session.execute(select(func.count(RegulationArticle.id))).scalar() or 0

    gap_open = session.execute(
        select(func.count(KnowledgeGap.id)).where(KnowledgeGap.status == "open")
    ).scalar() or 0
    review_pending = session.execute(
        select(func.count(PlanningReview.id)).where(PlanningReview.status == "pending")
    ).scalar() or 0

    # 向量索引点数：依赖不可用时如实说明，不写成 0
    index_points: int | None = None
    try:
        from app.retrieval.collections import KB_ARTICLES
        from app.retrieval.qdrant_client import get_client

        index_points = get_client().count(collection_name=KB_ARTICLES, exact=True).count
    except Exception as exc:  # noqa: BLE001 - 看板不能因为一个依赖挂了就打不开
        degraded.append(f"qdrant: {type(exc).__name__}")
        logger.warning("概览读取向量库失败：%s", exc)

    latest_run = session.execute(
        select(EvalRun).order_by(EvalRun.created_at.desc()).limit(1)
    ).scalars().first()
    latest_gate = session.execute(
        select(GateDecision).order_by(GateDecision.created_at.desc()).limit(1)
    ).scalars().first()

    from app.planning import RedLineMatcher

    return ConsoleOverviewResponse(
        regulations_total=sum(count for _, count in effect_rows),
        articles_total=article_count,
        index_points=index_points,
        by_effect_status={name: count for name, count in effect_rows},
        by_review_state={name: count for name, count in review_rows},
        open_gaps=gap_open,
        pending_reviews=review_pending,
        latest_evaluation=(
            {
                "id": latest_run.id,
                "gate": latest_run.gate,
                "total_cases": latest_run.total_cases,
                "passed_cases": latest_run.passed_cases,
                "failed_cases": latest_run.failed_cases,
                "metrics": latest_run.metrics,
                "finished_at": latest_run.finished_at,
            }
            if latest_run
            else None
        ),
        latest_gate_decision=(
            {
                "id": latest_gate.id,
                "gate": latest_gate.gate,
                "decision": latest_gate.decision,
                "veto_items": latest_gate.veto_items,
                "reasons": latest_gate.reasons,
                "rolled_back": latest_gate.rolled_back,
                "created_at": latest_gate.created_at,
            }
            if latest_gate
            else None
        ),
        planning_enabled=RedLineMatcher().planning_enabled,
        degraded=degraded,
    )


@router.get(
    "/audit-logs",
    response_model=AuditLogResponse,
    summary="审计日志（留痕查询）",
    dependencies=[Depends(require_any_permission(AUDIT_VIEW))],
)
def audit_logs(
    session: DatabaseSession,
    limit: int = Query(default=100, ge=1, le=500),
    action: str | None = Query(default=None, description="只看某类动作，如 qa.ask"),
    actor: str | None = Query(default=None, description="只看某个人"),
) -> AuditLogResponse:
    """谁在什么时候做了什么——登录、授权、知识变更、问答留痕都在这里。

    只读、可筛选。审计日志不提供修改与删除接口（技术方案 10.3）。
    """

    from app.models import AuditLog

    statement = select(AuditLog)
    if action:
        statement = statement.where(AuditLog.action == action)
    if actor:
        statement = statement.where(AuditLog.actor_username == actor)
    rows = session.execute(
        statement.order_by(AuditLog.occurred_at.desc()).limit(limit)
    ).scalars().all()
    return AuditLogResponse(
        total=len(rows),
        items=[
            AuditLogItem(
                id=row.id,
                occurred_at=row.occurred_at,
                actor_username=row.actor_username,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                detail=dict(row.detail or {}),
            )
            for row in rows
        ],
    )


@router.get(
    "/gaps",
    response_model=ConsoleGapResponse,
    summary="知识缺口清单",
    dependencies=[Depends(require_any_permission(KNOWLEDGE_GAP_HANDLE, KNOWLEDGE_REVIEW))],
)
def gaps(
    session: DatabaseSession,
    limit: int = Query(default=100, ge=1, le=500),
) -> ConsoleGapResponse:
    """按出现次数排序的缺口清单。

    缺口从哪里来：验证层记录的"没答好"、用户明确说"不对"的反馈、
    以及检索零命中的问题。这里只展示，不自动生成知识。
    """

    rows = session.execute(
        select(KnowledgeGap)
        .where(KnowledgeGap.status == "open")
        .order_by(KnowledgeGap.occurrences.desc(), KnowledgeGap.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return ConsoleGapResponse(
        total=len(rows),
        items=[
            {
                "id": row.id,
                "topic": row.topic,
                "tax_type": row.tax_type,
                "reason": row.reason,
                "occurrences": row.occurrences,
                "samples": list(row.sample_questions or []),
                "status": row.status,
            }
            for row in rows
        ],
    )


@router.get(
    "/evaluations",
    response_model=ConsoleEvaluationResponse,
    summary="评测报告与门禁结论",
    dependencies=[Depends(require_any_permission(KNOWLEDGE_REVIEW, SYSTEM_CONFIG))],
)
def evaluations(
    session: DatabaseSession,
    limit: int = Query(default=10, ge=1, le=100),
) -> ConsoleEvaluationResponse:
    runs = session.execute(
        select(EvalRun).order_by(EvalRun.created_at.desc()).limit(limit)
    ).scalars().all()
    decisions = session.execute(
        select(GateDecision).order_by(GateDecision.created_at.desc()).limit(limit)
    ).scalars().all()
    return ConsoleEvaluationResponse(
        runs=[
            {
                "id": row.id,
                "gate": row.gate,
                "total_cases": row.total_cases,
                "passed_cases": row.passed_cases,
                "failed_cases": row.failed_cases,
                "metrics": row.metrics,
                "failures": row.failures,
                "finished_at": row.finished_at,
            }
            for row in runs
        ],
        decisions=[
            {
                "id": row.id,
                "gate": row.gate,
                "decision": row.decision,
                "veto_items": row.veto_items,
                "reasons": row.reasons,
                "score": row.score,
                "release_ref": row.release_ref,
                "rolled_back": row.rolled_back,
                "created_at": row.created_at,
            }
            for row in decisions
        ],
    )


@router.get(
    "/policy-changes",
    response_model=PolicyChangeResponse,
    summary="政策变化提醒",
    dependencies=[Depends(require_any_permission(KNOWLEDGE_REVIEW, PLANNING_REVIEW, AUDIT_VIEW))],
)
def policy_changes(
    session: DatabaseSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> PolicyChangeResponse:
    """政策变更摘要 + 原始事件列表。

    推送通道未配置时，摘要里会如实写明"未配置推送通道"——
    界面上不能显示"已推送"，那会让人以为订阅生效了（口径）。
    """

    from app.evolution.notify import build_change_digest

    digest = build_change_digest(session, limit=limit)
    events = session.execute(
        select(EvolutionEvent).order_by(EvolutionEvent.created_at.desc()).limit(limit)
    ).scalars().all()
    return PolicyChangeResponse(
        digest=digest.to_dict(),
        events=[
            {
                "id": row.id,
                "event_type": row.event_type,
                "status": row.status,
                "title": row.title,
                "document_number": row.document_number,
                "source_url": row.source_url,
                "impact": row.impact,
                "discovered_at": row.discovered_at,
                "created_at": row.created_at,
            }
            for row in events
        ],
    )
