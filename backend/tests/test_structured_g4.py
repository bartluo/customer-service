"""结构化检索测试。

按税种、适用对象、文号、条款号、效力状态精确过滤。
这一路走 PostgreSQL 而不是向量库——用户问"财税〔2019〕39号第一条"时，
要的是精确命中，语义相似反而是干扰。
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
from app.retrieval.structured import (
    StructuredQuery,
    search_structured,
)

TEST_DB_NAME = "customer_service_test"


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


def _test_database_url() -> str:
    return os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/" + TEST_DB_NAME


def _seed(db) -> None:
    """造三份法规：增值税优惠（小规模）、企业所得税（研发费用）、印花税。"""

    specs = [
        {
            "title": "关于增值税小规模纳税人减免增值税政策的公告",
            "document_number": "财政部 税务总局公告2023年第19号",
            "tax_types": ["增值税"],
            "applies_to": ["小规模纳税人"],
            "hierarchy_level": "normative_document",
            "articles": [
                ("第一条", "对月销售额10万元以下的增值税小规模纳税人，免征增值税。"),
                ("第二条", "小规模纳税人适用3%征收率的应税销售收入，减按1%征收率征收增值税。"),
            ],
        },
        {
            "title": "关于企业研发费用加计扣除政策的公告",
            "document_number": "财政部 税务总局公告2023年第7号",
            "tax_types": ["企业所得税"],
            "applies_to": ["一般纳税人"],
            "hierarchy_level": "normative_document",
            "articles": [
                ("第一条", "企业开展研发活动实际发生的研发费用，再按照实际发生额的100%在税前扣除。"),
            ],
        },
        {
            "title": "中华人民共和国印花税法",
            "document_number": "中华人民共和国主席令第六十三号",
            "tax_types": ["印花税"],
            "applies_to": [],
            "hierarchy_level": "law",
            "articles": [
                ("第一条", "在中华人民共和国境内书立应税凭证、进行证券交易的单位和个人，应当依法缴纳印花税。"),
            ],
        },
    ]

    for index, spec in enumerate(specs):
        regulation = Regulation(
            domain_id="finance_tax",
            title=spec["title"],
            document_number=spec["document_number"],
            issuer="财政部 税务总局",
            hierarchy_level=spec["hierarchy_level"],
            region_scope="national",
            tax_types=spec["tax_types"],
            applies_to=spec["applies_to"],
            source_url="https://fgk.chinatax.gov.cn/test",
            retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            publish_date=datetime(2023, 1, 1, tzinfo=timezone.utc),
            effective_date=datetime(2023, 1, 1, tzinfo=timezone.utc),
            version=1,
            effect_status="effective",
            review_state="published",
            is_draft=False,
        )
        db.add(regulation)
        db.flush()
        for order, (no, content) in enumerate(spec["articles"]):
            article = RegulationArticle(
                regulation_id=regulation.id,
                level_code="article",
                article_no=no,
                full_no=f"第一章 {no}",
                heading_path=f"第一章 {no}",
                order_index=order,
            )
            db.add(article)
            db.flush()
            db.add(
                RegulationArticleVersion(
                    article_id=article.id,
                    version=1,
                    content=content,
                    valid_from=datetime(2023, 1, 1, tzinfo=timezone.utc),
                    valid_to=None,
                    effect_status="effective",
                )
            )
    db.commit()


def test_search_by_tax_type(db) -> None:
    """按税种过滤：增值税只召回增值税相关。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(tax_types=["增值税"]))
    assert len(hits) == 2
    assert all("增值税" in hit.tax_types for hit in hits)


def test_search_by_document_number(db) -> None:
    """按文号精确命中。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(document_number="财政部 税务总局公告2023年第19号"))
    assert len(hits) == 2
    assert all(hit.document_number == "财政部 税务总局公告2023年第19号" for hit in hits)


def test_search_by_applies_to(db) -> None:
    """按适用对象过滤：小规模纳税人。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(applies_to=["小规模纳税人"]))
    assert len(hits) == 2


def test_search_by_hierarchy_level(db) -> None:
    """按位阶过滤：只取法律。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(hierarchy_levels=["law"]))
    assert len(hits) == 1
    assert hits[0].hierarchy_level == "law"


def test_search_excludes_repealed(db) -> None:
    """废止条文不召回——财税硬要求。"""

    _seed(db)
    regulation = db.query(Regulation).filter_by(
        document_number="财政部 税务总局公告2023年第19号"
    ).one()
    for version in db.query(RegulationArticleVersion).join(RegulationArticle).filter(
        RegulationArticle.regulation_id == regulation.id
    ):
        version.effect_status = "repealed"
    db.commit()

    hits = search_structured(db, StructuredQuery(tax_types=["增值税"]))
    assert len(hits) == 0


def test_search_excludes_pending_review(db) -> None:
    """待复核法规不召回。"""

    _seed(db)
    regulation = db.query(Regulation).filter_by(
        document_number="财政部 税务总局公告2023年第19号"
    ).one()
    regulation.review_state = "pending_review"
    db.commit()

    hits = search_structured(db, StructuredQuery(tax_types=["增值税"]))
    assert len(hits) == 0


def test_search_by_keyword_in_content(db) -> None:
    """按内容关键词过滤。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(keyword="研发费用"))
    assert len(hits) == 1
    assert "研发费用" in hits[0].content


def test_search_by_full_no(db) -> None:
    """按条款号过滤："第一章 第一条"。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(article_no="第一条"))
    assert len(hits) == 3


def test_search_combines_filters(db) -> None:
    """多条件组合：税种 + 适用对象。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(tax_types=["增值税"], applies_to=["小规模纳税人"]))
    assert len(hits) == 2

    hits = search_structured(db, StructuredQuery(tax_types=["企业所得税"], applies_to=["小规模纳税人"]))
    assert len(hits) == 0


def test_search_respects_limit(db) -> None:
    _seed(db)
    hits = search_structured(db, StructuredQuery(limit=1))
    assert len(hits) == 1


def test_hit_carries_citation_fields(db) -> None:
    """命中结果必须带齐引用要素：文号、条号、层级、来源。"""

    _seed(db)
    hit = search_structured(db, StructuredQuery(tax_types=["增值税"]))[0]
    assert hit.document_number
    assert hit.full_no
    assert hit.level_code == "article"
    assert hit.source_url or hit.regulation_source_url
    assert hit.content


def test_time_point_filter(db) -> None:
    """时点过滤：2022 年的查询不该召回 2023 年生效的条文。"""

    _seed(db)
    hits = search_structured(db, StructuredQuery(as_of=datetime(2022, 6, 1, tzinfo=timezone.utc)))
    assert len(hits) == 0

    hits = search_structured(db, StructuredQuery(as_of=datetime(2024, 6, 1, tzinfo=timezone.utc)))
    assert len(hits) == 4
