"""评测与门禁测试（含单份索引重建）。

这一层的验收不是"功能能跑"，而是**红线拦得住、且不误报**：
  · 一票否决不能被平均分稀释（做成加权打分就等于没有红线）
  · 废止陷阱的判据必须是身份比对（子串匹配会把有效文件误报成废止）
  · 失败题必须记 0 分（否则 Recall@5 会算出 1.0 而实际全挂）
  · 单份法规索引重建要能"写完再撤"，撤不干净等于废止条文还查得到
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
from app.evaluation.gate import (
    CANARY_STEPS,
    GATE_CANARY,
    GATE_SHADOW,
    GateKeeper,
)
from app.evaluation.runner import CaseResult, EvalRunner, EvalRunReport
from app.models.eval import CASE_INSUFFICIENT, CASE_REPEALED_TRAP, CASE_STANDARD
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
from app.retrieval.collections import KB_ARTICLES, ensure_collections
from app.retrieval.indexer import (
    build_regulation_entries,
    reindex_regulation,
    remove_regulation_points,
)
from app.retrieval.qdrant_client import get_client
from app.retrieval.searcher import Citation

TEST_DB = "customer_service_test_evaluation"


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


@pytest.fixture(scope="module")
def qdrant():
    client = get_client()
    try:
        client.get_collections()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Qdrant 不可用：{exc}")
    ensure_collections(client)
    return client


# ---------------------------------------------------------------- 构造工具


def _report(
    *,
    total: int = 100,
    passed: int = 97,
    failures: list[CaseResult] | None = None,
    recall: float | None = 0.93,
    trap_leaks: int = 0,
    hard_answers: int = 0,
) -> EvalRunReport:
    report = EvalRunReport(total=total, passed=passed, failed=total - passed)
    report.failures = list(failures or [])
    report.results = list(report.failures)
    report.metrics = {
        "用例总数": total,
        "通过数": passed,
        "通过率": passed / total,
        "Recall@5": recall,
        "MRR@5": 0.8,
        "废止陷阱泄漏数": trap_leaks,
        "信息不足硬答数": hard_answers,
    }
    return report


def _citation(
    *,
    document_number: str | None = None,
    regulation_title: str = "某法规",
    full_no: str = "第一条",
    effect_status: str = "effective",
    content: str = "内容",
) -> Citation:
    return Citation(
        document_number=document_number,
        full_no=full_no,
        regulation_title=regulation_title,
        issuer="某机关",
        effect_status=effect_status,
        level_code="article",
        hierarchy_level="normative_document",
        content=content,
        source_url=None,
        score=1.0,
    )


def _regulation(db, title: str, *, status: str = "effective", review: str = "published") -> Regulation:
    regulation = Regulation(
        domain_id="finance_tax",
        title=title,
        document_number=f"财政部 税务总局公告2024年第{abs(hash(title)) % 100}号",
        issuer="财政部 税务总局",
        hierarchy_level="normative_document",
        region_scope="national",
        tax_types=["增值税"],
        applies_to=[],
        source_url="https://fgk.chinatax.gov.cn/test",
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        version=1,
        effect_status=status,
        review_state=review,
        is_draft=False,
    )
    db.add(regulation)
    db.flush()
    return regulation


def _article(db, regulation, *, no: str = "第一条", content: str = "内容", status: str) -> RegulationArticleVersion:
    article = RegulationArticle(
        regulation_id=regulation.id,
        level_code="article",
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
        valid_to=None,
        effect_status=status,
    )
    db.add(version)
    db.flush()
    return version


# ---------------------------------------------------------------- 门禁：一票否决


def test_veto_blocks_even_when_average_is_high(session) -> None:
    """一票否决不能被平均分稀释——这正是"加权打分"做法最危险的地方。"""

    keeper = GateKeeper(session)
    report = _report(total=100, passed=99, trap_leaks=1)
    decision = keeper.evaluate(report, gate="offline", release_ref="t9-veto")

    assert decision.decision == "blocked"
    assert not decision.released
    assert any("已废止" in item for item in decision.veto_items)


def test_clean_run_releases(session) -> None:
    """没有红线泄漏且指标达标 → 放行。"""

    keeper = GateKeeper(session)
    decision = keeper.evaluate(_report(), gate="offline", release_ref="t9-clean")

    assert decision.released
    assert decision.veto_items == []
    assert decision.reasons == []


def test_quality_shortfall_is_not_a_red_line(session) -> None:
    """指标不达标要阻断，但**不能记进一票否决**——两类问题的处置完全不同。

    混在一起会让"到底能不能发"变成一句含糊的话；
    分开记，人一眼能看出是"红线破了"还是"质量不够"。
    """

    keeper = GateKeeper(session)
    decision = keeper.evaluate(_report(recall=0.5), gate="offline", release_ref="t9-quality")

    assert decision.decision == "blocked"
    assert decision.veto_items == []
    assert any("Recall@5" in reason for reason in decision.reasons)


def test_failed_cases_carry_detail_and_red_lines_first(session) -> None:
    """阻断必须给出失败明细，且红线类排在前面，人先看最要命的。"""

    keeper = GateKeeper(session)
    failures = [
        CaseResult("c1", "通用问题", CASE_STANDARD, False, "Top5 未命中"),
        CaseResult("c2", "陷阱问题", CASE_REPEALED_TRAP, False, "出现已废止引用"),
    ]
    decision = keeper.evaluate(_report(trap_leaks=1, failures=failures), release_ref="t9-detail")

    assert [item["case_type"] for item in decision.failed_cases] == [
        CASE_REPEALED_TRAP,
        CASE_STANDARD,
    ]
    assert decision.failed_cases[0]["detail"] == "出现已废止引用"


def test_canary_ladder_only_goes_up_one_step(session) -> None:
    """灰度只能逐级放量：5% → 20% → 50% → 100%。跳级放量等于没有灰度。"""

    keeper = GateKeeper(session)
    assert keeper.canary_stage(GATE_CANARY, 0.01) == 0.05
    assert keeper.canary_stage(GATE_CANARY, 0.05) == 0.05
    assert keeper.canary_stage(GATE_CANARY, 0.15) == 0.20
    assert keeper.canary_stage(GATE_CANARY, 0.45) == 0.50
    assert keeper.canary_stage(GATE_CANARY, 0.99) == 1.00
    assert list(CANARY_STEPS) == [0.05, 0.20, 0.50, 1.00]

    # 非灰度闸不做阶梯限制
    assert keeper.canary_stage(GATE_SHADOW, 0.33) == 0.33


def test_rollback_leaves_a_trail(session) -> None:
    """回滚必须留痕：没有留痕，事后查不出当时为什么回滚。"""

    keeper = GateKeeper(session)
    keeper.evaluate(_report(), release_ref="t9-rollback")

    # 回滚要拿到已落库的门禁记录（evaluate 返回的是判定结论，
    # 记录本身用 latest() 取——与验收脚本同一条路径）
    decision = keeper.latest(gate="offline")
    assert decision is not None

    rolled = keeper.rollback(decision.id, reason="演练：发现效力错误")
    assert rolled.rolled_back is True
    assert rolled.decision == "rolled_back"
    assert any("演练：发现效力错误" in reason for reason in rolled.reasons)

    with pytest.raises(LookupError):
        keeper.rollback("00000000-0000-0000-0000-000000000000", reason="不存在的记录")


# ---------------------------------------------------------------- 评测判据


def test_leak_detection_uses_identity_not_substring() -> None:
    """废止陷阱必须是身份比对，不能拿标题做子串匹配。

    库里有《国务院关于废止〈营业税暂行条例〉和修改〈增值税暂行条例〉的决定》——
    这份文件本身仍然有效，但标题里含"增值税暂行条例"。
    用子串匹配会把它误报成"引用了废止条款"；门禁老报假警，人就不看它了。
    """

    valid_decision = _citation(
        regulation_title="国务院关于废止《营业税暂行条例》和修改《增值税暂行条例》的决定",
        full_no="",
    )
    assert EvalRunner._find_leaks([valid_decision], ["增值税暂行条例"]) == []

    repealed = _citation(document_number="财政部 税务总局公告2019年第1号")
    assert EvalRunner._find_leaks([repealed], ["财政部 税务总局公告2019年第1号"]) == [
        "财政部 税务总局公告2019年第1号 第一条"
    ]

    # 空禁止项不该误伤任何引用
    assert EvalRunner._find_leaks([repealed], []) == []


def test_metrics_count_failures_not_just_passes() -> None:
    """失败题必须记 0 分。失败题若不带指标，Recall@5 算出 1.0，而 30 条挂了 29 条。"""

    failures = [
        CaseResult(
            "c1", "标准题", CASE_STANDARD, False, "Top5 未命中", metrics={"recall": 0, "rr": 0.0}
        )
    ]
    report = _report(total=1, passed=0, failures=failures, recall=None, trap_leaks=0)
    metrics = EvalRunner._metrics(report, [0], [0.0])

    assert metrics["Recall@5"] == 0.0
    assert metrics["MRR@5"] == 0.0
    assert metrics["通过率"] == 0.0


def test_metrics_break_down_red_lines_by_type() -> None:
    """废止泄漏与信息不足硬答要分开计数——它们是两条不同的红线。"""

    failures = [
        CaseResult("c1", "陷阱", CASE_REPEALED_TRAP, False, "出现已废止引用"),
        CaseResult("c2", "追问", CASE_INSUFFICIENT, False, "未追问"),
        CaseResult("c3", "又一陷阱", CASE_REPEALED_TRAP, False, "出现已废止引用"),
    ]
    report = _report(total=3, passed=0, failures=failures, recall=None)
    metrics = EvalRunner._metrics(report, [], [])

    assert metrics["废止陷阱泄漏数"] == 2
    assert metrics["信息不足硬答数"] == 1


# ---------------------------------------------------------------- 单份索引重建


def test_build_regulation_entries_is_scoped(session) -> None:
    """单份构建只取目标法规，不串到别的法规。"""

    target = _regulation(session, "目标法规")
    other = _regulation(session, "无关法规")
    _article(session, target, content="目标条文", status="effective")
    _article(session, other, content="无关条文", status="effective")
    session.commit()

    entries = build_regulation_entries(session, target.id)
    assert len(entries) == 1
    assert entries[0]["payload"]["regulation_id"] == target.id


def test_build_regulation_entries_excludes_non_citable(session) -> None:
    """已废止法规的单份构建结果为空——它不该出现在索引里。"""

    repealed = _regulation(session, "已废止法规", status="repealed")
    _article(session, repealed, content="废止条文", status="repealed")
    session.commit()

    assert build_regulation_entries(session, repealed.id) == []


def test_reindex_then_revoke_removes_points(session, qdrant) -> None:
    """端到端：发布后重建能查到，改回废止再重建必须查不到。

    回归背景：验收要"故意导入一条错误知识看门禁能不能拦住"，
    可已废止法规的索引点在时效治理时就被清掉了，只改状态补不回来；
    还原时如果只改状态不删点，验收脚本自己就制造了一起事故。
    """

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    regulation = _regulation(session, "索引重建测试法规")
    version = _article(session, regulation, content="先按现行有效写入的条文", status="effective")
    session.commit()

    selector = Filter(
        must=[FieldCondition(key="regulation_id", match=MatchValue(value=regulation.id))]
    )

    def _count() -> int:
        return qdrant.count(collection_name=KB_ARTICLES, count_filter=selector, exact=True).count

    remove_regulation_points(regulation.id, client=qdrant)
    assert _count() == 0

    outcome = reindex_regulation(session, regulation.id, client=qdrant)
    assert outcome["indexed"] == 1
    assert _count() == 1

    # 重复重建是覆盖，不是重复点
    reindex_regulation(session, regulation.id, client=qdrant)
    assert _count() == 1

    # 改回废止后再重建：不该留下任何点
    version.effect_status = "repealed"
    regulation.effect_status = "repealed"
    session.commit()
    reindex_regulation(session, regulation.id, client=qdrant)
    assert _count() == 0


def test_reindex_removes_stale_points_when_article_count_drops(session, qdrant) -> None:
    """条文变少时旧点必须清掉——只 upsert 会把已经不该存在的点留在索引里。"""

    regulation = _regulation(session, "条文收缩测试法规")
    first = _article(session, regulation, no="第一条", content="第一条内容", status="effective")
    _article(session, regulation, no="第二条", content="第二条内容", status="effective")
    session.commit()

    reindex_regulation(session, regulation.id, client=qdrant)
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    selector = Filter(
        must=[FieldCondition(key="regulation_id", match=MatchValue(value=regulation.id))]
    )
    assert qdrant.count(collection_name=KB_ARTICLES, count_filter=selector, exact=True).count == 2

    first.effect_status = "repealed"
    session.commit()
    reindex_regulation(session, regulation.id, client=qdrant)
    assert qdrant.count(collection_name=KB_ARTICLES, count_filter=selector, exact=True).count == 1

    remove_regulation_points(regulation.id, client=qdrant)
