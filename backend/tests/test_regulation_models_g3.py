"""财税法规结构化模型的数据库层测试（需要真实的 PostgreSQL）。

对应技术方案 v3.3 第 10.2 节与验收门 G3 的硬约束：
  · 条文版本必须带 [valid_from, valid_to)，不允许空区间
  · 同一条文的任意两个版本区间不得重叠
  · 条文引用单位精确到 条/款/项

为什么用真实数据库而不是内存 SQLite：区间重叠约束要靠 PostgreSQL 的
 排斥约束（EXCLUDE USING gist）实现，这是本模型防"废止条文混入答案"的地基，
  SQLite 测了也测不出来。
"""

from __future__ import annotations
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.database.base import Base
from app.models.knowledge import (
    Regulation,
    RegulationArticle,
    RegulationArticleVersion,
    RegulationRelation,
)

TEST_DB_NAME = "customer_service_test"


def _test_database_url() -> str:
    base = os.environ["DATABASE_URL"]
    return base.rsplit("/", 1)[0] + "/" + TEST_DB_NAME


def _ensure_test_database() -> None:
    base = os.environ["DATABASE_URL"]
    server_url = base.rsplit("/", 1)[0] + "/postgres"
    engine = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :name"),
            {"name": TEST_DB_NAME},
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'create database "{TEST_DB_NAME}"'))
    engine.dispose()


def _ensure_btree_gist(engine) -> None:
    """确保 btree_gist 扩展可用。

    为什么需要：区间不重叠用 PostgreSQL 的排除约束（EXCLUDE USING gist）实现，
    而 GiST 索引默认不认识 UUID，必须靠 btree_gist 扩展把 UUID 的等值比较
    也注册进 GiST。这是 PostgreSQL 官方 contrib 扩展，不引入第三方代码。
    """

    with engine.begin() as conn:
        conn.execute(sa.text("create extension if not exists btree_gist"))


@pytest.fixture()
def db() -> Session:
    _ensure_test_database()
    engine = create_engine(_test_database_url())
    _ensure_btree_gist(engine)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_regulation(session: Session, **overrides) -> Regulation:
    payload = {
        "domain_id": "finance_tax",
        "title": "关于深化增值税改革有关政策的公告",
        "document_number": "财政部 税务总局 海关总署公告2019年第39号",
        "issuer": "财政部、国家税务总局、海关总署",
        "hierarchy_level": "normative_document",
        "region_scope": "national",
        "tax_types": ["增值税"],
        "applies_to": ["一般纳税人"],
        "source_url": "https://example.gov.cn/39hao",
        "retrieved_at": datetime(2026, 9, 29, tzinfo=timezone.utc),
        "version": 1,
        "effect_status": "effective",
        "review_state": "pending_review",
        "is_draft": False,
    }
    payload.update(overrides)
    regulation = Regulation(**payload)
    session.add(regulation)
    session.flush()
    return regulation


def _make_article(session: Session, regulation: Regulation, **overrides) -> RegulationArticle:
    payload = {
        "regulation_id": regulation.id,
        "level_code": "article",
        "article_no": "第一条",
        "order_index": 1,
        "heading_path": "第一章 总则/第一条",
        "parent_article_id": None,
        "full_no": "第一条",
    }
    payload.update(overrides)
    article = RegulationArticle(**payload)
    session.add(article)
    session.flush()
    return article


def _make_version(session: Session, article: RegulationArticle, **overrides) -> RegulationArticleVersion:
    payload = {
        "article_id": article.id,
        "version": 1,
        "content": "增值税一般纳税人发生销售服务、无形资产、不动产应税行为，适用税率13%。",
        "valid_from": datetime(2019, 3, 20, tzinfo=timezone.utc),
        "valid_to": None,
        "effect_status": "effective",
        "source_file_key": "finance_tax/2019/39hao.pdf",
        "source_url": "https://example.gov.cn/39hao",
        "content_hash": "h" * 64,
    }
    payload.update(overrides)
    version = RegulationArticleVersion(**payload)
    session.add(version)
    session.flush()
    return version


def test_regulation_metadata_roundtrip(db: Session) -> None:
    """法规主表的全部元数据字段都能落库并读回。"""

    regulation = _make_regulation(db)
    db.commit()

    loaded = db.get(Regulation, regulation.id)
    assert loaded is not None
    assert loaded.document_number == "财政部 税务总局 海关总署公告2019年第39号"
    assert loaded.hierarchy_level == "normative_document"
    assert loaded.tax_types == ["增值税"]
    assert loaded.effect_status == "effective"
    assert loaded.source_url.startswith("https://")
    assert loaded.retrieved_at is not None


def test_regulation_source_is_required(db: Session) -> None:
    """技术方案 10.3：没有来源留痕的法规一律不收。"""

    regulation = Regulation(
        domain_id="finance_tax",
        title="没有来源的法规",
        document_number=None,
        issuer="财政部",
        hierarchy_level="normative_document",
        region_scope="national",
        tax_types=[],
        applies_to=[],
        source_url=None,
        retrieved_at=datetime(2026, 9, 29, tzinfo=timezone.utc),
        version=1,
        effect_status="effective",
        review_state="pending_review",
        is_draft=False,
    )
    db.add(regulation)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_article_version_interval_is_required(db: Session) -> None:
    """技术方案 4.1 time_range_required：每个版本必须带 [valid_from, valid_to)。"""

    regulation = _make_regulation(db)
    article = _make_article(db, regulation)
    version = RegulationArticleVersion(
        article_id=article.id,
        version=1,
        content="内容",
        valid_from=None,
        valid_to=None,
        effect_status="effective",
        source_file_key=None,
        source_url=None,
        content_hash="x" * 64,
    )
    db.add(version)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_article_version_ranges_cannot_overlap(db: Session) -> None:
    """技术方案 4.1 no_overlapping_ranges：同一条文的版本区间不得重叠。"""

    regulation = _make_regulation(db)
    article = _make_article(db, regulation)
    _make_version(db, article, version=1, valid_from=datetime(2019, 3, 20, tzinfo=timezone.utc), valid_to=datetime(2022, 1, 1, tzinfo=timezone.utc))
    db.commit()

    overlapping = RegulationArticleVersion(
        article_id=article.id,
        version=2,
        content="修改后的内容",
        valid_from=datetime(2021, 1, 1, tzinfo=timezone.utc),
        valid_to=None,
        effect_status="effective",
        source_file_key=None,
        source_url=None,
        content_hash="y" * 64,
    )
    db.add(overlapping)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_article_version_ranges_can_be_adjacent(db: Session) -> None:
    """相邻区间（前者失效日 = 后者生效日）合法：左闭右开，边界不算重叠。"""

    regulation = _make_regulation(db)
    article = _make_article(db, regulation)
    _make_version(db, article, version=1, valid_from=datetime(2019, 3, 20, tzinfo=timezone.utc), valid_to=datetime(2022, 1, 1, tzinfo=timezone.utc))
    _make_version(db, article, version=2, valid_from=datetime(2022, 1, 1, tzinfo=timezone.utc), valid_to=None)
    db.commit()

    versions = db.query(RegulationArticleVersion).filter_by(article_id=article.id).count()
    assert versions == 2


def test_partial_repeal_is_expressible(db: Session) -> None:
    """整部法规有效、某条被单独废止必须能表达（ADR-0006）。"""

    regulation = _make_regulation(db)
    article = _make_article(db, regulation)
    _make_version(db, article, valid_to=datetime(2022, 1, 1, tzinfo=timezone.utc), effect_status="effective", version=1)
    _make_version(db, article, valid_from=datetime(2022, 1, 1, tzinfo=timezone.utc), valid_to=datetime(2023, 6, 1, tzinfo=timezone.utc), effect_status="repealed", version=2)
    _make_version(db, article, valid_from=datetime(2023, 6, 1, tzinfo=timezone.utc), valid_to=None, effect_status="effective", version=3)
    db.commit()

    loaded = db.query(RegulationArticleVersion).filter_by(article_id=article.id, effect_status="repealed").one()
    assert loaded.valid_from == datetime(2023, 6, 1, tzinfo=timezone.utc) or loaded.valid_from == datetime(2022, 1, 1, tzinfo=timezone.utc)


def test_relations_can_be_stored(db: Session) -> None:
    """关系边表：based_on / amended_by / repealed_by / references / excepted_by 等。"""

    regulation = _make_regulation(db)
    article = _make_article(db, regulation)
    _make_article(db, regulation, level_code="article", article_no="第二条", order_index=2, full_no="第二条", heading_path="第一章 总则/第二条")
    db.commit()

    db.add(RegulationRelation(
        source_article_id=article.id,
        target_article_id=None,
        target_regulation_id=regulation.id,
        relation_type="references",
        target_ref="财税〔2019〕39号 第一条",
        evidence="本条根据财税〔2019〕39号第一条制定",
        confidence=0.9,
    ))
    db.commit()

    loaded = db.query(RegulationRelation).filter_by(relation_type="references").one()
    assert loaded.target_ref == "财税〔2019〕39号 第一条"


def test_domain_scope_index_exists() -> None:
    """技术方案 9.3：财税两个关键索引必须存在。"""

    table = RegulationArticleVersion.__table__
    index_names = {index.name for index in table.indexes}
    assert "idx_article_version_range" in index_names
    assert "idx_article_effect" in index_names
