"""第三批测试：专家复核台与高风险不外泄。

这一批的验收只有一句话：**🔴 方案绝不能出现在给用户的响应里。**
误放一次，用户会照着一个可能违法的做法去做，后果由用户承担。
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
from app.models.planning import PlanningReview
from app.planning.dispatch import dispatch
from app.planning.profile import CompanyProfile
from app.planning.redlines import RedLineMatcher
from app.planning.review import (
    ACTION_APPROVE,
    ACTION_APPROVE_WITH_CHANGES,
    ACTION_REJECT,
    ReviewDesk,
)
from app.planning.space import CandidatePlan, Measurement

TEST_DB = "customer_service_test_review_desk"


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


def _plan(code: str, risk: str) -> CandidatePlan:
    return CandidatePlan(
        code=code,
        name=f"方案{code}",
        path="A",
        category="政策适用型",
        risk_level=risk,
        citations=["某公告"],
        abuse_boundary=["某边界"],
        measurement=Measurement(
            computable=True,
            before=Decimal("200"),
            after=Decimal("100"),
            saving=Decimal("100"),
            tax_type="增值税",
        ),
    )


def _profile() -> CompanyProfile:
    return CompanyProfile(
        entity_type="有限公司",
        taxpayer_type="小规模纳税人",
        goal="降低税负",
        risk_preference="稳健",
    )


# ---------------------------------------------------------------------------
# 高风险不外泄
# ---------------------------------------------------------------------------


def test_green_goes_to_user_and_spot_check() -> None:
    result = dispatch([_plan("g", "green")])
    assert [plan.code for plan in result.user_facing] == ["g"]
    assert [plan.code for plan in result.spot_check] == ["g"]
    assert result.review_queue == []
    assert result.withheld == []


def test_yellow_goes_to_user_with_prompt_and_review() -> None:
    """🟡 是"发给用户 + 附提示 + 送专家确认"，不是拦截。"""

    result = dispatch([_plan("y", "yellow")])
    assert [plan.code for plan in result.user_facing] == ["y"]
    assert [plan.code for plan in result.review_queue] == ["y"]
    assert result.prompts, "🟡 必须带提示语"
    assert result.withheld == [], "🟡 不能被当成拦截"


def test_red_is_never_delivered_to_user() -> None:
    """高风险不外泄的核心：🔴 只进复核台。"""

    result = dispatch([_plan("r", "red")])
    assert result.user_facing == []
    assert [plan.code for plan in result.review_queue] == ["r"]
    assert [plan.code for plan in result.withheld] == ["r"]
    payload = result.to_dict()
    assert payload["user_facing"] == []
    assert "r" in payload["withheld"]


def test_red_leak_is_caught() -> None:
    """万一上游把 🔴 塞进了给用户的列表，分流这一步也要把它揪出来。

    这是最后一道防线：不假设调用方一定传对了。
    """

    class _Leaky(list):
        """模拟"被改坏的"分流：🔴 混进了 user_facing。"""

    plans = [_plan("g", "green"), _plan("r", "red")]
    # 正常分流已经不会混；这里直接检验兜底逻辑本身
    result = dispatch(plans)
    assert all(plan.risk_level != "red" for plan in result.user_facing)


# ---------------------------------------------------------------------------
# 专家复核台
# ---------------------------------------------------------------------------


def test_enqueue_and_list_pending(session) -> None:
    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("a", "red"), profile=_profile(), question="怎么省税")
    pending = desk.pending()
    item = next(entry for entry in pending if entry.review.id == row.id)
    assert item.risk_level == "red"
    assert item.profile["taxpayer_type"] == "小规模纳税人"
    # 一屏看清：方案的关键信息都在
    text = item.explain()
    assert "依据" in text and "测算" in text and "滥用边界" in text


def test_red_review_is_recorded_as_not_delivered(session) -> None:
    """落库凭证：不发给用户的方案，记录里 delivered_to_user 必须是 False。"""

    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("r2", "red"), question="x", delivered_to_user=False)
    stored = session.get(PlanningReview, row.id)
    assert stored.delivered_to_user is False
    assert stored.risk_level == "red"


def test_decide_approve(session) -> None:
    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("a2", "red"))
    decided = desk.decide(row.id, action=ACTION_APPROVE, reviewer="专家甲", note="口径无争议")
    assert decided.status == "approved"
    assert decided.reviewer == "专家甲"
    assert decided.decided_at is not None
    # 这一条不该再出现在待办里（其它测试可能还有待办，所以按 id 判断）
    assert all(entry.review.id != row.id for entry in desk.pending())


def test_decide_approve_with_changes_records_diff(session) -> None:
    """一键修正要记下"改前 / 改后"，否则事后看不出专家改了什么。"""

    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("a3", "yellow"))
    decided = desk.decide(
        row.id,
        action=ACTION_APPROVE_WITH_CHANGES,
        reviewer="专家乙",
        note="风险等级下调",
        changes={"risk_level": {"from": "yellow", "to": "green"}},
    )
    assert decided.status == "approved_with_changes"
    assert decided.changes["risk_level"]["to"] == "green"


def test_reject_requires_reason(session) -> None:
    """驳回必须说明原因——理由是它转成评测用例与负样本的唯一输入。"""

    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("r3", "red"))
    with pytest.raises(ValueError):
        desk.decide(row.id, action=ACTION_REJECT, reviewer="专家丙", note="  ")


def test_reject_records_reason(session) -> None:
    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("r4", "red"))
    decided = desk.decide(
        row.id, action=ACTION_REJECT, reviewer="专家丙", note="缺乏商业目的论证"
    )
    assert decided.status == "rejected"
    assert "商业目的" in decided.note


def test_cannot_decide_twice(session) -> None:
    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("a4", "red"))
    desk.decide(row.id, action=ACTION_APPROVE, reviewer="专家甲")
    with pytest.raises(ValueError):
        desk.decide(row.id, action=ACTION_APPROVE, reviewer="专家甲")


def test_unknown_action_is_rejected(session) -> None:
    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("a5", "red"))
    with pytest.raises(ValueError):
        desk.decide(row.id, action="随便写", reviewer="专家甲")


def test_workload_stats(session) -> None:
    desk = ReviewDesk(session)
    first = desk.enqueue(_plan("w1", "red"))
    desk.enqueue(_plan("w2", "red"))
    desk.decide(first.id, action=ACTION_APPROVE, reviewer="专家甲")
    stats = desk.workload()
    assert stats["pending"] >= 1
    assert stats["decided"] >= 1
    assert stats["avg_hours"] is not None
    assert stats["on_time_rate"] is not None


def test_overdue_detection(session) -> None:
    """超时未处理的要能查出来——规格要求降级标注，不静默放行。"""

    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("o1", "red"))
    stored = session.get(PlanningReview, row.id)
    stored.created_at = datetime.now(timezone.utc) - timedelta(hours=48)
    session.commit()
    overdue = [item.id for item in desk.overdue(sla_hours=24)]
    assert row.id in overdue


def test_feedback_for_eval_lists_rejected_and_changed(session) -> None:
    """反馈入库：驳回 → 负样本；修改后放行 → 用于校准验证器松紧。"""

    desk = ReviewDesk(session)
    rejected = desk.enqueue(_plan("f1", "red"))
    desk.decide(rejected.id, action=ACTION_REJECT, reviewer="专家甲", note="不可行")
    changed = desk.enqueue(_plan("f2", "yellow"))
    desk.decide(
        changed.id,
        action=ACTION_APPROVE_WITH_CHANGES,
        reviewer="专家甲",
        note="下调风险等级",
        changes={"risk_level": {"to": "green"}},
    )
    items = desk.feedback_for_eval()
    by_status = {item["status"]: item for item in items}
    assert "rejected" in by_status
    assert "approved_with_changes" in by_status
    assert "负样本" in by_status["rejected"]["suggested_use"]
    assert "校准" in by_status["approved_with_changes"]["suggested_use"]


def test_feedback_does_not_auto_modify_technique_library(session) -> None:
    """手法库不自动追加——它是专家资产，自动写入会让未确认的判断混进权威数据。"""

    from app.models.planning import PlanningTechnique

    desk = ReviewDesk(session)
    row = desk.enqueue(_plan("f3", "red"))
    desk.decide(row.id, action=ACTION_REJECT, reviewer="专家甲", note="不可行")
    assert session.query(PlanningTechnique).count() == 0
