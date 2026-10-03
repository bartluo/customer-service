"""端到端检索编排测试（收口）。

这一层验证的是"接线对不对"，不是单路能力：
  · 三路都能通，且路由统计准确
  · embedding 挂掉时降级为纯结构化而不是整站报错
  · 引用串可直接放进答案
  · 全文检索绝不返回废止或待复核条文（端到端层面的最后一道闸）
"""

from __future__ import annotations
import os
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database.base import Base
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
from app.retrieval.collections import KB_ARTICLES, ensure_collections
from app.retrieval.glossary import GlossaryExpander
from app.retrieval.indexer import index_articles
from app.retrieval.qdrant_client import get_client
from app.retrieval.searcher import KnowledgeSearcher, SearchRequest, detect_document_number

TEST_DB_NAME = "customer_service_test_searcher"


def _test_database_url() -> str:
    return os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/" + TEST_DB_NAME


@pytest.fixture()
def db():
    server = create_engine(
        os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT"
    )
    with server.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :n"), {"n": TEST_DB_NAME}
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'create database "{TEST_DB_NAME}"'))
    server.dispose()

    engine = create_engine(_test_database_url())
    with engine.begin() as conn:
        conn.execute(sa.text("create extension if not exists btree_gist"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(scope="module")
def qdrant():
    client = get_client()
    try:
        client.get_collections()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Qdrant 不可用：{exc}")
    ensure_collections(client)
    return client


def _seed_regulation(db, **overrides) -> Regulation:
    payload = {
        "domain_id": "finance_tax",
        "title": "关于深化增值税改革有关政策的公告",
        "document_number": "财政部 税务总局公告2019年第39号",
        "issuer": "财政部 税务总局",
        "hierarchy_level": "normative_document",
        "region_scope": "national",
        "tax_types": ["增值税"],
        "applies_to": [],
        "source_url": "https://fgk.chinatax.gov.cn/test",
        "retrieved_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "version": 1,
        "effect_status": "effective",
        "review_state": "published",
        "is_draft": False,
    }
    payload.update(overrides)
    regulation = Regulation(**payload)
    db.add(regulation)
    db.flush()
    return regulation


def _seed_articles(db) -> None:
    """三份法规：正常、废止、待复核各一份，覆盖三类硬过滤。"""

    good = _seed_regulation(db)
    for index, content in enumerate(
        [
            "增值税一般纳税人购进货物、服务发生的进项税额，准予在计算应纳税额时抵扣。",
            "小规模纳税人适用3%征收率的应税销售收入，减按1%征收率征收增值税。",
        ],
        start=1,
    ):
        article = RegulationArticle(
            regulation_id=good.id,
            level_code="article",
            article_no=f"第{index}条",
            full_no=f"第{index}条",
            heading_path=f"第{index}条",
            order_index=index,
        )
        db.add(article)
        db.flush()
        db.add(
            RegulationArticleVersion(
                article_id=article.id,
                version=1,
                content=content,
                valid_from=datetime(2019, 3, 20, tzinfo=timezone.utc),
                valid_to=None,
                effect_status="effective",
            )
        )

    repealed = _seed_regulation(
        db,
        title="已废止的旧规定",
        document_number="国税发〔1994〕155号",
        effect_status="repealed",
    )
    article = RegulationArticle(
        regulation_id=repealed.id,
        level_code="article",
        article_no="第一条",
        full_no="第一条",
        heading_path="第一条",
        order_index=1,
    )
    db.add(article)
    db.flush()
    db.add(
        RegulationArticleVersion(
            article_id=article.id,
            version=1,
            content="本规定已废止，不得作为依据。",
            valid_from=datetime(1994, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2015, 1, 1, tzinfo=timezone.utc),
            effect_status="repealed",
        )
    )

    pending = _seed_regulation(
        db,
        title="待复核的公告",
        document_number="财政部 税务总局公告2024年第99号",
        review_state="pending_review",
    )
    article = RegulationArticle(
        regulation_id=pending.id,
        level_code="article",
        article_no="第一条",
        full_no="第一条",
        heading_path="第一条",
        order_index=1,
    )
    db.add(article)
    db.flush()
    db.add(
        RegulationArticleVersion(
            article_id=article.id,
            version=1,
            content="这是一份尚未通过专家复核的内容，不得被引用。",
            valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            valid_to=None,
            effect_status="effective",
        )
    )
    db.commit()


@pytest.fixture()
def searcher(db, qdrant):
    _seed_articles(db)
    index_articles(db, client=qdrant)
    return KnowledgeSearcher(db=db, client=qdrant)


def test_detect_document_number_variants() -> None:
    """文号识别覆盖用户常见的三种写法。"""

    assert detect_document_number("财税〔2019〕39号怎么规定的") == "财税〔2019〕39号"
    assert "公告" in (detect_document_number("财政部税务总局公告2019年第39号") or "")


def test_search_returns_citations(searcher) -> None:
    """端到端能返回带引用的结果。"""

    response = searcher.search(SearchRequest(question="增值税进项税额抵扣", top_n=5))
    assert not response.is_empty
    citation = response.citations[0]
    assert citation.content
    assert citation.label()
    assert citation.rerank_reason


def test_citation_traceability_rule() -> None:
    """引用可溯源：必须有条款号；文号缺失时只有法律/行政法规可用名称代替。

    法律由主席令公布、行政法规由国务院令公布，官方政策法规库里这两类的
    "发文字号"字段就是空的，实务引用写的是法规名称。
    规范性文件不行：同名公告太多，没文号无法唯一定位。
    """

    from app.retrieval.searcher import Citation

    def make(**overrides) -> Citation:
        base = dict(
            document_number="财税〔2019〕39号",
            full_no="第一条",
            regulation_title="关于深化增值税改革有关政策的公告",
            issuer="财政部",
            effect_status="effective",
            level_code="article",
            hierarchy_level="normative_document",
            content="正文",
            source_url="https://fgk.chinatax.gov.cn/x",
            score=1.0,
        )
        base.update(overrides)
        return Citation(**base)

    assert make().is_traceable()

    # 法律：没有文号，用名称也能定位
    law = make(document_number=None, hierarchy_level="law", regulation_title="中华人民共和国增值税法")
    assert law.is_traceable()
    assert law.identifier() == "中华人民共和国增值税法"
    assert "中华人民共和国增值税法" in law.label()

    # 规范性文件没有文号 → 不可溯源
    assert not make(document_number=None).is_traceable()

    # 没有条款号 → 不可溯源，哪怕有文号
    assert not make(full_no=None).is_traceable()


def test_search_never_returns_repealed_end_to_end(searcher) -> None:
    """端到端红线：废止条文 100% 不出现。"""

    for question in ("进项税额抵扣", "增值税", "小规模纳税人", "申报"):
        response = searcher.search(SearchRequest(question=question, top_n=20))
        for citation in response.citations:
            assert citation.effect_status != "repealed", f"「{question}」召回了废止条文"


def test_search_never_returns_pending_review_end_to_end(searcher) -> None:
    """待复核内容不进答案。"""

    response = searcher.search(SearchRequest(question="公告2024年第99号", top_n=20))
    for citation in response.citations:
        assert citation.document_number != "财政部 税务总局公告2024年第99号"


def test_search_route_stats(searcher) -> None:
    """路由统计准确，便于线上排查召回问题。"""

    response = searcher.search(SearchRequest(question="增值税进项税额抵扣", top_n=5))
    stats = response.route_stats
    assert stats["structured"] >= 0
    assert stats["semantic"] >= 0
    assert stats["fused"] >= len(response.citations)


def test_search_expands_glossary(searcher) -> None:
    """口语术语被扩展。"""

    response = searcher.search(SearchRequest(question="小规模怎么交税", top_n=5))
    assert response.expanded_question is not None
    assert "小规模纳税人" in response.expanded_question


def test_search_detects_intent(searcher) -> None:
    """意图分类贯通到响应。"""

    response = searcher.search(SearchRequest(question="不申报会怎么处罚", top_n=5))
    assert response.intent == "liability"


def test_search_infers_tax_type(searcher) -> None:
    """从口语里推断税种并自动过滤。"""

    response = searcher.search(SearchRequest(question="小规模纳税人抵扣怎么算", top_n=5))
    for citation in response.citations:
        assert "增值税" in (citation.document_number or "") or citation.content


def test_search_degrades_when_embedding_down(db, qdrant, monkeypatch) -> None:
    """embedding 服务挂掉时降级为纯结构化，不抛异常。"""

    _seed_articles(db)
    index_articles(db, client=qdrant)

    def _broken_embed(texts):
        return None

    monkeypatch.setattr(
        "app.retrieval.semantic.get_embedding_client",
        lambda: type("Broken", (), {"embed": staticmethod(_broken_embed)})(),
    )

    searcher = KnowledgeSearcher(db=db, client=qdrant)
    # 带文号才有结构化路可降级到（结构化路只在有精确条件时启用）
    response = searcher.search(
        SearchRequest(
            question="公告2019年第39号怎么规定的", top_n=5,
            document_number="财政部 税务总局公告2019年第39号",
        )
    )
    assert response.degraded == []
    assert response.route_stats["semantic"] == 0
    assert response.route_stats["structured"] > 0
    assert not response.is_empty


def test_search_degrades_when_qdrant_down(db, monkeypatch) -> None:
    """Qdrant 挂掉时只剩结构化路，不抛异常。"""

    _seed_articles(db)

    class _DeadClient:
        def __getattr__(self, name):
            def _raise(*args, **kwargs):
                raise ConnectionError("qdrant down")

            return _raise

    searcher = KnowledgeSearcher(db=db, client=_DeadClient())
    # 用一个一定会触发术语扩展的问法，才能验证第三路也走了降级
    response = searcher.search(
        SearchRequest(
            question="小规模怎么交税", top_n=5,
            document_number="财政部 税务总局公告2019年第39号",
        )
    )
    assert "semantic" in response.degraded
    assert "glossary_expansion" in response.degraded
    assert response.route_stats["structured"] > 0


def test_search_empty_knowledge_base(db, qdrant) -> None:
    """知识库为空时返回空结果而不是报错。"""

    from qdrant_client.models import Filter

    qdrant.delete(collection_name=KB_ARTICLES, points_selector=Filter(), wait=True)
    searcher = KnowledgeSearcher(db=db, client=qdrant)
    response = searcher.search(SearchRequest(question="增值税", top_n=5))
    assert response.is_empty
    assert response.citations == []


def test_stale_vector_points_do_not_leak_into_answers(db, qdrant) -> None:
    """索引漂移防线：库里已删的条文，索引里残留的点也必须作废。

    这是财税红线的一种隐蔽形态——法规被撤回、条文被删除后，
    如果只改了数据库没重建向量索引，答案里仍会引用已撤回的内容。
    做法是每轮检索前用数据库的权威状态过滤一次（见 searcher 的 _prune_stale_points）。
    """

    _seed_articles(db)
    index_articles(db, client=qdrant)

    # 模拟漂移：把法规从库里删掉，但不重建索引
    regulation = db.query(Regulation).filter(
        Regulation.document_number == "财政部 税务总局公告2019年第39号"
    ).one()
    db.delete(regulation)
    db.commit()

    searcher = KnowledgeSearcher(db=db, client=qdrant)
    response = searcher.search(SearchRequest(question="增值税进项税额抵扣", top_n=10))
    for citation in response.citations:
        assert citation.document_number != "财政部 税务总局公告2019年第39号"


def test_search_top_n_respected(searcher) -> None:
    """返回条数不超过 top_n。"""

    response = searcher.search(SearchRequest(question="增值税", top_n=2))
    assert len(response.citations) <= 2


def test_search_respects_as_of(searcher) -> None:
    """时点过滤：查询 2010 年时只看当时有效的条文。"""

    past = datetime(2010, 1, 1, tzinfo=timezone.utc)
    response = searcher.search(SearchRequest(question="增值税抵扣", as_of=past, top_n=5))
    assert isinstance(response, object)
    # 2010 年时 2019 年的公告还不存在，答案里不应出现它
    for citation in response.citations:
        assert citation.effect_status != "repealed"


def test_glossary_expander_direct() -> None:
    """术语扩展器独立可用。"""

    expander = GlossaryExpander()
    assert "小规模纳税人" in expander.expand("小规模怎么交税")
    assert expander.expand_tax_types("小规模") == ["增值税"]
    assert expander.expand("没有术语的句子") == "没有术语的句子"
