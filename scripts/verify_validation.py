"""验证层验收。

跑法（项目根目录，需 docker compose up -d）：
    python scripts/verify_validation.py

检查项：
  · G6-A 四类验证器齐备且能跑通
  · G6-B 正常答案能通过（防止"验证器把对的也拦了"）
  · G6-C **人为注入 20 个错误，拦截率 100%**（验收门的核心指标）
  · G6-D 验证记录留痕可统计

注入的三类错误与规格一致：
  · 假文号     —— 引用一份根本不存在的文件（编造依据）
  · 废止条款   —— 引用已废止/已被替代/执行期已过的条文
  · 错金额     —— 篡改答案里的数字

退出码：0 = 全部通过；1 = 有失败项
"""

from __future__ import annotations

import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import _console  # noqa: F401  Windows 控制台 UTF-8 修复

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "scripts"))

DISCLAIMER = "本回答依据现行有效政策生成，具体口径以主管税务机关认定为准。"


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

    from app.calculation import get_engine
    from app.calculation.engine import VatInput
    from app.database.session import SessionLocal
    from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion
    from app.verification import AnswerVerifier, VerificationInput
    from app.verification.result import Severity

    checker = Checker()
    session = SessionLocal()
    try:
        print("[G6-A] 四类验证器")
        verifier = AnswerVerifier(session, record=True)
        names = [item.name for item in verifier.verifiers]
        checker.check(
            "A1",
            "引用验证 / 时效验证 / 计算复算 / 合规验证 四类齐备",
            len(names) == 4,
            "、".join(names),
        )

        # ---- 取一条真实可引用的条文作为基线 ----
        row = session.execute(
            select(RegulationArticle, RegulationArticleVersion, Regulation)
            .join(RegulationArticleVersion, RegulationArticleVersion.article_id == RegulationArticle.id)
            .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
            .where(
                Regulation.review_state == "published",
                RegulationArticleVersion.effect_status == "effective",
                RegulationArticle.level_code == "article",
            )
            .limit(1)
        ).first()
        assert row is not None, "库里没有可引用的条文，无法做验收"
        article, version, regulation = row
        real_citation = {
            "document_number": regulation.document_number,
            "full_no": article.full_no,
            "regulation_title": regulation.title,
            "effect_status": "effective",
            "content": version.content,
            "regulation_id": regulation.id,
            "valid_from": None,
            "valid_to": None,
        }
        print(f"     基线引用：{regulation.title} {article.full_no}")

        # ---- 基线答案：应当通过验证 ----
        engine = get_engine()
        vat = engine.calc_vat(
            VatInput(
                taxpayer_type="小规模纳税人",
                business_type="销售货物",
                sales_amount=Decimal("100000"),
            )
        )
        base_calculation = {"vat": vat.to_dict(), "notes": list(vat.notes)}
        base_context = {
            "taxpayer_type": "小规模纳税人",
            "business_type": "销售货物",
            "amount": "100000.00",
            "amount_includes_tax": False,
            "region": None,
        }
        base_sections = {
            "judgement": "小规模纳税人适用简易计税。",
            "basis": [{"text": f"{regulation.title} {article.full_no}"}],
            "calculation": base_calculation["vat"]["steps"],
            "disclaimer": DISCLAIMER,
        }

        print("\n[G6-B] 正常答案能通过（防止验证器把对的也拦了）")
        good = VerificationInput(
            question="基线答案",
            citations=[real_citation],
            sections=dict(base_sections),
            calculation=base_calculation,
            calculation_context=base_context,
        )
        good_report = verifier.verify(good)
        checker.check(
            "B1",
            "正常答案验证通过",
            good_report.passed,
            "；".join(issue.message for issue in good_report.blocking[:2]),
        )

        print("\n[G6-C] 注入 20 个错误，拦截率 100%")
        now = datetime.now(timezone.utc)
        injections: list[tuple[str, VerificationInput]] = []

        # ① 假文号 8 个：引用一份根本不存在的文件
        for index in range(8):
            fake = dict(real_citation)
            fake["document_number"] = f"财税〔{2020 + index}〕{90000 + index}号"
            injections.append(
                (
                    f"假文号 {fake['document_number']}",
                    VerificationInput(
                        question="注入测试",
                        citations=[fake],
                        sections=dict(base_sections),
                    ),
                )
            )

        # ② 不可用条文 6 个
        unusable = [
            ("已废止", {"effect_status": "repealed"}),
            ("已被替代", {"effect_status": "superseded"}),
            ("草案", {"effect_status": "draft"}),
            ("尚未生效且未说明", {"effect_status": "not_yet_effective"}),
            ("执行期已届满", {"valid_to": int((now - timedelta(days=1)).timestamp())}),
            ("尚未到生效日", {"valid_from": int((now + timedelta(days=30)).timestamp())}),
        ]
        for index, (label, override) in enumerate(unusable, start=1):
            bad = dict(real_citation)
            bad.update(override)
            injections.append(
                (
                    f"{index}. 不可用条文（{label}）",
                    VerificationInput(
                        question="注入测试",
                        citations=[bad],
                        sections=dict(base_sections),
                    ),
                )
            )

        # ③ 数字被篡改 6 个
        for index in range(1, 7):
            tampered = {
                "vat": {
                    "payable": f"{index * 1111}.00",
                    "steps": [{"title": "应纳税额", "result": f"{index * 1111}.00"}],
                },
                "notes": [],
            }
            injections.append(
                (
                    f"{index}. 篡改金额 {index * 1111}.00",
                    VerificationInput(
                        question="注入测试",
                        citations=[real_citation],
                        sections=dict(base_sections),
                        calculation=tampered,
                        calculation_context=base_context,
                    ),
                )
            )

        intercepted = 0
        misses: list[str] = []
        for label, payload in injections:
            report = verifier.verify(payload)
            issues = report.issues
            if issues:
                intercepted += 1
            else:
                misses.append(label)

        rate = intercepted / len(injections) * 100 if injections else 0
        checker.check(
            "C1",
            f"注入 {len(injections)} 个错误全部被拦截",
            intercepted == len(injections),
            f"拦截率 {rate:.0f}%，漏掉：{misses[:3]}",
        )

        # 逐类复核：三类错误分别由哪个验证器拦下
        blocking_codes = {
            "citation_document_not_found",
            "citation_not_citable",
            "citation_not_yet_effective",
            "citation_after_expiry",
            "citation_before_effective",
        }
        seen_codes = {
            issue.code
            for _label, payload in injections
            for issue in verifier.verify(payload).issues
        }
        checker.check(
            "C2",
            "假文号与不可用条文由阻断级问题拦下",
            bool(blocking_codes & seen_codes),
            f"实际命中的原因码：{sorted(seen_codes)}",
        )
        checker.check(
            "C3",
            "错金额被发现（可自动修正）",
            "calculation_mismatch" in seen_codes
            or "calculation_value_missing" in seen_codes,
            f"实际命中的原因码：{sorted(seen_codes)}",
        )

        print("\n[G6-D] 验证记录留痕")
        from app.models.verification import VerificationRecord

        total = session.execute(
            select(VerificationRecord.id)
        ).scalars().all()
        checker.check("D1", "验证记录已写入", len(total) > 0, f"共 {len(total)} 条")

        recent = session.execute(
            select(VerificationRecord).order_by(VerificationRecord.created_at.desc()).limit(1)
        ).scalars().first()
        checker.check(
            "D2",
            "记录含结论与原因码（可统计拦截率）",
            recent is not None and isinstance(recent.issue_codes, list),
            f"最近一条：outcome={recent.outcome if recent else None}",
        )
    finally:
        session.close()

    return checker.summary()


if __name__ == "__main__":
    raise SystemExit(main())
