"""衰退淘汰：health_score 打分与复审队列。

技术方案 8.1 的 D 类流水线。财税域的特点（8.1）：
**以"法规效力变化"为主要触发器**——不是因为"没人用"才衰退，
而是因为它依据的政策变了。

所以 health_score 的构成以效力信号为主，使用率信号为辅。
当前系统还没有"被引用次数"的埋点，所以只用现有信号；
这一点如实反映在扣分项里，不装作有不存在的数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge import Regulation, RegulationArticle

# 各信号的扣分权重。效力类权重最高（财税域的主要触发器）。
WEIGHTS = {
    "effect_not_citable": 60.0,  # 已废止 / 已被替代：内容整体不可用
    "effect_expired": 30.0,  # 执行期已届满
    "not_yet_effective": 5.0,  # 尚未生效：暂不该作依据，但不是"坏"
    "source_missing": 15.0,  # 缺来源链接：合规上不该收录
    "stale_review": 10.0,  # 长期未复核
    "incomplete": 10.0,  # 切不出条文，等于库里没有可检索内容
}

# 低于这个分数进复审队列
REVIEW_THRESHOLD = 70.0

# 多久没复核算"陈"
STALE_DAYS = 365


@dataclass
class HealthItem:
    """一条法规的健康度。"""

    regulation_id: str
    title: str
    score: float
    signals: list[str] = field(default_factory=list)
    effect_status: str = ""

    def to_dict(self) -> dict:
        return {
            "regulation_id": self.regulation_id,
            "title": self.title,
            "score": round(self.score, 1),
            "effect_status": self.effect_status,
            "signals": list(self.signals),
        }


class HealthScorer:
    """给法规打健康分，挑出需要复审的。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def score_all(self, *, limit: int | None = None) -> list[HealthItem]:
        statement = select(Regulation)
        if limit:
            statement = statement.limit(limit)
        regulations = self.session.execute(statement).scalars().all()

        items = [self._score(item, self._has_articles(item.id)) for item in regulations]
        items.sort(key=lambda entry: entry.score)
        return items

    def review_queue(self, *, threshold: float = REVIEW_THRESHOLD) -> list[HealthItem]:
        return [item for item in self.score_all() if item.score < threshold]

    # ------------------------------------------------------------------
    def _has_articles(self, regulation_id: str) -> bool:
        found = self.session.execute(
            select(RegulationArticle.id)
            .where(RegulationArticle.regulation_id == regulation_id)
            .limit(1)
        ).scalars().first()
        return found is not None

    def _score(self, regulation: Regulation, has_articles: bool) -> HealthItem:
        score = 100.0
        signals: list[str] = []

        if regulation.effect_status in {"repealed", "superseded", "draft"}:
            score -= WEIGHTS["effect_not_citable"]
            signals.append(f"效力状态为 {regulation.effect_status}，已不可引用")
        elif regulation.effect_status == "not_yet_effective":
            score -= WEIGHTS["not_yet_effective"]
            signals.append("尚未生效，暂不作为依据")

        if regulation.expiry_date is not None:
            expiry = regulation.expiry_date
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                score -= WEIGHTS["effect_expired"]
                signals.append("执行期已届满")

        if not (regulation.source_url or "").strip():
            score -= WEIGHTS["source_missing"]
            signals.append("缺来源链接，不符合收录要求")

        if not has_articles:
            score -= WEIGHTS["incomplete"]
            signals.append("没有可检索条文（内容不完整）")

        if regulation.updated_at is not None:
            updated = regulation.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - updated > timedelta(days=STALE_DAYS):
                score -= WEIGHTS["stale_review"]
                signals.append(f"超过 {STALE_DAYS} 天未复核")

        return HealthItem(
            regulation_id=regulation.id,
            title=regulation.title,
            score=max(score, 0.0),
            signals=signals,
            effect_status=regulation.effect_status,
        )
