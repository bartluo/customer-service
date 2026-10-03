"""批量导入流水线测试（批量导入 + 进度 + 失败清单）。

对应任务清单：一次导入上百份文件，能看到进度与失败清单。

为什么用真实数据库：导入要落 regulations / regulation_articles /
regulation_article_versions 三张表，涉及外键与唯一约束，内存库测不出真问题。
"""

from __future__ import annotations
import os

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database.base import Base
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
from app.services.ingest.importer import (
    DocumentInput,
    ImportReport,
    import_documents,
)

TEST_DB_NAME = "customer_service_test"


def _test_database_url() -> str:
    base = os.environ["DATABASE_URL"]
    return base.rsplit("/", 1)[0] + "/" + TEST_DB_NAME


@pytest.fixture()
def db():
    server_url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
    server = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :name"),
            {"name": TEST_DB_NAME},
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


GOOD_DOC = """财政部 税务总局公告2020年第23号

关于延续实施应对疫情部分税费优惠政策的公告

第一条 为支持新冠肺炎疫情纾困，本次出台的增值税小规模纳税人优惠政策适用至2021年12月31日。

第二条 增值税小规模纳税人适用3%征收率的应税销售收入，减按1%征收率征收增值税。
"""


def _good(name: str) -> DocumentInput:
    return DocumentInput(
        filename=name,
        text=GOOD_DOC,
        source_url=f"https://example.gov.cn/{name}",
    )


def test_import_single_document_creates_full_structure(db) -> None:
    """一份法规导入后：主表 + 条文表 + 版本表都要有数据。"""

    report = import_documents(db, [_good("a.txt")])

    assert report.success_count == 1
    assert report.failed_count == 0

    regulation = db.query(Regulation).one()
    assert regulation.document_number == "财政部 税务总局公告2020年第23号"
    assert regulation.effective_date is not None or regulation.review_state == "pending_review"

    articles = db.query(RegulationArticle).all()
    assert len(articles) >= 2
    versions = db.query(RegulationArticleVersion).all()
    assert len(versions) >= 2
    # 每个版本都必须带生效日（ontology time_range_required）
    assert all(v.valid_from is not None for v in versions)


def test_import_is_idempotent(db) -> None:
    """重复导入同一份法规不产生重复数据。"""

    import_documents(db, [_good("a.txt")])
    second = import_documents(db, [_good("a.txt")])

    assert db.query(Regulation).count() == 1
    assert second.success_count + second.skipped_count == 1


# 没有文号的文件：官方库里法律、行政法规就是这样，页面排版偶尔也会把文号拆行
NO_NUMBER_DOC = """某部门关于某事项的公告

一、本公告自发布之日起执行。
二、本公告由某部门负责解释。
"""


def test_import_without_document_number_is_still_idempotent(db) -> None:
    """没有文号的文件重复导入也不能产生重复记录。

    回归背景：旧实现"文号为空就认为库里没有"，于是同一份文件每导入一次
    就多一条记录，检索结果里同一条法规重复出现。
    """

    document = DocumentInput(
        filename="no-number.txt",
        text=NO_NUMBER_DOC,
        source_url="https://example.gov.cn/no-number.html",
    )
    first = import_documents(db, [document])
    second = import_documents(db, [document])

    assert first.success_count == 1
    assert second.skipped_count == 1
    assert db.query(Regulation).count() == 1


def test_import_respects_official_effect_status(db) -> None:
    """官方标"全文废止"的法规，入库时就不能是"现行有效"。

    回归背景：导入器把 effect_status 写死成 effective，
    于是 45 份已废止法规照样进索引、照样被引用。
    主表和条文版本表两处都要写对——检索过滤看的是版本表。
    """

    document = DocumentInput(
        filename="repealed.txt",
        text=GOOD_DOC,
        source_url="https://example.gov.cn/repealed.html",
        effect_status="repealed",
    )
    import_documents(db, [document])

    regulation = db.query(Regulation).one()
    assert regulation.effect_status == "repealed"

    versions = db.query(RegulationArticleVersion).all()
    assert versions
    assert all(version.effect_status == "repealed" for version in versions)


def test_repealed_regulation_is_not_indexed(db) -> None:
    """已废止法规的条文不能进向量索引（检索只认可引用状态）。"""

    from app.retrieval.indexer import build_index_entries

    document = DocumentInput(
        filename="repealed.txt",
        text=GOOD_DOC,
        source_url="https://example.gov.cn/repealed.html",
        effect_status="repealed",
    )
    import_documents(db, [document])

    assert build_index_entries(db) == []


def test_import_reports_progress(db) -> None:
    """批量导入要能看到进度。"""

    report = import_documents(db, [_good(f"doc{i}.txt") for i in range(5)])

    assert report.total == 5
    assert len(report.progress_trace) == 5
    assert report.progress_trace[-1] == 5
    assert report.progress_percent == 100.0


def test_import_collects_failure_reasons(db) -> None:
    """失败要有清单和原因，不能静默吞掉。"""

    bad = DocumentInput(filename="bad.txt", text="", source_url="https://example.gov.cn/bad")
    report = import_documents(db, [_good("ok.txt"), bad])

    assert report.success_count == 1
    assert report.failed_count == 1
    assert report.failures[0]["filename"] == "bad.txt"
    assert report.failures[0]["reason"]


def test_import_does_not_block_batch_on_review_items(db) -> None:
    """需要人工复核的文件照常入库并标 pending_review，但不能拖垮整批。"""

    no_source = DocumentInput(filename="nosource.txt", text=GOOD_DOC, source_url=None)
    report = import_documents(db, [_good("ok.txt"), no_source])

    assert report.failed_count == 0
    assert report.review_count == 1
    pending = db.query(Regulation).filter_by(review_state="pending_review").all()
    assert len(pending) == 1


def test_import_partial_failure_does_not_rollback_earlier_rows(db) -> None:
    """一份文件失败，前后的成功都不受影响。

    注意 a.txt 与 a-copy.txt 内容相同（文号也相同），按幂等设计后者会被跳过，
    所以这里是 1 成功 + 1 跳过 + 1 失败，重点是失败不影响其他两份。
    """

    docs = [
        _good("a.txt"),
        DocumentInput(filename="x.txt", text="", source_url="https://e.com/x"),
        _good("a-copy.txt"),
    ]
    report = import_documents(db, docs)

    assert report.success_count == 1
    assert report.skipped_count == 1
    assert report.failed_count == 1
    assert db.query(Regulation).count() == 1


def test_report_has_summary_fields(db) -> None:
    """报告字段完整，便于脚本与人工查看。"""

    report = import_documents(db, [_good("a.txt")])
    assert isinstance(report, ImportReport)
    assert report.total == 1
    assert 0 <= report.progress_percent <= 100.0
    assert report.to_dict()["success"] == 1
