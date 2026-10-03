"""推理链预览：把一句口语问题走完"事实抽取 → 候选召回 → 适用性判定"。

用法（项目根目录，需 docker compose up -d）：
    python scripts/reason.py --question "我开了个小卖部，上个月卖了103万，怎么交税"
    python scripts/reason.py --question "小微企业有什么所得税优惠" --top 5

为什么要有这个入口：
  答案模板与问答主链路都建立在这三步之上。
  先在命令行把效果看清楚——哪一步抽错了、哪条政策判错了，
  命令行比界面好定位得多。

输出四段：抽到的要素 / 待补要素与追问 / 候选政策 / 适用性三类清单。
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
    parser = argparse.ArgumentParser(description="推理链预览（事实抽取 + 适用性判定）")
    parser.add_argument("--question", required=True, help="用户的问题（用口语写）")
    parser.add_argument("--top", type=int, default=5, help="取前几条候选政策，默认 5")
    parser.add_argument("--tax-type", default=None, help="强制指定税种，不填按问题自动判断")
    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from app.database.session import SessionLocal
    from app.reasoning import ApplicabilityJudge, FactExtractor
    from app.reasoning.facts import load_extractor_config
    from app.retrieval.searcher import KnowledgeSearcher, SearchRequest

    extractor = FactExtractor(load_extractor_config())
    facts = extractor.extract(args.question)

    print("=" * 68)
    print("① 抽到的要素")
    if facts.evidence:
        for item in facts.evidence:
            print(f"   · {item.describe()}")
    else:
        print("   （没有抽到任何要素）")

    print("\n② 待补要素与建议追问")
    if facts.missing:
        for item in facts.missing:
            print(f"   · 缺：{item}")
        for question in facts.questions:
            print(f"   ? {question}")
    else:
        print("   （要素齐全）")

    session = SessionLocal()
    try:
        searcher = KnowledgeSearcher(session)
        request = SearchRequest(
            question=args.question,
            tax_types=[args.tax_type] if args.tax_type else list(facts.tax_types),
            top_n=args.top,
        )
        response = searcher.search(request)

        print("\n③ 召回的候选政策")
        if not response.citations:
            print("   （没有召回任何条文）")
        for citation in response.citations:
            print(f"   · {citation.label()}")
            print(f"     效力：{citation.effect_status}　税种：{'、'.join(citation.tax_types) or '未标注'}")

        candidates = [
            {
                "title": c.regulation_title or c.document_number or "",
                "citation": c.label(),
                "content": c.content or "",
                "tax_types": list(c.tax_types),
            }
            for c in response.citations
        ]
        report = ApplicabilityJudge().judge(facts, candidates)

        print("\n④ 适用性判定")
        print(report.explain())
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
