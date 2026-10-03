"""自进化引擎验收。

跑法（项目根目录，需 docker compose up -d）：
    python scripts/verify_evolution.py

检查项：
  · G8-A 变更监控能发现库里没有的新文件
  · G8-B 影响分析能认出新文件废止/修订了哪些既有法规
  · G8-C 缺口发现能从"没答好"的问题里聚类出缺口主题
  · G8-D 衰退体检能挑出应当复审的法规
  · G8-E **模拟一次政策修订，从发现到知识更新完成 ≤ 24 小时**（验收门的核心）
  · G8-F 政策变化摘要能生成，且未配置推送通道时如实说明

退出码：0 = 全部通过；1 = 有失败项

关于 G8-E 怎么模拟：
  真去等一次官方发布不现实，所以用**真实的链路 + 构造的触发源**：
    ① 造一份"废止决定"，正文点名废止库里一个真实存在的法规
    ② 走监控的登记入口（discovered_at 开始计时）
    ③ 跑影响分析，确认识别到被点名的法规
    ④ 真的导入这份新文件 → 放行 → 重建索引
    ⑤ 核对新文件可检索
  只有"触发源"是构造的，中间每一步都是真实代码。
"""

from __future__ import annotations

import pathlib
import sys
import time
from datetime import datetime, timezone

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

HOURS_BUDGET = 24


class Checker:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.start = time.time()

    def check(self, code: str, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(f"{code} {name}")
            print(f"  [通过] {code} {name}")
        else:
            self.failed.append((f"{code} {name}", detail))
            print(f"  [失败] {code} {name} — {detail}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print()
        print(f"验收结果：{len(self.passed)}/{total} 通过，用时 {time.time() - self.start:.1f} 秒")
        if self.failed:
            print("失败清单：")
            for name, detail in self.failed:
                print(f"  - {name}：{detail}")
            return 1
        return 0


def main() -> int:
    from build_vector_index import _load_env

    _load_env()

    from sqlalchemy import select

    from app.database.session import SessionLocal
    from app.evolution import (
        GapFinder,
        HealthScorer,
        OfficialMonitor,
        analyze_impact,
        build_change_digest,
    )
    from app.evolution.monitor import DiscoveredDocument
    from app.models.evolution import EvolutionEvent
    from app.models.knowledge import Regulation
    from app.models.verification import VerificationRecord
    from app.retrieval.indexer import index_articles
    from app.retrieval.qdrant_client import get_client
    from app.retrieval.searcher import KnowledgeSearcher, SearchRequest
    from app.services.ingest.importer import DocumentInput, import_documents
    from app.services.review import apply_decision, list_pending

    checker = Checker()
    session = SessionLocal()
    try:
        print("[G8-A] 法规变更监控")
        monitor = OfficialMonitor(session, pages=1, size=10)
        scan = monitor.scan(channels=["税务规范性文件"])
        checker.check(
            "A1",
            "能从官方栏目取到列表并与库内比对",
            scan.scanned > 0,
            f"扫描 {scan.scanned} 条，发现新文件 {len(scan.discovered)} 份"
            + (f"；错误：{scan.errors[:1]}" if scan.errors else ""),
        )
        checker.check(
            "A2",
            "按来源 URL 判重（标题会被重新排版，不能拿来判重）",
            len(monitor.known_urls) > 0,
            f"已知来源 {len(monitor.known_urls)} 条",
        )

        target = session.execute(
            select(Regulation)
            .where(
                Regulation.review_state == "published",
                Regulation.effect_status == "effective",
                Regulation.document_number.isnot(None),
            )
            .limit(1)
        ).scalars().first()
        assert target is not None, "库里没有可用于模拟的法规"
        print(f"     模拟对象：{target.title}（{target.document_number}）")

        print("\n[G8-B] 影响分析")
        new_doc_title = f"关于废止《{target.title}》的决定（验收模拟）"
        new_doc_text = (
            "财税〔2099〕99号\n"
            f"{new_doc_title}\n\n"
            f"经研究决定，废止《{target.title}》。\n"
            "本决定自发布之日起施行。\n"
        )
        analysis = analyze_impact(
            session,
            title=new_doc_title,
            content=new_doc_text,
            document_number="财税〔2099〕99号",
        )
        checker.check(
            "B1",
            "识别出正文点名废止的既有法规",
            any(item.regulation_id == target.id for item in analysis.impacted),
            f"识别到 {len(analysis.impacted)} 条受影响",
        )
        checker.check(
            "B2", "判断为需要触发复审", analysis.needs_review is True, f"kind={analysis.kind}"
        )

        reading = analyze_impact(
            session, title="关于某政策的解读", content="本文对某政策作解读。", document_number=None
        )
        checker.check(
            "B3",
            "解读性文章归档、不作为引用依据",
            reading.kind == "interpretation",
            f"kind={reading.kind}",
        )

        print("\n[G8-C] 缺口发现")
        finder = GapFinder(session)
        for question in (
            "合伙企业的所得税怎么交",
            "合伙企业分红怎么处理",
            "合伙企业要不要交企业所得税",
        ):
            session.add(
                VerificationRecord(
                    question=question,
                    outcome="degraded",
                    passed=False,
                    retries=0,
                    detail={},
                    issue_codes=["no_basis"],
                )
            )
        session.commit()
        summaries = finder.from_verification_records()
        checker.check(
            "C1", "从未答好的问题里聚类出缺口主题", len(summaries) > 0, f"共 {len(summaries)} 个主题"
        )
        checker.check(
            "C2",
            "缺口带可读主题与样例问题",
            all(item.topic and item.samples for item in summaries),
            "；".join(item.topic for item in summaries[:3]),
        )
        written = finder.persist(summaries)
        checker.check("C3", "缺口可落库供后续补数据", written > 0, f"写入 {written} 个主题")

        print("\n[G8-D] 衰退体检")
        scorer = HealthScorer(session)
        queue = scorer.review_queue()
        all_items = scorer.score_all()
        checker.check(
            "D1",
            "health_score 能算出来且有分档",
            len(all_items) > 0 and min(item.score for item in all_items) < 100,
            f"共 {len(all_items)} 条，复审队列 {len(queue)} 条",
        )
        checker.check(
            "D2",
            "低分法规带明确原因",
            all(item.signals for item in queue) if queue else True,
            f"队列 {len(queue)} 条",
        )

        print("\n[G8-E] 模拟一次政策修订，测「发现 → 更新完成」的耗时")
        event = monitor.record(
            DiscoveredDocument(
                title=new_doc_title, url="https://example.test/verify", channel="验收模拟"
            ),
            event_type="repealed",
        )
        checker.check("E1", "变更事件已登记（计时起点）", event.discovered_at is not None)

        event.impact = analysis.to_dict()
        session.commit()

        report = import_documents(
            session,
            [
                DocumentInput(
                    filename="verify_evolution_revision.txt",
                    text=new_doc_text,
                    source_url="https://example.test/verify",
                )
            ],
        )
        checker.check(
            "E2",
            "新文件真的能入库",
            report.success_count + report.skipped_count >= 1,
            f"成功 {report.success_count}，跳过 {report.skipped_count}，失败 {report.failed_count}",
        )

        for pending in list_pending(session, limit=500):
            if pending.title == new_doc_title or pending.document_number == "财税〔2099〕99号":
                apply_decision(
                    session,
                    pending,
                    action="approve",
                    note="验收模拟放行",
                    actor_id=None,
                    actor_username="system:verify_evolution",
                )
        session.commit()
        index_articles(session, client=get_client(), domain_id="finance_tax")

        searcher = KnowledgeSearcher(session)
        found = searcher.search(SearchRequest(question="废止某公告的决定 财税2099", top_n=5))
        hit = any(
            (citation.document_number or "") == "财税〔2099〕99号" for citation in found.citations
        )
        checker.check("E3", "更新后新文件可被检索到", hit, f"召回 {len(found.citations)} 条")

        event.status = "done"
        event.finished_at = datetime.now(timezone.utc)
        session.commit()
        hours = (event.finished_at - event.discovered_at).total_seconds() / 3600
        checker.check(
            "E4",
            f"发现到完成 ≤ {HOURS_BUDGET} 小时",
            hours <= HOURS_BUDGET,
            f"实际 {hours * 60:.1f} 分钟",
        )

        print("\n[G8-F] 政策变化摘要")
        digest = build_change_digest(session, limit=5)
        checker.check("F1", "能生成变化摘要", digest.count > 0, f"{digest.count} 条")
        checker.check(
            "F2",
            "未配置推送通道时如实说明（不假装已发送）",
            "未配置" in digest.delivery,
            digest.delivery,
        )

        # 清理：验收过程往库里塞了一份构造的法规，必须删掉。
        # 留着它，它会像真法规一样被检索、被引用——验收脚本不能污染生产数据。
        print("\n清理验收数据")
        from sqlalchemy import delete
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from app.retrieval.collections import KB_ARTICLES

        fake = session.execute(
            select(Regulation).where(Regulation.document_number == "财税〔2099〕99号")
        ).scalars().all()
        for row in fake:
            get_client().delete(
                collection_name=KB_ARTICLES,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="regulation_id", match=MatchValue(value=row.id)
                        )
                    ]
                ),
                wait=True,
            )
        if fake:
            session.execute(
                delete(Regulation).where(Regulation.document_number == "财税〔2099〕99号")
            )
        session.execute(
            delete(EvolutionEvent).where(EvolutionEvent.source_url == "https://example.test/verify")
        )
        session.commit()
        print(f"     已删除构造的法规 {len(fake)} 份及其索引点、变更事件")
    finally:
        session.close()

    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
