"""评测集生成与导入。

技术方案 11.1 要求财税评测集起步 300 条，覆盖
税种 × 情形 × 时间 × 难度，并含 10% 废止陷阱、15% 信息不足。

**本模块生成的是候选题，一律以 draft 入库。**
标准答案必须由内部专家确认——让系统自己出题又自己判卷，
等于把"对不对"交给同一个可能出错的环节。

生成方式（全部程序化、可复现）：
  · 标准题：从已发布的条文里抽，期望召回该条款
  · 废止陷阱：以某税种为话题，把库里的废止条款列为"禁止出现"
  · 信息不足题：去掉关键要素的问题，期望系统追问
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.tax_types import TAX_TYPES
from app.models.eval import (
    CASE_INSUFFICIENT,
    CASE_REPEALED_TRAP,
    CASE_STANDARD,
    EvalCase,
)
from app.models.knowledge import Regulation, RegulationArticle, RegulationArticleVersion

# 标准题的提问模板 + **内容线索**。
#
# 为什么必须带线索：若"按顺序轮着配模板"，会出现
# "环境保护税的纳税人怎么确定"配上"第一条"（立法目的条）这种错配——
# 问题问的是纳税人，期望答案却是一条跟纳税人无关的条文。
# 那样的评测题本身是错的，测出来的分数没有意义。
# 现在只在条文内容确实谈这个话题时才用它出题。
# 问"优惠"的题**必须带上主体**。
#
# 原因（见 ADR-0020）：优惠是按主体分档的
# （小规模纳税人 / 一般纳税人 / 小型微利企业 / 个体工商户 各不相同），
# 问题里不写主体时，系统的**正确行为是先问清楚主体**。
# 拿"增值税有哪些税收优惠"这种题去要求"必须答出条文"，测的是错的行为。
SUBJECTS = ("小规模纳税人", "一般纳税人", "小型微利企业", "个体工商户")

QUESTION_TEMPLATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("{tax}的纳税人怎么确定", ("纳税人", "纳税义务人")),
    ("{tax}的税率是多少", ("税率", "征收率")),
    ("{tax}的计税依据是什么", ("计税依据", "应纳税额")),
    ("{subject}的{tax}优惠有哪些", ("免征", "减免", "优惠")),
    ("{tax}的纳税义务发生时间怎么规定", ("纳税义务发生时间",)),
    ("{tax}怎么申报缴纳", ("申报", "缴纳")),
    ("{tax}的征收管理怎么规定", ("征收管理", "税务机关")),
)

# 信息不足题的模板：**必须问的是"我这种情况"，而不是"这个税种怎么规定"**。
# 信息不足题若写成"增值税怎么交"这种通用问法，那属于政策查询，直接答是对的；
# 拿它当"信息不足题"，等于要求系统对通用问题也反问，口径错了。
INSUFFICIENT_TEMPLATES = (
    "我这个业务要交{tax}吗",
    "我这个月大概要交多少{tax}",
    "我这种情况能享受{tax}优惠吗",
)


@dataclass
class GeneratedCase:
    case_type: str
    question: str
    expected_citations: list[str]
    forbidden_citations: list[str]
    expected_behavior: str
    tax_type: str | None
    difficulty: str = "medium"
    # 话题线索：判定"Top5 里有没有在讲这个话题"。
    # 为什么不能只认一条具体条款：同一个问题（"申报怎么缴"）下
    # 有好几条条文都算对，只认其中一条会把正确答案判成错的。
    expected_points: list[str] = field(default_factory=list)


def generate_cases(session: Session, *, target: int = 300) -> list[GeneratedCase]:
    """从知识库生成候选评测题。"""

    cases: list[GeneratedCase] = []
    cases.extend(_standard_cases(session, limit=int(target * 0.55)))
    cases.extend(_repealed_trap_cases(session, limit=int(target * 0.15)))
    cases.extend(_insufficient_cases(session, limit=int(target * 0.20)))
    # 剩下的用标准题补足（同税种不同问法）
    if len(cases) < target:
        cases.extend(_standard_cases(session, limit=target - len(cases), offset=len(cases)))
    return cases[: target * 2]  # 多生成一些备选，人工可挑


def _standard_cases(session: Session, *, limit: int, offset: int = 0) -> list[GeneratedCase]:
    """标准题：期望召回某个具体条款。"""

    rows = session.execute(
        select(RegulationArticle, RegulationArticleVersion, Regulation)
        .join(RegulationArticleVersion, RegulationArticleVersion.article_id == RegulationArticle.id)
        .join(Regulation, Regulation.id == RegulationArticle.regulation_id)
        .where(
            Regulation.review_state == "published",
            RegulationArticleVersion.effect_status == "effective",
            RegulationArticle.level_code == "article",
        )
        .offset(offset)
        .limit(limit)
    ).all()

    cases: list[GeneratedCase] = []
    for index, (article, version, regulation) in enumerate(rows):
        tax_type = next(
            (name for name in (regulation.tax_types or []) if name in TAX_TYPES), None
        )
        if not tax_type:
            continue
        content = version.content or ""
        # 只在这个条文确实谈了某个话题时才用它出题
        chosen = next(
            ((text, hints) for text, hints in QUESTION_TEMPLATES if any(h in content for h in hints)),
            None,
        )
        if chosen is None:
            continue
        template, hints = chosen
        label = (
            f"{regulation.document_number} {article.full_no}"
            if regulation.document_number
            else f"{regulation.title} {article.full_no}"
        )
        cases.append(
            GeneratedCase(
                case_type=CASE_STANDARD,
                question=template.format(
                    tax=tax_type, subject=SUBJECTS[index % len(SUBJECTS)]
                ),
                expected_citations=[label.strip()],
                forbidden_citations=[],
                expected_behavior="answer",
                tax_type=tax_type,
                expected_points=[hint for hint in hints if hint in content][:3],
            )
        )
    return cases


def _repealed_trap_cases(session: Session, *, limit: int) -> list[GeneratedCase]:
    """废止陷阱：这个问题下绝不能出现已废止的条款。

    这是整套评测里最有价值的一类：**"答错"不好自动判，
    但"引用了不该引用的东西"可以。**
    """

    repealed = session.execute(
        select(Regulation)
        .where(Regulation.effect_status.in_(("repealed", "superseded")))
        .limit(limit)
    ).scalars().all()

    cases: list[GeneratedCase] = []
    for regulation in repealed:
        tax_type = next(
            (name for name in (regulation.tax_types or []) if name in TAX_TYPES), "增值税"
        )
        label = (regulation.document_number or regulation.title or "").strip()
        if not label:
            continue
        cases.append(
            GeneratedCase(
                case_type=CASE_REPEALED_TRAP,
                question=f"{tax_type}现在是怎么规定的",
                expected_citations=[],
                forbidden_citations=[label],
                expected_behavior="answer",
                tax_type=tax_type,
                difficulty="hard",
            )
        )
    return cases


def _insufficient_cases(session: Session, *, limit: int) -> list[GeneratedCase]:
    """信息不足题：正确行为是追问，不是硬答。"""

    tax_types = [
        row[0]
        for row in session.execute(
            select(Regulation.tax_types).where(Regulation.review_state == "published").limit(200)
        ).all()
        if row[0]
    ]
    flat = [name for group in tax_types for name in group if name in TAX_TYPES]
    unique = list(dict.fromkeys(flat))

    cases: list[GeneratedCase] = []
    for index in range(limit):
        tax_type = unique[index % len(unique)] if unique else "增值税"
        template = INSUFFICIENT_TEMPLATES[index % len(INSUFFICIENT_TEMPLATES)]
        cases.append(
            GeneratedCase(
                case_type=CASE_INSUFFICIENT,
                question=template.format(tax=tax_type),
                expected_citations=[],
                forbidden_citations=[],
                expected_behavior="clarify",
                tax_type=tax_type,
                difficulty="medium",
            )
        )
    return cases


def import_cases(
    session: Session,
    cases: list[GeneratedCase],
    *,
    review_state: str = "draft",
    domain_id: str = "finance_tax",
) -> dict:
    """把候选题导入库（按问题文本幂等）。"""

    created = 0
    skipped = 0
    for item in cases:
        exists = session.execute(
            select(EvalCase).where(
                EvalCase.question == item.question, EvalCase.case_type == item.case_type
            )
        ).scalars().first()
        if exists is not None:
            skipped += 1
            continue
        session.add(
            EvalCase(
                domain_id=domain_id,
                case_type=item.case_type,
                question=item.question,
                expected_citations=item.expected_citations,
                forbidden_citations=item.forbidden_citations,
                expected_points=item.expected_points,
                expected_behavior=item.expected_behavior,
                tax_type=item.tax_type,
                difficulty=item.difficulty,
                review_state=review_state,
                source="generated:app/evaluation/seed.py",
            )
        )
        created += 1
    session.commit()
    return {
        "created": created,
        "skipped": skipped,
        "total": session.execute(select(EvalCase.id)).scalars().all().__len__(),
    }
