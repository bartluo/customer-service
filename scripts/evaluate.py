"""评测与门禁命令行。

用法（项目根目录，需 docker compose up -d）：
    python scripts/evaluate.py seed --target 300    # 生成候选评测题（draft，待专家评审）
    python scripts/evaluate.py run --limit 100      # 跑评测，出指标报告
    python scripts/evaluate.py gate --limit 100     # 跑评测并判门禁
    python scripts/evaluate.py latest               # 看最近一次门禁结论
    python scripts/evaluate.py rollback <门禁ID> --reason "线上投诉激增"
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
    parser = argparse.ArgumentParser(description="评测与门禁")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="生成候选评测题")
    seed.add_argument("--target", type=int, default=300)
    seed.add_argument(
        "--review-state",
        default="draft",
        choices=["draft", "approved"],
        help="入库状态，默认 draft（待专家评审）",
    )

    run = sub.add_parser("run", help="跑评测")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--types", default=None, help="限定题型，逗号分隔")

    gate = sub.add_parser("gate", help="跑评测并判门禁")
    gate.add_argument("--limit", type=int, default=None)
    gate.add_argument("--gate", default="offline", choices=["offline", "shadow", "canary"])

    latest = sub.add_parser("latest", help="看最近一次门禁结论")
    latest.add_argument("--gate", default=None)

    rollback = sub.add_parser("rollback", help="回滚并留痕")
    rollback.add_argument("decision_id")
    rollback.add_argument("--reason", default="")

    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from app.database.session import SessionLocal

    session = SessionLocal()
    try:
        if args.command == "seed":
            from app.evaluation import generate_cases, import_cases

            cases = generate_cases(session, target=args.target)
            print(f"生成候选题 {len(cases)} 条：")
            counts: dict[str, int] = {}
            for item in cases:
                counts[item.case_type] = counts.get(item.case_type, 0) + 1
            for name, count in counts.items():
                print(f"  · {name}：{count} 条")
            stats = import_cases(session, cases, review_state=args.review_state)
            print(f"\n入库：新增 {stats['created']}，跳过重复 {stats['skipped']}，库里共 {stats['total']} 条")
            print(f"状态：{args.review_state}"
                  + ("（待专家评审）" if args.review_state == "draft" else ""))
            return 0

        if args.command in {"run", "gate"}:
            from app.evaluation import EvalRunner, GateKeeper
            from app.evaluation.runner import persist_run

            types = tuple(args.types.split(",")) if getattr(args, "types", None) else None
            runner = EvalRunner(session)
            report = runner.run(case_types=types, limit=args.limit)
            print(report.explain())
            persist_run(session, report, gate=getattr(args, "gate", "offline"))

            if args.command == "gate":
                print()
                result = GateKeeper(session).evaluate(report, gate=args.gate)
                print(result.explain())
                return 0 if result.released else 1
            return 0

        if args.command == "latest":
            from app.evaluation import GateKeeper

            row = GateKeeper(session).latest(gate=args.gate)
            if row is None:
                print("还没有门禁记录。")
                return 0
            print(f"最近一次门禁（{row.gate}）：{row.decision}")
            print(f"  评分：{row.score}")
            for item in row.veto_items or []:
                print(f"  ⛔ 一票否决：{item}")
            for reason in row.reasons or []:
                print(f"  · {reason}")
            print(f"  是否已回滚：{row.rolled_back}")
            return 0

        if args.command == "rollback":
            from app.evaluation import GateKeeper

            row = GateKeeper(session).rollback(args.decision_id, reason=args.reason)
            print(f"已回滚并留痕：{row.id}")
            print(f"  状态：{row.decision}　原因：{row.reasons[-1] if row.reasons else ''}")
            print("  提示：真正把版本切回去由 CI/CD 负责（接入）")
            return 0
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
