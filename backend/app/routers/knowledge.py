"""财税知识库接口（对外能力）。

两条主链路：
  触发采集，数据库出现新法规记录 → POST /regulations/import
  一次导入上百份，有进度与失败清单 → ImportResponse
检索层只读这里的 published 数据。

权限：导入需要 knowledge.import（仅审核专家与固定管理员）。
客户角色只能读，不能写。
"""

from __future__ import annotations
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.dependencies import CurrentPrincipal, DatabaseSession, require_permission
from app.domain.permissions import KNOWLEDGE_IMPORT, KNOWLEDGE_REVIEW
from app.knowledge.ontology import EFFECT_STATUSES, HIERARCHY_LEVELS
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
from app.schemas.knowledge import (
    ArticleNode,
    ArticleVersionSummary,
    ImportDocumentRequest,
    ImportRequest,
    ImportResponse,
    PendingReviewItem,
    PendingReviewResponse,
    RegulationDetail,
    RegulationListResponse,
    RegulationSummary,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
)
from app.services import audit
from app.services.ingest.importer import DocumentInput, import_documents
from app.services.review import (
    ACTION_APPROVE,
    apply_decision,
    list_pending,
    pending_reasons,
)

router = APIRouter(prefix="/knowledge", tags=["财税知识库"])

require_knowledge_import = require_permission(KNOWLEDGE_IMPORT)
require_knowledge_review = require_permission(KNOWLEDGE_REVIEW)


def _to_summary(regulation: Regulation) -> RegulationSummary:
    return RegulationSummary(
        id=regulation.id,
        domain_id=regulation.domain_id,
        title=regulation.title,
        document_number=regulation.document_number,
        issuer=regulation.issuer,
        hierarchy_level=regulation.hierarchy_level,
        region_scope=regulation.region_scope,
        effect_status=regulation.effect_status,
        review_state=regulation.review_state,
        is_draft=regulation.is_draft,
        publish_date=regulation.publish_date,
        effective_date=regulation.effective_date,
        tax_types=list(regulation.tax_types or []),
        applies_to=list(regulation.applies_to or []),
        source_url=regulation.source_url,
        retrieved_at=regulation.retrieved_at,
        version=regulation.version,
    )


def _build_article_tree(articles: list[RegulationArticle]) -> list[ArticleNode]:
    """把平铺的条文列表组装成树。章 → 节 → 条 → 款 → 项。"""

    nodes: dict[str, ArticleNode] = {}
    roots: list[ArticleNode] = []

    for article in sorted(articles, key=lambda a: a.order_index):
        versions = [
            ArticleVersionSummary(
                id=version.id,
                version=version.version,
                content=version.content,
                valid_from=version.valid_from,
                valid_to=version.valid_to,
                effect_status=version.effect_status,
                repeal_basis=version.repeal_basis,
                amend_basis=version.amend_basis,
                source_url=version.source_url,
            )
            for version in sorted(article.versions, key=lambda v: v.version)
        ]
        nodes[article.id] = ArticleNode(
            id=article.id,
            level_code=article.level_code,
            article_no=article.article_no,
            full_no=article.full_no,
            heading_path=article.heading_path,
            order_index=article.order_index,
            parent_article_id=article.parent_article_id,
            versions=versions,
        )

    for article in sorted(articles, key=lambda a: a.order_index):
        node = nodes[article.id]
        if article.parent_article_id and article.parent_article_id in nodes:
            nodes[article.parent_article_id].children.append(node)
        else:
            roots.append(node)

    return roots


@router.post(
    "/regulations/import",
    response_model=ImportResponse,
    summary="批量导入法规",
    dependencies=[Depends(require_knowledge_import)],
)
def import_regulations(
    payload: ImportRequest,
    db: DatabaseSession,
    principal: CurrentPrincipal,
) -> ImportResponse:
    """批量导入法规文档，返回进度、成功/失败数、失败清单与待复核清单。

    单份失败不影响其它份（逐份独立事务）。缺文号或缺来源的文件会入库但标
    pending_review，由审核专家处理；它们不会进入检索结果。
    """

    documents = [
        DocumentInput(
            filename=item.filename,
            text=item.text,
            source_url=item.source_url,
            source_file_key=item.source_file_key,
            tax_types=item.tax_types,
            applies_to=item.applies_to,
        )
        for item in payload.documents
    ]

    report = import_documents(db, documents, domain_id=payload.domain_id)

    audit.record(
        db,
        action="knowledge.regulations_imported",
        actor_id=principal.user_id,
        actor_username=principal.username,
        target_type="regulation_batch",
        detail=report.to_dict(),
    )
    db.commit()

    return ImportResponse(**report.to_dict())


@router.get(
    "/regulations",
    response_model=RegulationListResponse,
    summary="法规列表",
)
def list_regulations(
    db: DatabaseSession,
    domain_id: str | None = Query(default=None),
    effect_status: str | None = Query(default=None),
    review_state: str | None = Query(default=None),
    hierarchy_level: str | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RegulationListResponse:
    """法规列表，支持按域、效力状态、审核状态、位阶与关键词过滤。"""

    statement = select(Regulation)
    if domain_id:
        statement = statement.where(Regulation.domain_id == domain_id)
    if effect_status:
        if effect_status not in EFFECT_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"未知效力状态：{effect_status}",
            )
        statement = statement.where(Regulation.effect_status == effect_status)
    if review_state:
        statement = statement.where(Regulation.review_state == review_state)
    if hierarchy_level:
        if hierarchy_level not in HIERARCHY_LEVELS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"未知效力位阶：{hierarchy_level}",
            )
        statement = statement.where(Regulation.hierarchy_level == hierarchy_level)
    if keyword:
        pattern = f"%{keyword}%"
        statement = statement.where(
            Regulation.title.ilike(pattern) | Regulation.document_number.ilike(pattern)
        )

    total = len(db.execute(statement).scalars().all())
    items = db.execute(statement.offset(offset).limit(limit)).scalars().all()
    return RegulationListResponse(
        count=total,
        items=[_to_summary(item) for item in items],
    )


@router.get(
    "/regulations/{regulation_id}",
    response_model=RegulationDetail,
    summary="法规详情（含条文树与版本）",
)
def get_regulation(regulation_id: str, db: DatabaseSession) -> RegulationDetail:
    """返回法规元数据 + 条文树 + 每个条文的全部历史版本。"""

    regulation = db.get(Regulation, regulation_id)
    if regulation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="法规不存在")

    articles = db.execute(
        select(RegulationArticle)
        .where(RegulationArticle.regulation_id == regulation_id)
        .options(selectinload(RegulationArticle.versions))
    ).scalars().all()

    summary = _to_summary(regulation)
    return RegulationDetail(
        **summary.model_dump(),
        summary=regulation.summary,
        expiry_date=regulation.expiry_date,
        source_file_key=regulation.source_file_key,
        content_hash=regulation.content_hash,
        articles=_build_article_tree(list(articles)),
    )


@router.get(
    "/articles/{article_id}/versions",
    summary="某条文的全部版本（政策变化追溯）",
)
def list_article_versions(article_id: str, db: DatabaseSession) -> dict[str, Any]:
    """返回某条文的版本历史。技术方案 4.1 的 version_history 查询模式。"""

    article = db.get(RegulationArticle, article_id)
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="条文不存在")

    versions = (
        db.execute(
            select(RegulationArticleVersion)
            .where(RegulationArticleVersion.article_id == article_id)
            .order_by(RegulationArticleVersion.version)
        )
        .scalars()
        .all()
    )
    return {
        "article_id": article.id,
        "full_no": article.full_no,
        "versions": [
            {
                "id": version.id,
                "version": version.version,
                "content": version.content,
                "valid_from": version.valid_from,
                "valid_to": version.valid_to,
                "effect_status": version.effect_status,
                "repeal_basis": version.repeal_basis,
                "amend_basis": version.amend_basis,
            }
            for version in versions
        ],
    }


# ---------- 复核：待复核清单与放行 / 驳回 ----------


def _indexed_point_count() -> int | None:
    """向量库里已有点数。Qdrant 不可用返回 None（复核本身不依赖它）。"""

    try:
        from app.retrieval.collections import KB_ARTICLES
        from app.retrieval.qdrant_client import get_client

        info = get_client().get_collection(KB_ARTICLES)
        return int(info.points_count or 0)
    except Exception:  # noqa: BLE001 - 索引状态只是提示信息，不影响复核动作
        return None


def _index_is_stale(db: Session, domain_id: str = "finance_tax") -> bool:
    """库里已放行的可索引条文数 与 向量库点数 是否对不上。

    对不上就说明有"已放行但还没进索引"的内容——审核专家放行之后必须重建索引，
    否则用户查不到刚放行的法规，看起来像"放行了没用"。
    """

    try:
        from app.retrieval.indexer import build_index_entries

        expected = len(build_index_entries(db, domain_id=domain_id))
    except Exception:  # noqa: BLE001
        return False
    actual = _indexed_point_count()
    if actual is None:
        return False
    return actual != expected


@router.get(
    "/review/pending",
    response_model=PendingReviewResponse,
    summary="待复核清单（含每条为什么待复核）",
    dependencies=[Depends(require_knowledge_review)],
)
def list_pending_review(
    db: DatabaseSession,
    domain_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> PendingReviewResponse:
    """审核专家打开复核台看到的第一屏。"""

    items = list_pending(db, domain_id=domain_id, limit=limit, offset=offset)
    return PendingReviewResponse(
        count=len(items),
        items=[
            PendingReviewItem(
                id=item.id,
                title=item.title,
                document_number=item.document_number,
                hierarchy_level=item.hierarchy_level,
                issuer=item.issuer,
                source_url=item.source_url or "",
                reasons=pending_reasons(item),
            )
            for item in items
        ],
        index_stale=_index_is_stale(db, domain_id or "finance_tax"),
    )


@router.post(
    "/review/{regulation_id}",
    response_model=ReviewDecisionResponse,
    summary="放行或驳回一条法规",
)
def decide_review(
    regulation_id: str,
    payload: ReviewDecisionRequest,
    db: DatabaseSession,
    principal: CurrentPrincipal,
) -> ReviewDecisionResponse:
    """给出复核结论。

    放行后这条法规立刻进入检索范围（重建索引后生效）；
    驳回则保持不可检索，等待补充材料后重新导入。
    """

    if not principal.has(KNOWLEDGE_REVIEW):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"缺少权限：{KNOWLEDGE_REVIEW}",
        )

    regulation = db.get(Regulation, regulation_id)
    if regulation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="法规不存在")

    reasons = pending_reasons(regulation)
    previous_state = regulation.review_state
    apply_decision(
        db,
        regulation,
        action=payload.action,
        note=payload.note,
        actor_id=principal.user_id,
        actor_username=principal.username,
    )
    db.commit()

    scheduled = False
    if payload.action == ACTION_APPROVE:
        # 放行改变了"哪些内容可以被检索"，索引必须跟着更新。
        # 用任务队列异步做，接口不用等几千条条文向量化完。
        try:
            from app.worker import rebuild_index

            rebuild_index.delay(regulation.domain_id)
            scheduled = True
        except Exception:  # noqa: BLE001 - 队列不可用时降级为提示，不让复核失败
            scheduled = False

    return ReviewDecisionResponse(
        id=regulation.id,
        title=regulation.title,
        review_state=regulation.review_state,
        previous_state=previous_state,
        reasons=reasons,
        index_rebuild_scheduled=scheduled,
    )
