"""缺口发现：低置信 + 未命中问题聚类成缺口清单。

技术方案 8.1 的 C 类流水线，财税域额外要求：
**统计"哪些税种/情形问题最多但知识最少"**——这决定了下一步该补什么。

缺口从三个来源收集：
  1. 检索完全没召回的问题（no_hit）；
  2. 检索召回了但适用性判定全是"不适用/需确认"的问题（low_confidence）；
  3. 验证层降级输出的问题（degraded）——降级本身就是"系统答不好"的信号。

聚类用的是税种 + 关键词，不用模型：缺口主题要能被人工直接读懂并去补数据，
模型生成的聚类标签反而不好检索、不好指派。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.tax_types import detect_tax_types
from app.models.evolution import KnowledgeGap
from app.models.verification import VerificationRecord

# 从问题里抽"情形关键词"，用来把同一税种下的问题再分细一点
TOPIC_KEYWORDS = (
    "抵扣", "留抵", "退税", "免税", "减免", "起征点", "申报", "发票", "开具", "红字",
    "汇算清缴", "加计扣除", "优惠", "核定", "登记", "注销", "清算", "扣除", "税率",
)


@dataclass
class GapSummary:
    """一条缺口的汇总。"""

    topic: str
    tax_type: str | None
    reason: str
    occurrences: int
    samples: list[str]

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "tax_type": self.tax_type,
            "reason": self.reason,
            "occurrences": self.occurrences,
            "samples": list(self.samples),
        }


def _topic_of(question: str) -> tuple[str, str | None]:
    """把问题归到一个可读的主题上。

    税种识别复用**检索层同一套逻辑**：先认全名，再用术语扩展兜口语说法
    （"小规模""进项"这类）。这样缺口主题和检索的税种过滤口径一致——
    否则会出现"检索按增值税过滤、缺口却归到未标注"的错位。
    """

    tax_types = detect_tax_types(question or "")
    if not tax_types:
        # 口语说法（"小规模""进项"）认不出来时，借术语扩展兜一层
        from app.retrieval.glossary import GlossaryExpander

        tax_types = GlossaryExpander().expand_tax_types(question or "")
    tax_type = tax_types[0] if tax_types else None
    keyword = next((word for word in TOPIC_KEYWORDS if word in (question or "")), None)
    if tax_type and keyword:
        topic = f"{tax_type}·{keyword}"
    elif tax_type:
        topic = tax_type
    elif keyword:
        topic = f"未标注税种·{keyword}"
    else:
        topic = "未分类问题"
    return topic, tax_type


def record_gap(
    session: Session,
    *,
    question: str,
    reason: str = "no_hit",
    sample_limit: int = 5,
) -> KnowledgeGap:
    """记录一次缺口。同一主题累加次数并追加样本，不产生重复行。"""

    topic, tax_type = _topic_of(question)
    existing = session.execute(
        select(KnowledgeGap).where(KnowledgeGap.topic == topic, KnowledgeGap.reason == reason)
    ).scalars().first()
    if existing is None:
        gap = KnowledgeGap(
            topic=topic,
            tax_type=tax_type,
            sample_questions=[question][:sample_limit],
            occurrences=1,
            reason=reason,
        )
        session.add(gap)
    else:
        samples = list(existing.sample_questions or [])
        if question not in samples and len(samples) < sample_limit:
            samples.append(question)
        existing.sample_questions = samples
        existing.occurrences = int(existing.occurrences or 0) + 1
        gap = existing
    session.commit()
    return gap


class GapFinder:
    """从验证记录里汇总缺口。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def from_verification_records(self, *, limit: int = 500) -> list[GapSummary]:
        """把"没答好"的问题整理成缺口。

        依据验证记录（已经在记）：降级输出与拒答都说明系统这一块弱。
        """

        rows = self.session.execute(
            select(VerificationRecord)
            .where(VerificationRecord.passed.is_(False))
            .order_by(VerificationRecord.created_at.desc())
            .limit(limit)
        ).scalars().all()

        buckets: dict[tuple[str, str], GapSummary] = {}
        for row in rows:
            topic, tax_type = _topic_of(row.question)
            reason = "degraded" if row.outcome == "degraded" else "low_confidence"
            key = (topic, reason)
            summary = buckets.get(key)
            if summary is None:
                summary = GapSummary(topic=topic, tax_type=tax_type, reason=reason, occurrences=0, samples=[])
                buckets[key] = summary
            summary.occurrences += 1
            if len(summary.samples) < 5 and row.question not in summary.samples:
                summary.samples.append(row.question)
        return sorted(buckets.values(), key=lambda item: -item.occurrences)

    def persist(self, summaries: list[GapSummary]) -> int:
        """把汇总结果落库（按主题 + 原因幂等更新）。"""

        written = 0
        for summary in summaries:
            existing = self.session.execute(
                select(KnowledgeGap).where(
                    KnowledgeGap.topic == summary.topic, KnowledgeGap.reason == summary.reason
                )
            ).scalars().first()
            if existing is None:
                self.session.add(
                    KnowledgeGap(
                        topic=summary.topic,
                        tax_type=summary.tax_type,
                        sample_questions=summary.samples,
                        occurrences=summary.occurrences,
                        reason=summary.reason,
                    )
                )
            else:
                existing.occurrences = summary.occurrences
                existing.sample_questions = summary.samples
            written += 1
        self.session.commit()
        return written

    def open_gaps(self, *, limit: int = 50) -> list[KnowledgeGap]:
        return list(
            self.session.execute(
                select(KnowledgeGap)
                .where(KnowledgeGap.status == "open")
                .order_by(KnowledgeGap.occurrences.desc())
                .limit(limit)
            ).scalars().all()
        )

    def by_tax_type(self) -> list[dict]:
        """按税种汇总——回答"哪个税种问题最多但知识最少"。"""

        rows = self.session.execute(
            select(
                KnowledgeGap.tax_type,
                func.sum(KnowledgeGap.occurrences).label("total"),
                func.count(KnowledgeGap.id).label("topics"),
            )
            .group_by(KnowledgeGap.tax_type)
            .order_by(func.sum(KnowledgeGap.occurrences).desc())
        ).all()
        result: list[dict] = []
        for tax_type, total, topics in rows:
            # 该税种已入库的已发布法规数，用来算"问题多 / 知识少"的反差
            from app.models.knowledge import Regulation

            knowledge = self.session.execute(
                select(func.count(Regulation.id)).where(
                    Regulation.review_state == "published",
                    Regulation.tax_types.contains([tax_type]) if tax_type else False,
                )
            ).scalar() if tax_type else 0
            result.append(
                {
                    "tax_type": tax_type or "未标注",
                    "occurrences": int(total or 0),
                    "topics": int(topics or 0),
                    "published_regulations": int(knowledge or 0),
                }
            )
        return result
