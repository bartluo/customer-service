"""问答主链路：问一个问题，拿到六段式结构化回答。

用法（项目根目录，需 docker compose up -d）：
    python scripts/ask.py --question "我是小规模纳税人，上个月在市区卖了10万元货物，怎么交税"
    python scripts/ask.py --question "..." --json      # 输出结构化 JSON

这是 M1 里程碑的演示入口：问一个税务问题，系统给出
判定 + 依据 + 计算过程 + 步骤 + 风险 + 免责声明。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


def main() -> int:
    parser = argparse.ArgumentParser(description="财税问答主链路")
    parser.add_argument("--question", required=True, help="用户的问题")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--tax-type", default=None, help="强制指定税种")
    parser.add_argument("--top", type=int, default=6, help="候选条文条数，默认 6")
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from app.database.session import SessionLocal
    from app.reasoning import AnswerPipeline

    session = SessionLocal()
    try:
        pipeline = AnswerPipeline(session, top_n=args.top)
        result = pipeline.answer(args.question, tax_type=args.tax_type)
    finally:
        session.close()

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if not result.refused else 3

    print("=" * 68)
    print(f"问题：{result.question}")
    print(f"意图：{result.intent}　模板：{result.template_id}")
    if result.degraded:
        print(f"降级：{'、'.join(result.degraded)}")
    print("=" * 68)
    print(result.to_text())
    if result.refused:
        print(f"\n[拒答] {result.refusal_reason}")
    return 0 if not result.refused else 3


if __name__ == "__main__":
    raise SystemExit(main())
