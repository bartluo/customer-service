"""Qdrant 集合与索引测试。

对应技术方案 5.2：五个集合，其中 kb_articles 与 kb_knowledge 挂命名向量
（dense + sparse 双组）。

为什么用真实 Qdrant：集合参数（向量名、维度、稀疏索引、payload 索引）
是本阶段最容易配错的地方，内存 mock 测不出来。配错的典型症状是
"写入成功但检索永远查不到"，很难定位。
"""

from __future__ import annotations
import uuid

import pytest

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


@pytest.fixture(scope="module")
def qdrant():
    client = get_client()
    try:
        client.get_collections()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Qdrant 不可用：{exc}")
    # 模块级只建一次集合：Qdrant 建集合 + 建索引要 30 秒，
    # 每个测试都重建会让整个套件慢到无法使用。
    ensure_collections(client)
    return client


def _cleanup(client) -> None:
    """清空集合里的点，保留集合与索引。

    删集合再重建要 30 秒（索引是异步任务），而 scroll+delete 只需毫秒级。
    """

    from qdrant_client.models import Filter

    for name in COLLECTIONS:
        try:
            client.delete(collection_name=name, points_selector=Filter(), wait=True)
        except Exception:  # noqa: BLE001 - 集合不存在时忽略
            pass


def test_ensure_collections_creates_all_five(qdrant) -> None:
    """技术方案 5.2 的五个集合都要建出来。"""

    names = {c.name for c in qdrant.get_collections().collections}
    for expected in (KB_ARTICLES, KB_KNOWLEDGE, KB_CHUNKS, KB_CASES, KB_HISTORY):
        assert expected in names, f"缺少集合 {expected}"


def test_kb_articles_has_named_vectors(qdrant) -> None:
    """kb_articles 必须有 dense + sparse 两组命名向量。"""

    info = qdrant.get_collection(KB_ARTICLES)

    vector_names = set()
    if getattr(info.config.params, "vectors", None):
        vector_names = set(info.config.params.vectors.keys())
    sparse_names = set()
    if getattr(info.config.params, "sparse_vectors", None):
        sparse_names = set(info.config.params.sparse_vectors.keys())

    assert "dense" in vector_names, f"缺少稠密向量，实际：{vector_names}"
    assert "sparse" in sparse_names, f"缺少稀疏向量，实际：{sparse_names}"


def test_kb_knowledge_has_named_vectors(qdrant) -> None:
    """kb_knowledge 同样挂 dense + sparse。"""

    info = qdrant.get_collection(KB_KNOWLEDGE)

    vector_names = set(info.config.params.vectors.keys())
    sparse_names = set(info.config.params.sparse_vectors.keys())
    assert "dense" in vector_names
    assert "sparse" in sparse_names


def test_dense_dim_is_1024(qdrant) -> None:
    """bge-m3 输出 1024 维，维度错了写入会直接报错。"""

    info = qdrant.get_collection(KB_ARTICLES)
    assert info.config.params.vectors["dense"].size == 1024


def test_ensure_collections_is_idempotent(qdrant) -> None:
    """重复执行不能报错，也不能重建集合（会丢数据）。"""

    before = qdrant.count(collection_name=KB_ARTICLES, exact=True).count
    ensure_collections(qdrant)
    ensure_collections(qdrant)
    names = [c.name for c in qdrant.get_collections().collections]
    assert names.count(KB_ARTICLES) == 1
    assert qdrant.count(collection_name=KB_ARTICLES, exact=True).count == before


def test_payload_indexes_created(qdrant) -> None:
    """payload 索引：按效力状态、域、条文过滤是最高频操作。"""

    info = qdrant.get_collection(KB_ARTICLES)

    payload_schema = getattr(info, "payload_schema", None)
    if payload_schema is None:
        payload_schema = getattr(info.config, "payload_schema", {})
    indexed = set(payload_schema.keys())
    for field in ("domain_id", "effect_status", "article_id"):
        assert field in indexed, f"缺少 payload 索引 {field}，实际：{indexed}"


def test_healthcheck_reports_missing(qdrant) -> None:
    """健康检查要能指出哪些集合缺失。"""

    from app.retrieval.collections import COLLECTIONS as ALL

    class _FakeEmpty:
        """模拟"一个集合都没有"的 Qdrant。"""

        class _Resp:
            collections: list = []

        def get_collections(self):
            return self._Resp()

    report = healthcheck_collections(qdrant)
    assert report["ready"] is True
    assert report["missing"] == []

    report_empty = healthcheck_collections(_FakeEmpty())
    assert report_empty["ready"] is False
    assert set(report_empty["missing"]) == set(ALL)


def test_healthcheck_ready_after_ensure(qdrant) -> None:
    report = healthcheck_collections(qdrant)
    assert report["ready"] is True
    assert report["missing"] == []


def test_write_and_read_back_dense_and_sparse(qdrant) -> None:
    """写入再读回，确认 payload 过滤可用（这是硬过滤的基础）。"""

    _cleanup(qdrant)

    from qdrant_client.models import PointStruct, SparseVector

    point_id = str(uuid.uuid4())
    qdrant.upsert(
        collection_name=KB_ARTICLES,
        points=[
            PointStruct(
                id=point_id,
                vector={
                    "dense": [0.1] * 1024,
                    "sparse": SparseVector(indices=[1, 100], values=[0.8, 0.6]),
                },
                payload={
                    "domain_id": "finance_tax",
                    "effect_status": "effective",
                    "article_id": "art-test",
                    "content": "增值税一般纳税人适用13%税率。",
                },
            )
        ],
    )

    hits = qdrant.scroll(
        collection_name=KB_ARTICLES,
        scroll_filter=None,
        limit=10,
        with_payload=True,
    )[0]
    assert len(hits) == 1
    assert hits[0].payload["effect_status"] == "effective"

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    filtered = qdrant.scroll(
        collection_name=KB_ARTICLES,
        scroll_filter=Filter(
            must=[FieldCondition(key="effect_status", match=MatchValue(value="repealed"))]
        ),
        limit=10,
    )[0]
    assert filtered == []
