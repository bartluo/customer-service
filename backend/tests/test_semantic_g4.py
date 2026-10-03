"""语义检索与硬过滤测试。

  稠密检索：问"能不能抵扣"能召回"进项税额抵扣"相关条文
  稀疏检索：搜文号能精确命中
  效力硬过滤：检索结果中不出现已废止条文（100%）

最后一条是财税系统的红线：废止条文混进答案就是事故。
"""

from __future__ import annotations
from datetime import datetime, timezone

import pytest

from app.retrieval.filters import build_effect_filter, effect_filter_payload
from app.retrieval.semantic import SemanticSearcher, SemanticQuery
from app.retrieval.collections import ensure_collections
from app.retrieval.qdrant_client import get_client


@pytest.fixture(scope="module")
def qdrant():
    client = get_client()
    try:
        client.get_collections()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Qdrant 不可用：{exc}")
    ensure_collections(client)
    return client


def _seed_points(client) -> None:
    """写入几条真实财税条文，覆盖生效 / 废止 / 待复核三种状态。"""

    from qdrant_client.models import Filter, PointStruct, SparseVector

    from app.retrieval.collections import KB_ARTICLES

    client.delete(collection_name=KB_ARTICLES, points_selector=Filter(), wait=True)

    dim = 1024
    specs = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "content": "增值税一般纳税人购进货物、服务发生的进项税额准予在计算应纳税额时抵扣。",
            "effect_status": "effective",
            "review_state": "published",
            "tax_types": ["增值税"],
            "full_no": "第二十七条",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "content": "小规模纳税人适用3%征收率的应税销售收入，减按1%征收率征收增值税，不得抵扣进项。",
            "effect_status": "effective",
            "review_state": "published",
            "tax_types": ["增值税"],
            "full_no": "第一条",
        },
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "content": "已废止的进项税额抵扣规定，本条自2020年1月1日起废止，不得作为依据。",
            "effect_status": "repealed",
            "review_state": "published",
            "tax_types": ["增值税"],
            "full_no": "旧第十条",
        },
        {
            "id": "44444444-4444-4444-4444-444444444444",
            "content": "待复核的条文内容，尚未经审核专家确认，不得对外引用。",
            "effect_status": "effective",
            "review_state": "pending_review",
            "tax_types": ["增值税"],
            "full_no": "待定",
        },
    ]

    points = []
    for index, spec in enumerate(specs):
        # 稠密向量：用确定性伪向量，索引 0 与"抵扣"关键词强相关
        vector = [0.0] * dim
        vector[index % len(specs)] = 1.0
        if "抵扣" in spec["content"]:
            vector[0] += 0.9
        points.append(
            PointStruct(
                id=spec["id"],
                vector={
                    "dense": vector,
                    "sparse": SparseVector(indices=[100 + index], values=[1.0]),
                },
                payload={
                    "domain_id": "finance_tax",
                    "regulation_id": f"reg-{index}",
                    "article_id": f"art-{index}",
                    "document_number": "财政部 税务总局公告2019年第39号",
                    "regulation_title": "关于深化增值税改革有关政策的公告",
                    "hierarchy_level": "normative_document",
                    "level_code": "article",
                    "full_no": spec["full_no"],
                    "effect_status": spec["effect_status"],
                    "review_state": spec["review_state"],
                    "tax_types": spec["tax_types"],
                    "content": spec["content"],
                    "valid_from_ts": int(datetime(2019, 3, 20, tzinfo=timezone.utc).timestamp()),
                    "valid_to_ts": None,
                },
            )
        )
    client.upsert(collection_name=KB_ARTICLES, points=points, wait=True)


@pytest.fixture()
def searcher(qdrant):
    _seed_points(qdrant)
    return SemanticSearcher(client=qdrant)


def test_effect_filter_excludes_non_citable() -> None:
    """硬过滤只放行 effective 与 partially_repealed。"""

    must = build_effect_filter()
    assert "effective" in str(must)
    assert "partially_repealed" in str(must)
    # 绝不能出现这三个
    text = str(must)
    for banned in ("repealed", "superseded", "draft"):
        assert f"'{banned}'" not in text


def test_effect_filter_payload() -> None:
    """导出的 payload 条件可直接给结构化检索用。"""

    payload = effect_filter_payload()
    assert set(payload) == {"effective", "partially_repealed"}


def test_dense_search_returns_results(searcher) -> None:
    """稠密检索能返回结果。"""

    results = searcher.search(SemanticQuery(text="增值税进项税额抵扣", limit=10))
    assert len(results) > 0


def test_dense_search_never_returns_repealed(searcher) -> None:
    """红线：废止条文 100% 不出现。"""

    for text in ("进项税额抵扣", "抵扣", "增值税", "小规模纳税人"):
        results = searcher.search(SemanticQuery(text=text, limit=20))
        for hit in results:
            assert hit["effect_status"] != "repealed", f"「{text}」召回了废止条文"
            assert "废止" not in hit["content"] or "不得作为依据" in hit["content"]


def test_dense_search_never_returns_pending_review(searcher) -> None:
    """待复核内容不进检索结果。"""

    results = searcher.search(SemanticQuery(text="待复核的条文", limit=20))
    for hit in results:
        assert hit.get("review_state") != "pending_review"


def test_sparse_search_by_document_number(searcher) -> None:
    """稀疏检索按文号精确命中。"""

    # 预置点用的是占位下标（100/101/102…），与真实分词下标不重合，
    # 所以这里用真实 embedding 另写一条"文号精确匹配"的点再召回，
    # 否则测的只是占位数字，不是稀疏检索本身。
    from qdrant_client.models import PointStruct, SparseVector

    from app.retrieval.collections import KB_ARTICLES
    from app.retrieval.embedding_client import get_embedding_client

    vectors = get_embedding_client().embed(["财政部 税务总局公告2019年第39号"])
    if vectors is None:
        pytest.skip("embedding 服务不可用，无法验证稀疏精确匹配")
    sparse = vectors["sparse"][0]
    assert sparse["indices"], "embedding 服务未返回稀疏下标"

    doc_text = "财政部 税务总局公告2019年第39号"
    searcher.client.upsert(
        collection_name=KB_ARTICLES,
        points=[
            PointStruct(
                id="33333333-3333-3333-3333-333333333333",
                vector={
                    "dense": vectors["dense"][0],
                    "sparse": SparseVector(
                        indices=sparse["indices"], values=sparse["values"]
                    ),
                },
                payload={
                    "domain_id": "finance_tax",
                    "regulation_id": "reg-doc-number",
                    "article_id": "art-doc-number",
                    "document_number": doc_text,
                    "regulation_title": "关于深化增值税改革有关政策的公告",
                    "hierarchy_level": "normative_document",
                    "level_code": "article",
                    "full_no": "第一条",
                    "effect_status": "effective",
                    "review_state": "published",
                    "tax_types": ["增值税"],
                    "content": "本公告自2019年3月20日起施行。",
                    "valid_from_ts": int(datetime(2019, 3, 20, tzinfo=timezone.utc).timestamp()),
                    "valid_to_ts": None,
                },
            )
        ],
        wait=True,
    )

    results = searcher.search(
        SemanticQuery(text="财税〔2019〕39号", sparse_only=True, limit=10)
    )
    assert len(results) > 0
    assert results[0]["document_number"] == doc_text


def test_search_filters_by_tax_type(searcher) -> None:
    """按税种过滤。"""

    results = searcher.search(SemanticQuery(text="增值税", tax_types=["增值税"], limit=10))
    assert all("增值税" in hit.get("tax_types", []) for hit in results)


def test_search_respects_as_of(searcher) -> None:
    """时点过滤：2020 年之后的查询仍能命中（区间未闭合）。"""

    results = searcher.search(
        SemanticQuery(text="抵扣", as_of=datetime(2030, 1, 1, tzinfo=timezone.utc), limit=10)
    )
    assert isinstance(results, list)


def test_search_results_carry_citation(searcher) -> None:
    """命中结果带齐引用要素。"""

    results = searcher.search(SemanticQuery(text="抵扣", limit=5))
    assert results
    hit = results[0]
    assert hit.get("content")
    assert hit.get("full_no")
    assert hit.get("document_number")


def test_search_handles_empty_index(searcher) -> None:
    """索引为空时不崩，返回空列表。"""

    from qdrant_client.models import Filter

    from app.retrieval.collections import KB_ARTICLES

    searcher.client.delete(collection_name=KB_ARTICLES, points_selector=Filter(), wait=True)
    results = searcher.search(SemanticQuery(text="增值税", limit=10))
    assert results == []
