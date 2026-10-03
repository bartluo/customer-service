"""自进化测试。

这一层的验收不是"功能能跑"，而是**发现得准、判断得对**：
把"新增场景"误判成"废止"会让正确知识被撤下；
把"废止"误判成"新增"会让废止条文继续被引用。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database.base import Base
from app.evolution.gaps import GapFinder, _topic_of, record_gap
from app.evolution.health import HealthScorer
from app.evolution.impact import analyze_impact
from app.evolution.monitor import DiscoveredDocument, OfficialMonitor
from app.evolution.notify import build_change_digest
from app.models.evolution import EvolutionEvent, KnowledgeGap
from app.models.knowledge import Regulation

TEST_DB = "customer_service_test_evolution"


@pytest.fixture(scope="module")
def factory():
    server_url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
    server = create_engine(server_url, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        exists = conn.execute(
            sa.text("select 1 from pg_database where datname = :n"), {"n": TEST_DB}
        ).scalar()
        if not exists:
            conn.execute(sa.text(f'create database "{TEST_DB}"'))
    server.dispose()

    url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/" + TEST_DB
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("create extension if not exists btree_gist"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture()
def session(factory):
    db = factory()
    try:
        yield db
    finally:
        db.close()


def _regulation(title: str, document_number: str | None = "财税〔2020〕1号", **overrides) -> Regulation:
    base = dict(
        domain_id="finance_tax",
        title=title,
        document_number=document_number,
        hierarchy_level="normative_document",
        region_scope="national",
        tax_types=["增值税"],
        applies_to=[],
        source_url="https://fgk.chinatax.gov.cn/x",
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        version=1,
        effect_status="effective",
        review_state="published",
        is_draft=False,
    )
    base.update(overrides)
    return Regulation(**base)


# ---------------------------------------------------------------------------
# 影响分析
# ---------------------------------------------------------------------------


def test_detects_repeal(session) -> None:
    session.add(_regulation("中华人民共和国某税暂行条例"))
    session.commit()
    analysis = analyze_impact(
        session,
        title="关于废止《中华人民共和国某税暂行条例》的决定",
        content="经研究决定，废止《中华人民共和国某税暂行条例》。本决定自发布之日起施行。",
    )
    assert analysis.kind == "repealed"
    assert analysis.needs_review is True
    assert len(analysis.impacted) == 1
    assert analysis.impacted[0].relation == "repealed"


def test_detects_amendment(session) -> None:
    session.add(_regulation("某发票管理办法"))
    session.commit()
    analysis = analyze_impact(
        session,
        title="关于修改《某发票管理办法》的决定",
        content="决定对《某发票管理办法》作如下修改：一、将第二条修改为……",
    )
    assert analysis.kind == "amended"
    assert analysis.impacted[0].relation == "amended"


def test_interpretation_is_archived_not_used(session) -> None:
    """解读性文章要归档，不能当依据——把解读当法条是财税问答的典型事故。"""

    analysis = analyze_impact(
        session, title="关于小微政策的解读", content="本文就小微政策作几点解读。"
    )
    assert analysis.kind == "interpretation"
    assert analysis.needs_review is False


def test_new_scenario_when_no_named_regulation(session) -> None:
    analysis = analyze_impact(
        session, title="关于某新情形适用政策的公告", content="现将有关事项公告如下：一、……"
    )
    assert analysis.kind == "new_scenario"
    assert analysis.impacted == []


def test_repeal_of_unknown_regulation_is_reported(session) -> None:
    """点名废止了库里没有的法规时，要说明"不在库内"，而不是悄悄跳过。"""

    analysis = analyze_impact(
        session,
        title="关于废止某规定的决定",
        content="经研究决定，废止《某个知识库里没有的文件》。",
    )
    assert analysis.kind == "repealed"
    assert analysis.impacted == []
    assert any("不在知识库内" in reason for reason in analysis.reasons)


# ---------------------------------------------------------------------------
# 缺口发现
# ---------------------------------------------------------------------------


def test_topic_clustering_uses_tax_type_and_keyword() -> None:
    topic, tax_type = _topic_of("小规模纳税人进项税额能不能抵扣")
    assert tax_type == "增值税"
    assert "增值税" in topic
    assert "抵扣" in topic


def test_topic_falls_back_when_unclassifiable() -> None:
    topic, tax_type = _topic_of("今天天气怎么样")
    assert topic == "未分类问题"
    assert tax_type is None


def test_record_gap_accumulates_same_topic(session) -> None:
    """同一主题累加次数与样本，不产生重复行。"""

    record_gap(session, question="合伙企业怎么交所得税", reason="no_hit")
    record_gap(session, question="合伙企业怎么交所得税（再问一次）", reason="no_hit")
    rows = session.query(KnowledgeGap).all()
    assert len(rows) == 1
    assert rows[0].occurrences == 2
    assert len(rows[0].sample_questions) == 2


def test_gap_finder_reads_verification_records(session) -> None:
    from app.models.verification import VerificationRecord

    for question in ("合伙企业所得怎么算", "合伙企业税务处理"):
        session.add(
            VerificationRecord(
                question=question, outcome="degraded", passed=False, retries=0, detail={},
                issue_codes=["no_basis"],
            )
        )
    session.commit()
    summaries = GapFinder(session).from_verification_records()
    assert summaries
    assert all(item.topic and item.samples for item in summaries)


def test_gap_finder_persist_is_idempotent(session) -> None:
    finder = GapFinder(session)
    from app.evolution.gaps import GapSummary

    summaries = [
        GapSummary(topic="增值税·留抵", tax_type="增值税", reason="degraded", occurrences=3, samples=["a"])
    ]
    finder.persist(summaries)
    finder.persist(summaries)
    assert session.query(KnowledgeGap).filter_by(topic="增值税·留抵").count() == 1


# ---------------------------------------------------------------------------
# 衰退淘汰
# ---------------------------------------------------------------------------


def test_health_penalizes_repealed(session) -> None:
    session.add(_regulation("已废止的某条例", effect_status="repealed"))
    session.commit()
    items = HealthScorer(session).score_all()
    target = next(item for item in items if item.title == "已废止的某条例")
    assert target.score <= 40  # 100 - 60
    assert any("已不可引用" in signal for signal in target.signals)


def test_health_penalizes_missing_source_and_empty(session) -> None:
    session.add(_regulation("缺来源且无条文的规定", document_number="财税〔2020〕2号", source_url=""))
    session.commit()
    items = HealthScorer(session).score_all()
    target = next(item for item in items if item.title == "缺来源且无条文的规定")
    assert target.score <= 75
    joined = " ".join(target.signals)
    assert "来源" in joined
    assert "条文" in joined


def test_healthy_regulation_scores_high(session) -> None:
    from app.models.knowledge import RegulationArticle, RegulationArticleVersion

    regulation = _regulation("健康的规定", document_number="财税〔2020〕3号")
    session.add(regulation)
    session.flush()
    article = RegulationArticle(
        regulation_id=regulation.id, level_code="article", article_no="第一条",
        full_no="第一条", heading_path="", order_index=0,
    )
    session.add(article)
    session.flush()
    session.add(
        RegulationArticleVersion(
            article_id=article.id, version=1, content="正文",
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            effect_status="effective",
        )
    )
    session.commit()
    items = HealthScorer(session).score_all()
    target = next(item for item in items if item.title == "健康的规定")
    assert target.score >= 70


def test_review_queue_uses_threshold(session) -> None:
    session.add(_regulation("又一条废止的", document_number="财税〔2020〕4号", effect_status="superseded"))
    session.commit()
    queue = HealthScorer(session).review_queue()
    assert any(item.title == "又一条废止的" for item in queue)


# ---------------------------------------------------------------------------
# 监控与摘要
# ---------------------------------------------------------------------------


def test_monitor_normalizes_urls(session) -> None:
    """官方域名有两种写法，判重前必须归一化，否则同一份文件会被反复当成新发现。"""

    session.add(_regulation("某公告", source_url="http://www.chinatax.gov.cn/zcfgk/c1/content.html"))
    session.commit()
    monitor = OfficialMonitor(session)
    assert "https://fgk.chinatax.gov.cn/zcfgk/c1/content.html" in monitor.known_urls


def test_monitor_records_event_with_discovery_time(session) -> None:
    monitor = OfficialMonitor(session)
    event = monitor.record(
        DiscoveredDocument(title="某新公告", url="https://example.test/a"), event_type="new_regulation"
    )
    assert event.discovered_at is not None
    assert event.status == "discovered"


def test_digest_orders_by_importance(session) -> None:
    """摘要排序按"对用户有没有实质影响"：废止 > 修订 > 新增 > 解读。"""

    session.add_all(
        [
            EvolutionEvent(event_type="interpretation", title="解读文章"),
            EvolutionEvent(event_type="repealed", title="废止决定"),
            EvolutionEvent(event_type="new_regulation", title="新公告"),
        ]
    )
    session.commit()
    digest = build_change_digest(session)
    assert digest.count >= 3
    assert digest.items[0]["title"] == "废止决定"


def test_digest_states_delivery_honestly(session) -> None:
    """没有推送通道就直说——假装已推送比不推送更糟，用户会以为订阅生效了。"""

    digest = build_change_digest(session)
    assert "未配置" in digest.delivery
    assert "未配置推送通道" in digest.text()
