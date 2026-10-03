"""自进化引擎命令行。

用法（项目根目录，需 docker compose up -d）：
    python scripts/evolve.py scan            # 扫描官方栏目，找出库里没有的新文件
    python scripts/evolve.py scan --record   # 顺便把发现记入变更事件表（G8 计时起点）
    python scripts/evolve.py impact --file 新文件.txt    # 分析一份新文件影响了哪些已有知识
    python scripts/evolve.py gaps            # 汇总知识缺口（从验证记录里找"没答好"的问题）
    python scripts/evolve.py health          # 衰退体检：health_score 与复审队列
    python scripts/evolve.py digest          # 生成政策变化摘要
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
    parser = argparse.ArgumentParser(description="自进化引擎")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="扫描官方栏目找新文件")
    scan.add_argument("--pages", type=int, default=2, help="每个栏目翻几页，默认 2")
    scan.add_argument("--record", action="store_true", help="把发现记入变更事件表")

    impact = sub.add_parser("impact", help="分析一份新文件的影响")
    impact.add_argument("--file", required=True, help="新文件的路径（txt）")
    impact.add_argument("--title", help="标题，不填取文件名")
    impact.add_argument("--document-number", help="文号")

    gaps = sub.add_parser("gaps", help="汇总知识缺口")
    gaps.add_argument("--persist", action="store_true", help="把结果落库")
    gaps.add_argument("--by-tax-type", action="store_true", help="按税种汇总")

    sub.add_parser("health", help="衰退体检")
    sub.add_parser("digest", help="生成政策变化摘要")

    args = parser.parse_args()

    from build_vector_index import _load_env

    _load_env()

    from app.database.session import SessionLocal

    session = SessionLocal()
    try:
        if args.command == "scan":
            from app.evolution import OfficialMonitor

            monitor = OfficialMonitor(session, pages=args.pages)
            result = monitor.scan()
            print(f"扫描 {result.scanned} 条，发现新文件 {len(result.discovered)} 份")
            for item in result.discovered[:20]:
                print(f"  · [{item.channel}] {item.published}　{item.title[:50]}")
            if result.errors:
                print("取列表失败：")
                for error in result.errors:
                    print(f"  - {error}")
            if args.record and result.discovered:
                for item in result.discovered:
                    monitor.record(item)
                print(f"已记入变更事件表：{len(result.discovered)} 条（G8 计时起点）")
            return 0

        if args.command == "impact":
            from app.evolution import analyze_impact

            path = pathlib.Path(args.file)
            if not path.exists():
                raise SystemExit(f"[错误] 文件不存在：{path}")
            text = path.read_text(encoding="utf-8", errors="replace")
            title = args.title or path.stem
            analysis = analyze_impact(
                session, title=title, content=text, document_number=args.document_number
            )
            print(f"新文件：{title}")
            print(analysis.explain())
            print(f"\n是否触发复审：{'是' if analysis.needs_review else '否'}")
            return 0

        if args.command == "gaps":
            from app.evolution import GapFinder

            finder = GapFinder(session)
            if args.by_tax_type:
                rows = finder.by_tax_type()
                print("按税种汇总（问题多 / 知识少）：")
                for row in rows:
                    print(
                        f"  · {row['tax_type']}：问题 {row['occurrences']} 次 / "
                        f"{row['topics']} 个主题，已发布法规 {row['published_regulations']} 份"
                    )
                return 0
            summaries = finder.from_verification_records()
            print(f"从验证记录汇总出缺口：{len(summaries)} 个主题")
            for item in summaries[:20]:
                print(f"  · {item.topic}（{item.reason}）出现 {item.occurrences} 次")
                for sample in item.samples[:2]:
                    print(f"      例：{sample[:40]}")
            if args.persist and summaries:
                written = finder.persist(summaries)
                print(f"\n已落库：{written} 个缺口主题")
            return 0

        if args.command == "health":
            from app.evolution import HealthScorer

            scorer = HealthScorer(session)
            queue = scorer.review_queue()
            print(f"复审队列：{len(queue)} 条（低于 70 分）")
            for item in queue[:20]:
                print(f"  · [{item.score:.0f}分] {item.title[:44]}")
                for signal in item.signals:
                    print(f"      - {signal}")
            return 0

        if args.command == "digest":
            from app.evolution import build_change_digest

            digest = build_change_digest(session)
            print(digest.text())
            return 0
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
