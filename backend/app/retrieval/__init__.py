"""检索层：三路召回 + RRF 融合 + 硬过滤 + 重排（技术方案 5.2 / 5.3）。

模块划分：
  qdrant_client  Qdrant 连接与集合管理
  collections    五个集合的定义与建集合逻辑
  embedding      调用 embedding 服务取稠密 + 稀疏向量
  structured     结构化精确检索（走 PostgreSQL）
  semantic       稠密 + 稀疏检索（走 Qdrant）
  filters        效力状态与时点硬过滤
  fusion         RRF 融合与加权
  rerank         重排
  searcher       端到端编排
"""

from app.retrieval.collections import (
    COLLECTIONS,
    KB_ARTICLES,
    KB_CASES,
    KB_CHUNKS,
    KB_HISTORY,
    KB_KNOWLEDGE,
    ensure_collections,
    healthcheck_collections,
)
from app.retrieval.qdrant_client import get_client
from app.retrieval.indexes import missing_indexes, rebuild_indexes
from app.retrieval.indexer import (
    build_regulation_entries,
    index_articles,
    reindex_regulation,
    remove_regulation_points,
)
from app.retrieval.structured import StructuredHit, StructuredQuery, search_structured
from app.retrieval.semantic import SemanticQuery, SemanticSearcher
from app.retrieval.filters import build_effect_filter, effect_filter_payload

__all__ = [
    "COLLECTIONS",
    "KB_ARTICLES",
    "KB_CASES",
    "KB_CHUNKS",
    "KB_HISTORY",
    "KB_KNOWLEDGE",
    "ensure_collections",
    "get_client",
    "healthcheck_collections",
    "missing_indexes",
    "rebuild_indexes",
    "index_articles",
    "reindex_regulation",
    "remove_regulation_points",
    "build_regulation_entries",
    "StructuredHit",
    "StructuredQuery",
    "search_structured",
    "SemanticQuery",
    "SemanticSearcher",
    "build_effect_filter",
    "effect_filter_payload",
    "DEFAULT_WEIGHTS",
    "FusedHit",
    "FusionWeights",
    "apply_boosts",
    "reciprocal_rank_fusion",
    "RerankResult",
    "HeuristicReranker",
    "rerank",
    "DEFAULT_TERMS",
    "GlossaryExpander",
    "GlossaryTerm",
    "Citation",
    "KnowledgeSearcher",
    "SearchRequest",
    "SearchResponse",
    "detect_document_number",
    "build_index_entries",
]
from app.retrieval.fusion import (
    DEFAULT_WEIGHTS,
    FusedHit,
    FusionWeights,
    apply_boosts,
    reciprocal_rank_fusion,
)
from app.retrieval.rerank import HeuristicReranker, RerankResult, rerank
from app.retrieval.glossary import DEFAULT_TERMS, GlossaryExpander, GlossaryTerm
from app.retrieval.searcher import (
    Citation,
    KnowledgeSearcher,
    SearchRequest,
    SearchResponse,
    detect_document_number,
)
from app.retrieval.indexer import build_index_entries
