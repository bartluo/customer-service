"""专家复核台命令行。

用法（项目根目录，需 docker compose up -d）：
    python scripts/review_desk.py --list
    python scripts/review_desk.py --approve <id> --reviewer 张三 --note "口径无争议"
    python scripts/review_desk.py --reject <id> --reviewer 张三 --note "缺乏商业目的论证"
    python scripts/review_desk.py --workload          # 工作量与按时率
    python scripts/review_desk.py --feedback          # 待转评测集的反馈清单

复核人必须是**审核专家**（权限 knowledge.review 或 planning.review），
脚本会去库里核对，不自称。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


def main() -> int:
    parser = argparse.ArgumentParser(description="筹划方案专家复核台")
    parser.add_argument("--list", action="store_true", help="列出待复核方案")
    parser.add_argument("--approve", metavar="ID", help="放行")
    parser.add_argument("--approve-with-changes", metavar="ID", help="修改后放行")
    parser.add_argument("--reject", metavar="ID", help="驳回（必须填 --note）")
    parser.add_argument("--reviewer", help="复核人用户名，默认取固定管理员")
    parser.add_argument("--note", default="", help="复核意见")
    parser.add_argument("--risk-level", help="修改后放行时，把风险等级改为 green/yellow/red")
    parser.add_argument("--workload", action="store_true", help="工作量统计")
    parser.add_argument("--feedback", action="store_true", help="待转评测集的反馈清单")
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from sqlalchemy import select

    from app.database.session import SessionLocal
    from app.domain.permissions import KNOWLEDGE_REVIEW, PLANNING_REVIEW
    from app.models import User
    from app.planning.review import (
        ACTION_APPROVE,
        ACTION_APPROVE_WITH_CHANGES,
        ACTION_REJECT,
        ReviewDesk,
    )
    from app.services.authorization import load_principal

    session = SessionLocal()
    try:
        statement = (
            select(User).where(User.username == args.reviewer)
            if args.reviewer
            else select(User).where(User.is_protected.is_(True))
        )
        user = session.execute(statement).scalars().first()
        if user is None:
            raise SystemExit("[错误] 找不到复核人账号")
        principal = load_principal(session, user)
        if not (
            principal.has(KNOWLEDGE_REVIEW) or principal.has(PLANNING_REVIEW)
        ):
            raise SystemExit(f"[错误] 账号 {user.username} 没有复核权限，不能执行复核")
        print(f"复核人：{user.username}")

        desk = ReviewDesk(session)

        if args.approve or args.approve_with_changes or args.reject:
            if args.approve:
                review_id, action = args.approve, ACTION_APPROVE
            elif args.approve_with_changes:
                review_id, action = args.approve_with_changes, ACTION_APPROVE_WITH_CHANGES
            else:
                review_id, action = args.reject, ACTION_REJECT
            changes = {}
            if args.risk_level:
                changes["risk_level"] = {"to": args.risk_level}
            if args.note:
                changes["note"] = {"to": args.note}
            row = desk.decide(
                review_id,
                action=action,
                reviewer=user.username,
                note=args.note,
                changes=changes,
            )
            print(f"已处理：{row.status}　复核人：{row.reviewer}")
            return 0

        if args.workload:
            stats = desk.workload()
            print("复核工作量：")
            print(f"  待办：{stats['pending']}　已处理：{stats['decided']}")
            print(f"  平均处理时长：{stats['avg_hours']} 小时（SLA {stats['sla_hours']} 小时）")
            print(f"  按时率：{stats['on_time_rate']}")
            overdue = desk.overdue()
            if overdue:
                print(f"  ⚠ 超时未处理：{len(overdue)} 条（规格要求降级标注，不静默放行）")
            return 0

        if args.feedback:
            items = desk.feedback_for_eval()
            print(f"待转清单：{len(items)} 条")
            for item in items:
                print(f"  · [{item['status']}] {item['question'][:30]}——{item['reason'][:40]}")
                print(f"      用途：{item['suggested_use']}")
            return 0

        items = desk.pending()
        print(f"待复核：{len(items)} 条\n")
        for item in items:
            print(item.explain())
            print("-" * 68)
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
