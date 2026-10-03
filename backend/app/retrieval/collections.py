"""Qdrant 集合定义与创建（技术方案 5.2）。

五个集合的职责：
  kb_knowledge  知识单元（从条文提炼的可独立回答片段）
  kb_articles   结构化条文（引用与过滤的主力）
  kb_chunks     原文块（要展示原文时用）
  kb_cases      例题 / 案例
  kb_history    历史问答（相似问题召回）

两条命名向量规范（沿用 v2 的混合检索设计）：
  dense   稠密语义向量，1024 维（bge-m3）
  sparse  稀疏词权重向量，用于文号、条款号这类精确串匹配

payload 索引为什么重要：财税的硬过滤（效力状态 + 时点 + 地域）
每次检索都要用，走 payload 索引才不会全表扫描。
"""

from __future__ import annotations
import logging
import os

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PayloadSchemaType,
    SparseVectorParams,
    VectorParams,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

# 集合名前缀。为什么需要它：
#   自动化测试里有一批用例要"清空集合再灌数据"来验证索引与检索，
#   而测试连的是同一个 Qdrant 实例。不隔离的话，跑一次 pytest 就会
#   把开发环境里几万条真实向量删干净——索引看着还在，其实已经空了。
#   测试通过 QDRANT_COLLECTION_PREFIX=test_ 用另一套集合，互不影响。
COLLECTION_PREFIX = os.getenv("QDRANT_COLLECTION_PREFIX", "").strip()

KB_ARTICLES = f"{COLLECTION_PREFIX}kb_articles"
KB_KNOWLEDGE = f"{COLLECTION_PREFIX}kb_knowledge"
KB_CHUNKS = f"{COLLECTION_PREFIX}kb_chunks"
KB_CASES = f"{COLLECTION_PREFIX}kb_cases"
KB_HISTORY = f"{COLLECTION_PREFIX}kb_history"

COLLECTIONS: tuple[str, ...] = (
    KB_ARTICLES,
    KB_KNOWLEDGE,
    KB_CHUNKS,
    KB_CASES,
    KB_HISTORY,
)

# keyword 型适合等值过滤（效力状态、域、ID），
# integer 型适合范围过滤（valid_from 时间戳）。
PAYLOAD_INDEXES: dict[str, dict[str, PayloadSchemaType]] = {
    KB_ARTICLES: {
        "domain_id": PayloadSchemaType.KEYWORD,
        "effect_status": PayloadSchemaType.KEYWORD,
        "article_id": PayloadSchemaType.KEYWORD,
        "regulation_id": PayloadSchemaType.KEYWORD,
        "hierarchy_level": PayloadSchemaType.KEYWORD,
        "level_code": PayloadSchemaType.KEYWORD,
        "valid_from_ts": PayloadSchemaType.INTEGER,
        "valid_to_ts": PayloadSchemaType.INTEGER,
    },
    KB_KNOWLEDGE: {
        "domain_id": PayloadSchemaType.KEYWORD,
        "status": PayloadSchemaType.KEYWORD,
        "subdomain": PayloadSchemaType.KEYWORD,
        "regulation_id": PayloadSchemaType.KEYWORD,
        "valid_from_ts": PayloadSchemaType.INTEGER,
        "valid_to_ts": PayloadSchemaType.INTEGER,
    },
    KB_CHUNKS: {
        "domain_id": PayloadSchemaType.KEYWORD,
        "doc_id": PayloadSchemaType.KEYWORD,
        "regulation_id": PayloadSchemaType.KEYWORD,
        "page": PayloadSchemaType.INTEGER,
    },
    KB_CASES: {
        "domain_id": PayloadSchemaType.KEYWORD,
        "tax_type": PayloadSchemaType.KEYWORD,
        "difficulty": PayloadSchemaType.KEYWORD,
    },
    KB_HISTORY: {
        "domain_id": PayloadSchemaType.KEYWORD,
        "conversation_id": PayloadSchemaType.KEYWORD,
    },
}


def _named_vectors(dense_dim: int) -> dict:
    """dense 命名向量定义。"""

    return {"dense": VectorParams(size=dense_dim, distance=Distance.COSINE)}


def _sparse_vectors() -> dict:
    """sparse 命名向量定义。"""

    return {"sparse": SparseVectorParams()}


def ensure_collections(client: QdrantClient, force: bool = False) -> dict[str, bool]:
    """确保五个集合存在并带好索引。幂等。

    force=True 会删除重建——只在测试与紧急重建索引时使用，会丢数据。
    返回 {集合名: 是否本次新建}。

    已存在的集合不再重复建索引：payload 索引创建是异步任务，
    每次启动都提交一遍会让启动多花几十秒且没有任何收益。
    需要补索引时用 rebuild_indexes() 或 ensure_collections(force=True)。
    """

    settings = get_settings()
    dense_dim = settings.dense_dim

    existing = {c.name for c in client.get_collections().collections}
    created: dict[str, bool] = {}

    for name in COLLECTIONS:
        if name in existing:
            if force:
                logger.warning("强制重建集合 %s，原有向量将丢失", name)
                client.delete_collection(name)
            else:
                created[name] = False
                continue

        client.create_collection(
            collection_name=name,
            vectors_config=_named_vectors(dense_dim),
            sparse_vectors_config=_sparse_vectors(),
        )
        created[name] = True
        _ensure_indexes(client, name)
        logger.info("已创建集合 %s（dense=%d + sparse）", name, dense_dim)

    return created


def _ensure_indexes(client: QdrantClient, name: str) -> None:
    """建 payload 索引。已存在时 Qdrant 会返回冲突，忽略即可（幂等）。"""

    for field, schema in PAYLOAD_INDEXES.get(name, {}).items():
        try:
            client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=schema,
            )
        except Exception:  # noqa: BLE001 - 索引已存在是正常情况，不该让启动失败
            logger.debug("payload 索引 %s.%s 已存在或不可创建", name, field, exc_info=True)


def healthcheck_collections(client: QdrantClient) -> dict:
    """检查集合是否齐备，供健康检查接口使用。"""

    try:
        existing = {c.name for c in client.get_collections().collections}
    except Exception as exc:  # noqa: BLE001
        return {
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}",
            "missing": list(COLLECTIONS),
        }

    missing = [name for name in COLLECTIONS if name not in existing]
    return {
        "ready": not missing,
        "existing": sorted(existing & set(COLLECTIONS)),
        "missing": missing,
        "total_expected": len(COLLECTIONS),
    }
