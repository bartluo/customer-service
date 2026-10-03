"""评测与门禁验收。

跑法（项目根目录，需 docker compose up -d）：
    python scripts/verify_evaluation.py

检查项：
  · G9-A 评测集齐备且题型分布合理
  · G9-B 自动评测流水线能跑出指标（检索、依据、过程、表达四个维度）
  · G9-C 正常状态下门禁放行
  · G9-D **故意导入一条错误知识 → 门禁阻断发布，并给出失败用例明细**（验收门 G9）
  · G9-E 三道闸与回滚可执行

退出码：0 = 全部通过；1 = 有失败项

关于 G9-D 的"错误知识"：
  用最典型、最要命的一类错误——**把已废止的法规当成现行有效发布**。
  这类错误在真实系统里非常常见（审核放行时点错、批量发布时漏看），
  后果是用户拿着废止的条款去申报。
  注入方式：把主表与条文版本表的效力状态都改成 effective，再走一遍
  **真实的发布后索引重建**（`reindex_regulation`）——这一步不能省：
  已废止法规的索引点在时效治理时就被清掉了（ADR-0012），
  只改状态不重建索引，它压根查不到，门禁也就测不出东西。
  验收结束后会还原。
"""

from __future__ import annotations

import pathlib
import sys
import time

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))


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
    from app.evaluation import EvalRunner, GateKeeper
    from app.evaluation.runner import persist_run
    from app.models.eval import CASE_INSUFFICIENT, CASE_REPEALED_TRAP, CASE_STANDARD, EvalCase
    from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
    from app.retrieval.indexer import reindex_regulation

    checker = Checker()
    session = SessionLocal()
    victim: Regulation | None = None
    original_effect = original_review = None
    original_version_states: dict[str, str] = {}
    try:
        print("[G9-A] 评测集")
        counts: dict[str, int] = {}
        for case in session.execute(select(EvalCase)).scalars().all():
            counts[case.case_type] = counts.get(case.case_type, 0) + 1
        total = sum(counts.values())
        checker.check("A1", "评测集非空", total > 0, f"共 {total} 条：{counts}")
        checker.check(
            "A2",
            "含废止陷阱题",
            counts.get(CASE_REPEALED_TRAP, 0) > 0,
            f"{counts.get(CASE_REPEALED_TRAP, 0)} 条",
        )
        checker.check(
            "A3",
            "含信息不足题",
            counts.get(CASE_INSUFFICIENT, 0) > 0,
            f"{counts.get(CASE_INSUFFICIENT, 0)} 条",
        )
        # 标准题必须带话题线索，否则"只认一条具体条款"会把正确答案判错
        standard = session.execute(
            select(EvalCase).where(EvalCase.case_type == CASE_STANDARD).limit(20)
        ).scalars().all()
        checker.check(
            "A4",
            "标准题带话题线索（判定口径公平）",
            all(case.expected_points for case in standard) if standard else False,
            f"抽查 {len(standard)} 条",
        )

        print("\n[G9-B] 自动评测流水线")
        runner = EvalRunner(session)
        report = runner.run(case_types=(CASE_REPEALED_TRAP, CASE_INSUFFICIENT))
        checker.check("B1", "能跑出指标报告", report.total > 0, f"跑了 {report.total} 条")
        checker.check(
            "B2",
            "指标含检索与红线两类",
            "Recall@5" in report.metrics and "废止陷阱泄漏数" in report.metrics,
            "、".join(report.metrics.keys()),
        )
        persist_run(session, report, gate="offline")

        print("\n[G9-C] 正常状态下门禁放行")
        keeper = GateKeeper(session)
        baseline = keeper.evaluate(report, gate="offline", release_ref="baseline")
        checker.check(
            "C1",
            "无红线泄漏时放行",
            baseline.released,
            "；".join(baseline.reasons) or "；".join(baseline.veto_items),
        )

        print("\n[G9-D] 故意导入一条错误知识 → 门禁应阻断")
        victim = session.execute(
            select(Regulation)
            .where(
                Regulation.effect_status.in_(("repealed", "superseded")),
                Regulation.review_state == "published",
                # 只挑"唯一的问题就是效力状态标错"的法规。
                # 有一部分废止法规是"执行期限届满"判出来的（带 valid_to），
                # 那类即使把状态改成 effective，时点过滤仍然会拦住它——
                # 测到的就不是"效力状态标错"这条红线了，验收结论会失焦。
                ~Regulation.id.in_(
                    select(RegulationArticle.regulation_id).where(
                        RegulationArticle.id.in_(
                            select(RegulationArticleVersion.article_id).where(
                                RegulationArticleVersion.valid_to.is_not(None)
                            )
                        )
                    )
                ),
            )
            .order_by(Regulation.id)
            .limit(1)
        ).scalars().first()
        checker.check("D1", "找到可用于注入的已废止法规", victim is not None, victim.title if victim else "无")
        if victim is not None:
            original_effect, original_review = victim.effect_status, victim.review_state
            # 针对这条法规造一条陷阱题：**直接用它的标题提问**。
            # 这样"注入前查不到、注入后查得到"是确定的——
            # 用通用问题（"增值税现在怎么规定"）不保证它进 Top5，
            # 验收会时灵时不灵，那样的门禁没人敢信。
            from app.evaluation.runner import EvalRunReport
            from app.models.eval import CASE_REPEALED_TRAP as TRAP
            from app.models.eval import EvalCase

            probe = EvalCase(
                case_type=TRAP,
                question=victim.title,
                forbidden_citations=[(victim.document_number or victim.title).strip()],
                expected_citations=[],
                expected_points=[],
                expected_behavior="answer",
                tax_type=(victim.tax_types or [None])[0],
                review_state="draft",
            )
            before = runner.run_case(probe)
            checker.check(
                "D2", "注入前该法规查不到（已废止被正确过滤）", before.passed, before.detail
            )

            # 注入：把已废止的法规整份改成"现行有效"（模拟审核放行时点错）。
            # 主表和条文版本表都要改——检索过滤与检索后的权威复核看的都是
            # 版本表的 effect_status，只改主表等于没改（ADR-0012 的教训）。
            victim.effect_status = "effective"
            versions = session.execute(
                select(RegulationArticleVersion).where(
                    RegulationArticleVersion.article_id.in_(
                        select(RegulationArticle.id).where(
                            RegulationArticle.regulation_id == victim.id
                        )
                    ),
                    RegulationArticleVersion.effect_status != "effective",
                )
            ).scalars().all()
            for version in versions:
                original_version_states[version.id] = version.effect_status
                version.effect_status = "effective"
            session.commit()

            # 状态改了还不够：这条法规的索引点在时效治理时就被清掉了
            # （ADR-0012 的索引清理）。发布流程的最后一个动作就是重建索引，
            # 这里照做——否则它压根查不到，门禁也就没机会拦住它。
            outcome = reindex_regulation(session, victim.id)
            print(
                f"     已注入错误知识：{victim.title}"
                f"（由 {original_effect} 改为 effective，"
                f"重灌索引点 {outcome.get('indexed', 0)} 条）"
            )

            after = runner.run_case(probe)
            infected = EvalRunReport(total=1, passed=1 if after.passed else 0, failed=0 if after.passed else 1)
            infected.results = [after]
            infected.failures = [] if after.passed else [after]
            infected.metrics = {
                "用例总数": 1,
                "通过数": infected.passed,
                "通过率": float(infected.passed),
                "废止陷阱泄漏数": 0 if after.passed else 1,
                "信息不足硬答数": 0,
            }
            checker.check(
                "D3",
                "评测能发现这次注入（废止陷阱泄漏）",
                not after.passed,
                after.detail,
            )
            decision = keeper.evaluate(infected, gate="offline", release_ref="with-bad-knowledge")
            checker.check(
                "D4",
                "门禁阻断发布",
                decision.decision == "blocked",
                f"decision={decision.decision}",
            )
            checker.check(
                "D5",
                "阻断原因是引用已废止条款（一票否决）",
                bool(decision.veto_items),
                "；".join(decision.veto_items),
            )
            checker.check(
                "D6",
                "给出失败用例明细",
                len(decision.failed_cases) > 0,
                f"{len(decision.failed_cases)} 条失败明细",
            )
            if decision.failed_cases:
                sample = decision.failed_cases[0]
                print(f"     失败明细示例：[{sample['case_type']}] {sample['question'][:30]}")
                print(f"       {sample['detail'][:80]}")

        print("\n[G9-E] 三道闸与回滚")
        checker.check("E1", "离线闸已执行", keeper.latest(gate="offline") is not None)
        checker.check(
            "E2",
            "灰度阶梯只能逐级放量（5%→20%→50%→100%）",
            keeper.canary_stage("canary", 0.1) == 0.20
            and keeper.canary_stage("canary", 0.45) == 0.50
            and keeper.canary_stage("canary", 0.9) == 1.00,
            "阶梯校验通过",
        )
        latest = keeper.latest(gate="offline")
        if latest is not None:
            rolled = keeper.rollback(latest.id, reason="验收演练：回滚留痕")
            checker.check(
                "E3",
                "回滚可执行且留痕",
                rolled.rolled_back and rolled.decision == "rolled_back",
                f"decision={rolled.decision}",
            )
    finally:
        # 还原注入的错误知识
        if victim is not None and original_effect is not None:
            victim.effect_status = original_effect
            if original_review:
                victim.review_state = original_review
            for version_id, status in original_version_states.items():
                session.get(RegulationArticleVersion, version_id).effect_status = status
            session.commit()
            # 还原后重建一次索引：此时该法规没有可索引条目（又变回废止），
            # reindex_regulation 会把注入时补进去的点删干净。
            # 这一步不能省——残留的索引点会让"已废止"的条文继续被检索到，
            # 验收脚本自己就制造了一起事故。
            reindex_regulation(session, victim.id)
            print(f"\n已还原注入的错误知识：{victim.title} → {original_effect}")
        session.close()

    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
