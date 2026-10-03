"""知识复核：待复核清单与放行/驳回（落地动作）。

为什么单独成一个服务模块：
  · 复核是"审核专家"的专业动作，不是普通读写。放在服务层，
    接口（复核台）与脚本（批量处理）走同一套规则，不存在两套判断。
  · 检索只读 published（见 retrieval/indexer.py），所以"放行"这个动作
    直接决定一条知识能不能出现在答案里——这是权限最敏感的地方。

复核结论只有三种状态（见 ontology.REVIEW_STATES）：
  pending_review 待复核 / published 已放行 / rejected 已驳回
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge import Regulation
from app.services import audit

REVIEW_PENDING = "pending_review"
REVIEW_PUBLISHED = "published"
REVIEW_REJECTED = "rejected"

ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"
ACTIONS = (ACTION_APPROVE, ACTION_REJECT)


def pending_reasons(regulation: Regulation) -> list[str]:
    """列出这条法规"为什么在待复核队列里"。

    判断口径与解析器（app/knowledge/parser.py）保持一致：解析器当初标了什么原因，
    这里就要能看出来，否则审核专家打开复核台只看到一个标题，不知道该看哪里。

    数据库里没有存"原因"字段，是有意的：原因是可以由记录本身推导出来的，
    存一份副本反而会出现"记录改了、原因没跟着改"的不一致。
    """

    reasons: list[str] = []
    if not regulation.document_number:
        reasons.append("文号未识别，需人工确认（系统不编造文号）")
    if not regulation.hierarchy_level:
        reasons.append("效力位阶未判定，需人工确认")
    if not regulation.effective_date:
        reasons.append("生效日期缺失，需人工确认")
    if not regulation.source_url:
        reasons.append("缺少来源 URL 与获取时间，按合规要求不予收录")
    return reasons


def list_pending(
    session: Session,
    *,
    domain_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Regulation]:
    """待复核清单，按标题排序，便于人工成批处理同税种。"""

    statement = select(Regulation).where(Regulation.review_state == REVIEW_PENDING)
    if domain_id:
        statement = statement.where(Regulation.domain_id == domain_id)
    statement = statement.order_by(Regulation.title).offset(offset).limit(limit)
    return list(session.execute(statement).scalars().all())


def apply_decision(
    session: Session,
    regulation: Regulation,
    *,
    action: str,
    note: str | None,
    actor_id: str | None,
    actor_username: str,
) -> Regulation:
    """放行或驳回一条法规，并写审计日志。**不提交事务**，由调用方决定提交时机。

    为什么不在服务里 commit：批量复核要能把一整批包在一个事务里，
    中途某一条出问题不会被写成"放行了一半"。单个调用的场景，调用方 commit 一次即可。
    """

    if action not in ACTIONS:
        raise ValueError(f"未知复核动作：{action}（可选：{' / '.join(ACTIONS)}）")

    target_state = REVIEW_PUBLISHED if action == ACTION_APPROVE else REVIEW_REJECTED
    previous_state = regulation.review_state
    regulation.review_state = target_state

    audit.record(
        session,
        action="knowledge.review_decision",
        actor_id=actor_id,
        actor_username=actor_username,
        target_type="regulation",
        target_id=regulation.id,
        detail={
            "title": regulation.title,
            "document_number": regulation.document_number,
            "from": previous_state,
            "to": target_state,
            "note": note or "",
        },
    )
    return regulation
