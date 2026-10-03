"""稠密与稀疏检索。

两路都打同一个集合 kb_articles：
  · 稠密用 "dense" 命名向量，找"意思相近"的条文
  · 稀疏用 "sparse" 命名向量，找"字面精确"的条文（文号、条款号、税率数字）

为什么必须两路都有：纯稠密向量对"财税〔2019〕39号"这类精确串不敏感，
纯稀疏又完全不懂"能不能抵"↔"进项税额抵扣"这种同义表达。财税场景两者都要。
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime

from qdrant_client import QdrantClient
from qdrant_client.models import Fusion, FusionQuery, SparseVector

from app.retrieval.collections import KB_ARTICLES
from app.retrieval.embedding_client import get_embedding_client
from app.retrieval.filters import build_effect_filter

logger = logging.getLogger(__name__)


@dataclass
class SemanticQuery:
    """语义检索条件。"""

    text: str
    domain_id: str = "finance_tax"
    limit: int = 30
    # 只走稀疏路（用于文号、条款号这类精确匹配）
    sparse_only: bool = False
    tax_types: list[str] = field(default_factory=list)
    as_of: datetime | None = None


class SemanticSearcher:
    """kb_articles 的稠密 + 稀疏检索器。"""

    def __init__(self, client: QdrantClient | None = None) -> None:
        from app.retrieval.qdrant_client import get_client

        self.client = client or get_client()

    def search(self, query: SemanticQuery) -> list[dict]:
        """执行检索，返回 payload 列表（已按相似度排序、已过硬过滤）。

        embedding 服务不可用时返回空列表而不是抛异常——
        调用方（searcher）会降级到结构化检索。
        """

        embedder = get_embedding_client()
        vectors = embedder.embed([query.text])
        if vectors is None:
            logger.warning("embedding 不可用，语义检索返回空（调用方应降级）")
            return []

        dense = vectors["dense"][0]
        sparse_raw = vectors["sparse"][0]

        search_filter = build_effect_filter(
            domain_id=query.domain_id,
            tax_types=query.tax_types or None,
            as_of=query.as_of,
        )

        # 稀疏路优先：查询串很短且像文号时（"财税〔2019〕39号"），
        # 精确匹配比语义更可靠，所以走 using=sparse
        sparse_only = query.sparse_only or _query_document_number(query.text) is not None

        if sparse_only:
            response = self.client.query_points(
                collection_name=KB_ARTICLES,
                query=SparseVector(
                    indices=sparse_raw["indices"], values=sparse_raw["values"]
                ),
                using="sparse",
                query_filter=search_filter,
                limit=query.limit,
                with_payload=True,
            )
        else:
            prefetch = [
                {
                    "query": dense,
                    "using": "dense",
                    "limit": query.limit,
                },
                {
                    "query": SparseVector(
                        indices=sparse_raw["indices"], values=sparse_raw["values"]
                    ),
                    "using": "sparse",
                    "limit": query.limit,
                },
            ]
            response = self.client.query_points(
                collection_name=KB_ARTICLES,
                prefetch=prefetch,
                query=FusionQuery(fusion=Fusion.RRF),
                query_filter=search_filter,
                limit=query.limit,
                with_payload=True,
            )

        return [point.payload for point in response.points]


def _query_document_number(text: str) -> str | None:
    """从查询串里识别完整文号，用于判断是否该优先走稀疏路。"""

    import re

    match = re.search(r"[一-龥]{2,20}(?:公告|通告|通知|文件|令)\s*\d{4}\s*年\s*第\s*\d+\s*号", text)
    if match:
        return match.group(0)
    match = re.search(r"[一-龥]{2,12}〔\d{4}〕\d+号", text)
    if match:
        return match.group(0)
    return None
