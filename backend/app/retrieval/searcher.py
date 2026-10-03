"""端到端检索编排（收口）。

把四层串成一条流水线：
    查询 → 三路并行召回 → RRF 融合 → boost 加权 → 启发式重排 → Top-N 引用

三路的分工是固定的：
  · 结构化（PostgreSQL）  问"哪一条" —— 文号、条款号、税种、时点
  · 语义（Qdrant 稠密）    问"什么意思" —— 自然语言问题
  · 术语扩展（Qdrant 稀疏）问"换个说法" —— 口语 → 法规术语

三层降级保证任何单点故障都不让接口挂掉：
  embedding 服务挂 → 语义与术语扩展返回空，只剩结构化
  Qdrant 挂       → 只剩结构化
  PostgreSQL 挂   → 只剩向量路（此时只剩检索能力，不能保证精确命中）
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app.retrieval.fusion import (
    FusedHit,
    FusionWeights,
    apply_boosts,
    reciprocal_rank_fusion,
)
from app.retrieval.glossary import GlossaryExpander
from app.retrieval.rerank import RerankResult, rerank
from app.retrieval.semantic import SemanticQuery, SemanticSearcher
from app.retrieval.structured import StructuredQuery, search_structured

logger = logging.getLogger(__name__)


@dataclass
class SearchRequest:
    """一次检索的完整输入。"""

    question: str
    domain_id: str = "finance_tax"
    tax_types: list[str] = field(default_factory=list)
    applies_to: list[str] = field(default_factory=list)
    document_number: str | None = None
    article_no: str | None = None
    as_of: datetime | None = None
    top_n: int = 10


@dataclass
class Citation:
    """一条可引用的依据。"""

    document_number: str | None
    full_no: str | None
    regulation_title: str | None
    issuer: str | None
    effect_status: str
    level_code: str | None
    hierarchy_level: str | None
    content: str
    source_url: str | None
    score: float
    # 税种。适用性判定要拿它和用户问的税种比对，所以随引用一起带出来。
    tax_types: list[str] = field(default_factory=list)
    # 验证层要用：条文归属哪份法规（引用验证）、有效区间（时效验证）
    regulation_id: str | None = None
    # 条文版本 ID。界面上的引用卡片靠它去查完整原文与沿革
    article_version_id: str | None = None
    valid_from_ts: int | None = None
    valid_to_ts: int | None = None
    routes: list[str] = field(default_factory=list)
    rerank_reason: str | None = None

    # 法律由主席令公布、行政法规由国务院令公布，官方政策法规库里这两类的
    # "发文字号"字段本来就是空的。实务中引用它们写的是法规名称
    # （《中华人民共和国增值税法》第九条），所以这两类允许用名称做文件标识。
    # 规范性文件不行：同名公告很多，没有文号就无法唯一定位。
    NAME_IDENTIFIED_LEVELS = ("law", "administrative_regulation")

    def identifier(self) -> str:
        """引用的文件标识：优先文号，法律/行政法规退化为法规名称。"""

        if self.document_number:
            return self.document_number
        if self.hierarchy_level in self.NAME_IDENTIFIED_LEVELS:
            return self.regulation_title or ""
        return ""

    def is_traceable(self) -> bool:
        """可溯源 = 有条款号（定位到条）+ 有文件标识（定位到哪部法规）。"""

        return bool(self.full_no) and bool(self.identifier())

    def label(self) -> str:
        """人类可读的引用标签，如「财税〔2019〕39号 第二十七条」。

        显示宽松、判定严格：标签退回法规名称也是有效引用
        （"《中华人民共和国发票管理办法》第十五条"能定位到条文）；
        能不能算"可溯源"由 is_traceable() 决定。
        """

        head = self.document_number or self.regulation_title or "未标注文号"
        return f"{head} {self.full_no}".strip() if self.full_no else head

    def to_dict(self) -> dict:
        """给接口层的完整引用。

        界面上的引用卡片要能点开看原文、看效力状态、看生效区间，
        所以这里把"定位信息 + 原文 + 时效"一起给出去，
        而不是只给一个文字标签让前端自己再去查一次。
        """

        return {
            "label": self.label(),
            "identifier": self.identifier(),
            "document_number": self.document_number,
            "full_no": self.full_no,
            "regulation_title": self.regulation_title,
            "issuer": self.issuer,
            "hierarchy_level": self.hierarchy_level,
            "level_code": self.level_code,
            "effect_status": self.effect_status,
            "content": self.content,
            "source_url": self.source_url,
            "tax_types": list(self.tax_types),
            "regulation_id": self.regulation_id,
            "article_version_id": self.article_version_id,
            "valid_from_ts": self.valid_from_ts,
            "valid_to_ts": self.valid_to_ts,
            "traceable": self.is_traceable(),
            "score": self.score,
            "routes": list(self.routes),
            "rerank_reason": self.rerank_reason,
        }


@dataclass
class SearchResponse:
    """检索结果。"""

    question: str
    citations: list[Citation]
    expanded_question: str | None = None
    intent: str | None = None
    route_stats: dict = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.citations


def detect_document_number(text: str) -> str | None:
    """从自然语言里识别完整文号。

    用户不会只打"财税〔2019〕39号"，通常说"财税39号公告里怎么规定的"。
    这里做归一化匹配：取到文号后再去数据库核对标准写法，
    避免因为用户少打了"〔〕"就匹配不上。
    """

    import re

    text = text.replace(" ", "").replace("　", "")
    match = re.search(r"[一-龥]{2,20}(?:公告|通告|通知|文件|令)\d{4}年?第?\d*号?", text)
    if match:
        return match.group(0)
    match = re.search(r"[一-龥]{2,12}〔\d{4}〕\d+号", text)
    if match:
        return match.group(0)
    match = re.search(r"[一-龥]{2,12}\[\d{4}\]\d+号", text)
    if match:
        return match.group(0)
    return None


class KnowledgeSearcher:
    """财税知识检索器。"""

    def __init__(
        self,
        db: Session,
        client: QdrantClient | None = None,
        weights: FusionWeights | None = None,
        expander: GlossaryExpander | None = None,
    ) -> None:
        self.db = db
        self.weights = weights or FusionWeights()
        self.expander = expander or GlossaryExpander()
        self.semantic = SemanticSearcher(client=client)

    def _route_structured(self, request: SearchRequest) -> tuple[list[Any], list[str]]:
        degraded: list[str] = []

        # 结构化路只在"用户明确指向某一份文件/某一条"时才启用。
        # 原因：StructuredQuery 的所有条件都是可选的，没有精确条件时它会
        # 按发布日期倒序返回任意 N 条——这不是召回，是噪声。
        # 而结构化路权重最高（1.0）且排在第 1 名，噪声会直接霸占答案顶部，
        # 把真正相关的语义结果挤到第 10 位之后。
        if not request.document_number and not request.article_no:
            return [], []

        try:
            hits = search_structured(
                self.db,
                StructuredQuery(
                    domain_id=request.domain_id,
                    tax_types=request.tax_types,
                    applies_to=request.applies_to,
                    document_number=request.document_number,
                    article_no=request.article_no,
                    keyword=None,
                    as_of=request.as_of,
                    limit=10,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 单路失败不能拖垮整体
            logger.error("结构化检索失败：%s", exc)
            degraded.append("structured")
            return [], degraded
        return list(hits), degraded

    def _route_semantic(self, text: str, request: SearchRequest) -> tuple[list[dict], list[str]]:
        try:
            hits = self.semantic.search(
                SemanticQuery(
                    text=text,
                    domain_id=request.domain_id,
                    limit=30,
                    tax_types=request.tax_types,
                    as_of=request.as_of,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("语义检索失败：%s", exc)
            return [], ["semantic"]
        return self._prune_stale_hits(hits), []

    def _route_glossary(
        self, expanded_text: str, request: SearchRequest
    ) -> tuple[list[dict], list[str]]:
        if expanded_text == request.question:
            # 术语扩展没有产生新词，第三路与第二路重复，跳过以免虚高
            return [], []
        try:
            hits = self.semantic.search(
                SemanticQuery(
                    text=expanded_text,
                    domain_id=request.domain_id,
                    limit=30,
                    tax_types=request.tax_types,
                    as_of=request.as_of,
                    sparse_only=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("术语扩展检索失败：%s", exc)
            return [], ["glossary_expansion"]
        return self._prune_stale_hits(hits), []

    def search(self, request: SearchRequest) -> SearchResponse:
        """执行完整检索。"""

        degraded: list[str] = []

        # 时点默认取"现在"。
        # 为什么要有这个默认值：生效区间过滤（valid_from / valid_to）只在 as_of
        # 非空时才生效。之前 as_of 默认是 None，等于"时点过滤平时根本没开"——
        # 后果是执行期限已经届满的税收优惠（如"执行期限为2023年1月1日至
        # 2024年12月31日"）会被当成现行政策引出来。
        # 用户问的默认就是"现在怎么规定"，所以默认取当前时刻，
        # 需要查历史口径时由调用方显式传 as_of。
        as_of = request.as_of or datetime.now(timezone.utc)

        document_number = request.document_number or detect_document_number(request.question)
        tax_types = list(request.tax_types)
        if not tax_types:
            tax_types = self.expander.expand_tax_types(request.question)

        expanded = self.expander.expand(request.question)

        # 结构化路：问"哪一条"时才用得上关键词过滤。
        # 传 keyword 做全文包含会砍掉大部分召回，所以这里只传精确条件。
        structured_request = SearchRequest(
            question=request.question,
            domain_id=request.domain_id,
            tax_types=tax_types,
            applies_to=request.applies_to,
            document_number=document_number,
            article_no=request.article_no,
            as_of=as_of,
        )
        structured_hits, s_degraded = self._route_structured(structured_request)
        degraded.extend(s_degraded)

        semantic_hits, sem_degraded = self._route_semantic(request.question, structured_request)
        degraded.extend(sem_degraded)

        glossary_hits, g_degraded = self._route_glossary(expanded, structured_request)
        degraded.extend(g_degraded)

        fused = reciprocal_rank_fusion(
            {
                "structured": structured_hits,
                "semantic": semantic_hits,
                "glossary_expansion": glossary_hits,
            },
            self.weights,
        )
        fused = apply_boosts(
            fused,
            query_document_number=document_number,
            weights=self.weights,
            as_of=as_of,
        )

        results: list[RerankResult] = rerank(fused, request.question, top_n=request.top_n)

        citations = [self._to_citation(item) for item in results]

        return SearchResponse(
            question=request.question,
            citations=citations,
            expanded_question=expanded if expanded != request.question else None,
            intent=results[0].intent if results else None,
            route_stats={
                "structured": len(structured_hits),
                "semantic": len(semantic_hits),
                "glossary_expansion": len(glossary_hits),
                "fused": len(fused),
                "returned": len(citations),
            },
            degraded=sorted(set(degraded)),
        )

    def _to_citation(self, result: RerankResult) -> Citation:
        payload = result.hit.payload
        return Citation(
            document_number=payload.get("document_number"),
            full_no=payload.get("full_no"),
            regulation_title=payload.get("regulation_title"),
            issuer=payload.get("issuer"),
            effect_status=payload.get("effect_status", ""),
            level_code=payload.get("level_code"),
            hierarchy_level=payload.get("hierarchy_level"),
            content=payload.get("content", ""),
            source_url=payload.get("source_url"),
            score=result.score,
            tax_types=list(payload.get("tax_types") or []),
            regulation_id=payload.get("regulation_id"),
            article_version_id=payload.get("article_version_id"),
            valid_from_ts=payload.get("valid_from_ts"),
            valid_to_ts=payload.get("valid_to_ts"),
            routes=result.hit.routes,
            rerank_reason=result.explain(),
        )

    def _valid_version_ids(self, version_ids: list[str]) -> set[str]:
        """用数据库的权威状态校验向量路召回的条文版本。

        为什么必须有这一步：向量索引是异步重建的。
        法规被撤回、条文被删除、复核状态从 published 改回 pending_review 时，
        数据库立刻生效但索引还留着旧点——此时语义检索仍会把已撤回的条文
        送进答案。财税场景下这是事故，所以每次检索都做一次权威复核。

        代价是每轮一次数据库查询。用 IN 批量查，只走主键，不做联表。
        """

        if not version_ids:
            return set()
        from sqlalchemy import select

        from app.knowledge.ontology import CITABLE_EFFECT_STATUSES
        from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion

        statement = (
            select(RegulationArticleVersion.id)
            .join(RegulationArticle, RegulationArticle.id == RegulationArticleVersion.article_id)
            .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
            .where(
                RegulationArticleVersion.id.in_(version_ids),
                Regulation.review_state == "published",
                RegulationArticleVersion.effect_status.in_(CITABLE_EFFECT_STATUSES),
            )
        )
        return {row[0] for row in self.db.execute(statement).all()}

    def _prune_stale_hits(self, hits: list[dict]) -> list[dict]:
        """剔除向量库里已失效的残留点。"""

        ids = [hit.get("article_version_id") for hit in hits if hit.get("article_version_id")]
        if not ids:
            return hits
        try:
            valid = self._valid_version_ids(ids)
        except Exception as exc:  # noqa: BLE001 - 校验失败宁可不召回
            logger.error("条文状态复核失败，本轮向量结果全部丢弃：%s", exc)
            return []
        return [hit for hit in hits if hit.get("article_version_id") in valid]
