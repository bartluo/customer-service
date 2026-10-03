"""向量化入库测试（写入侧 + 硬过滤的基础）。

覆盖：
  · 只索引可引用层级（条/款/项），章与节不建向量
  · 只索引已发布法规，待复核的不进索引（财税硬要求）
  · 只索引有效版本区间，废止的不进
  · point ID 稳定，重建索引是覆盖不是重复
  · payload 带时间戳，供范围过滤
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
from app.retrieval.indexer import build_index_entries, index_articles
from app.retrieval.qdrant_client import get_client

TEST_DB_NAME = "customer_service_test"


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


def _make_regulation(db, **overrides) -> Regulation:
    payload = {
        "domain_id": "finance_tax",
        "title": "测试法规",
        "document_number": "财政部 税务总局公告2024年第1号",
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


def _add_version(db, regulation, level="article", no="第一条", content="内容",
                 effect="effective", valid_to=None) -> RegulationArticleVersion:
    article = RegulationArticle(
        regulation_id=regulation.id,
        level_code=level,
        article_no=no,
        full_no=no,
        heading_path=no,
        order_index=1,
    )
    db.add(article)
    db.flush()
    version = RegulationArticleVersion(
        article_id=article.id,
        version=1,
        content=content,
        valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
        valid_to=valid_to,
        effect_status=effect,
    )
    db.add(version)
    db.flush()
    return version


def test_build_entries_includes_published_articles(db) -> None:
    regulation = _make_regulation(db)
    _add_version(db, regulation)
    db.commit()

    entries = build_index_entries(db)
    assert len(entries) == 1
    assert entries[0]["payload"]["effect_status"] == "effective"
    assert entries[0]["payload"]["domain_id"] == "finance_tax"


def test_build_entries_embeds_regulation_title(db) -> None:
    """向量文本必须带上法规标题，否则"按文件问"整类问题都匹配不到。

    回归背景：问"全面数字化电子发票怎么开"，正确答案是
    《国家税务总局关于推广应用全面数字化电子发票的公告》，
    但它的条文正文里没有"全面数字化"这几个字。
    只把正文放进向量，这个问题永远匹配不到那份公告。
    """

    regulation = _make_regulation(db, title="国家税务总局关于推广应用全面数字化电子发票的公告")
    _add_version(db, regulation, content="现将有关事项公告如下。")
    db.commit()

    entries = build_index_entries(db)
    assert len(entries) == 1
    dense_text = entries[0]["dense_text"]
    assert "全面数字化电子发票" in dense_text
    assert "现将有关事项公告如下。" in dense_text


def test_build_entries_excludes_pending_review(db) -> None:
    """待复核法规不进索引——财税硬要求，未审内容不能被引用。"""

    regulation = _make_regulation(db, review_state="pending_review")
    _add_version(db, regulation)
    db.commit()

    assert build_index_entries(db) == []


def test_build_entries_excludes_chapter_and_section(db) -> None:
    """章与节是目录结构，不建向量。"""

    regulation = _make_regulation(db)
    _add_version(db, regulation, level="chapter", no="第一章")
    _add_version(db, regulation, level="section", no="第一节")
    _add_version(db, regulation, level="article", no="第一条")
    db.commit()

    entries = build_index_entries(db)
    assert len(entries) == 1
    assert entries[0]["payload"]["level_code"] == "article"


def test_build_entries_excludes_repealed(db) -> None:
    """废止条文不进索引——这是财税最关键的一条。"""

    regulation = _make_regulation(db)
    _add_version(db, regulation, effect="repealed")
    db.commit()

    assert build_index_entries(db) == []


def test_build_entries_payload_has_timestamps(db) -> None:
    """payload 必须带 Unix 秒时间戳，Qdrant 范围过滤只认数值。"""

    regulation = _make_regulation(db)
    _add_version(db, regulation, valid_to=datetime(2027, 1, 1, tzinfo=timezone.utc))
    db.commit()

    payload = build_index_entries(db)[0]["payload"]
    assert isinstance(payload["valid_from_ts"], int)
    assert isinstance(payload["valid_to_ts"], int)
    assert payload["valid_from_ts"] < payload["valid_to_ts"]


def test_point_id_is_stable(db) -> None:
    """同一版本 ID 派生同样的 point ID，重建索引是覆盖。"""

    regulation = _make_regulation(db)
    version = _add_version(db, regulation)
    db.commit()

    first = build_index_entries(db)[0]["point_id"]
    second = build_index_entries(db)[0]["point_id"]
    assert first == second
    assert version.id  # point ID 由版本 ID 派生


def test_index_writes_to_qdrant(db, qdrant) -> None:
    """端到端：数据库 → 向量库，能写进去也能读回来。"""

    from qdrant_client.models import Filter

    qdrant.delete(collection_name=KB_ARTICLES, points_selector=Filter(), wait=True)

    regulation = _make_regulation(db)
    _add_version(db, regulation, content="增值税一般纳税人适用13%税率的销售货物。")
    db.commit()

    report = index_articles(db, client=qdrant)
    assert report["total"] == 1
    assert report["indexed"] == 1
    assert report["failed_batches"] == 0

    count = qdrant.count(collection_name=KB_ARTICLES, exact=True).count
    assert count == 1


def test_index_is_idempotent(db, qdrant) -> None:
    """重复索引不产生重复点。"""

    from qdrant_client.models import Filter

    qdrant.delete(collection_name=KB_ARTICLES, points_selector=Filter(), wait=True)

    regulation = _make_regulation(db)
    _add_version(db, regulation)
    db.commit()

    index_articles(db, client=qdrant)
    index_articles(db, client=qdrant)

    assert qdrant.count(collection_name=KB_ARTICLES, exact=True).count == 1


def test_index_removes_stale_points(db, qdrant) -> None:
    """法规被驳回后重新索引，旧点必须消失。

    这是财税场景的硬要求：紧急下架要≤ 10 秒生效（技术方案 5.4）。
    """

    from qdrant_client.models import Filter

    qdrant.delete(collection_name=KB_ARTICLES, points_selector=Filter(), wait=True)

    regulation = _make_regulation(db)
    _add_version(db, regulation)
    db.commit()
    index_articles(db, client=qdrant)
    assert qdrant.count(collection_name=KB_ARTICLES, exact=True).count == 1

    # 改成待复核，重新索引后应清空
    regulation.review_state = "pending_review"
    db.commit()
    index_articles(db, client=qdrant)
    assert qdrant.count(collection_name=KB_ARTICLES, exact=True).count == 0
