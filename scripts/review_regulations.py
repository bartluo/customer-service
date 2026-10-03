"""知识复核脚本：查看待复核清单、放行或驳回法规。

用法（项目根目录）：
    python scripts/review_regulations.py --list
    python scripts/review_regulations.py --approve-all --note "开发期批量放行，来源为税务总局政策法规库"
    python scripts/review_regulations.py --approve <法规ID> --note "已人工核对"
    python scripts/review_regulations.py --reject <法规ID> --note "转发的附件型公告，无正文"

为什么要有这个脚本：
  复核是"审核专家"的动作，正式入口是复核台。在那之前，
  批量导入的上百份法规如果只能一条条点，等于没法推进。
  本脚本走的是跟接口完全相同的服务层规则（app/services/review.py），
  不存在"脚本能绕过接口校验"的后门。

两条硬规则：
  · 执行人必须真的拥有 knowledge.review 权限，脚本会去库里核对，不自称。
  · 每次放行/驳回都写审计日志，谁在什么时候放行了什么，事后可查。

放行之后默认会重建向量索引——不然"放行了却搜不到"。
不想重建加 --no-rebuild。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


def _resolve_actor(session, username: str | None):
    """确定执行人，并核对其确实拥有复核权限。"""

    from sqlalchemy import select

    from app.domain.permissions import KNOWLEDGE_REVIEW
    from app.models import User
    from app.services.authorization import load_principal

    if username:
        statement = select(User).where(User.username == username)
    else:
        statement = select(User).where(User.is_protected.is_(True))

    user = session.execute(statement).scalars().first()
    if user is None:
        raise SystemExit(
            f"[错误] 找不到执行人账号（--actor {username}）"
            if username
            else "[错误] 库里没有固定管理员，请先启动一次后端完成初始化"
        )

    principal = load_principal(session, user)
    if not principal.has(KNOWLEDGE_REVIEW):
        raise SystemExit(f"[错误] 账号 {user.username} 没有 knowledge.review 权限，不能执行复核")
    return user, principal


def main() -> int:
    parser = argparse.ArgumentParser(description="知识复核：待复核清单 / 放行 / 驳回")
    parser.add_argument("--list", action="store_true", help="打印待复核清单")
    parser.add_argument("--approve", metavar="REGULATION_ID", help="放行一条")
    parser.add_argument("--reject", metavar="REGULATION_ID", help="驳回一条")
    parser.add_argument("--approve-all", action="store_true", help="放行全部待复核法规")
    parser.add_argument("--domain", default="finance_tax", help="域包标识，默认 finance_tax")
    parser.add_argument("--actor", default=None, help="执行人用户名，默认取固定管理员")
    parser.add_argument("--note", default=None, help="复核意见，会写进审计日志")
    parser.add_argument("--no-rebuild", action="store_true", help="放行后不重建向量索引")
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from app.database.session import SessionLocal
    from app.models.knowledge import Regulation
    from app.retrieval.indexer import index_articles
    from app.retrieval.qdrant_client import get_client
    from app.services.review import (
        ACTION_APPROVE,
        ACTION_REJECT,
        apply_decision,
        list_pending,
        pending_reasons,
    )

    session = SessionLocal()
    try:
        user, principal = _resolve_actor(session, args.actor)
        scope = "固定管理员，拥有全部权限" if principal.is_protected else f"权限 {len(principal.permissions)} 项"
        print(f"执行人：{user.username}（{scope}，含 knowledge.review）")

        pending = list_pending(session, domain_id=args.domain, limit=1000)

        if args.list or not (args.approve or args.reject or args.approve_all):
            print(f"\n待复核 {len(pending)} 条：")
            for item in pending:
                reasons = "；".join(pending_reasons(item)) or "无（可放行）"
                number = item.document_number or "（无文号）"
                print(f"  · [{item.id}] {item.title}")
                print(f"      位阶={item.hierarchy_level or '未判定'} 文号={number}")
                print(f"      原因：{reasons}")
            if not pending:
                print("  （空）")
            return 0

        targets: list[Regulation] = []
        action = ACTION_APPROVE
        if args.approve_all:
            targets = pending
        elif args.approve:
            action = ACTION_APPROVE
            record = session.get(Regulation, args.approve)
            if record is None:
                raise SystemExit(f"[错误] 找不到法规：{args.approve}")
            targets = [record]
        elif args.reject:
            action = ACTION_REJECT
            record = session.get(Regulation, args.reject)
            if record is None:
                raise SystemExit(f"[错误] 找不到法规：{args.reject}")
            targets = [record]

        if not targets:
            print("没有需要处理的法规。")
            return 0

        # 整批一个事务：要么都放行，要么都不动，不会留下"放行了一半"的状态
        for record in targets:
            apply_decision(
                session,
                record,
                action=action,
                note=args.note,
                actor_id=user.id,
                actor_username=user.username,
            )
        session.commit()

        verb = "放行" if action == ACTION_APPROVE else "驳回"
        print(f"\n已{verb} {len(targets)} 条：")
        for record in targets[:20]:
            print(f"  · {record.title}")
        if len(targets) > 20:
            print(f"  …… 其余 {len(targets) - 20} 条")

        if not args.no_rebuild:
            stats = index_articles(session, client=get_client(), domain_id=args.domain)
            print("\n向量索引已重建：")
            for key, value in stats.items():
                print(f"  {key}: {value}")
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
