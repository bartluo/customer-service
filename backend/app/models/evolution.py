"""自进化相关的两张表（技术方案第 8 章）。

为什么变更事件要单独建表、而不是只看法规的 created_at：
  验收门 G8 要求"从发现到知识更新完成 ≤ 24 小时"。要度量这个时间，
  就必须记录**发现时刻**与**完成时刻**——法规表里只有最终入库时间，
  看不出它是什么时候被发现的、中间卡在哪一步。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.knowledge import KnowledgeTimestampMixin


class EvolutionEvent(Base, KnowledgeTimestampMixin):
    """一次法规变更事件的全过程。

    状态流转：discovered → analyzed → imported → published → indexed → done
    每一步都盖时间戳，G8 的"≤24 小时"就是首末时间差。
    """

    __tablename__ = "evolution_events"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 事件类型：new_regulation（发现新文件）/ amendment（修订）/ repeal（废止）
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(30), default="discovered", nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    document_number: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_url: Mapped[str] = mapped_column(String(1000), default="", nullable=False)
    # 影响分析结果：受影响的已有法规清单与原因
    impact: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class KnowledgeGap(Base, KnowledgeTimestampMixin):
    """知识缺口（C 类流水线：低置信 + 未命中聚类）。

    一条缺口 = 一类"用户问得多、但知识库答不上来"的问题。
    财税域额外统计"哪些税种/情形问题最多但知识最少"——这决定了下一步该补什么。
    """

    __tablename__ = "knowledge_gaps"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 缺口主题（按税种或关键词聚类后的标签）
    topic: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    tax_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    sample_questions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 缺口原因：no_hit（完全没召回）/ low_confidence（召回但被判不适用或需确认）
    reason: Mapped[str] = mapped_column(String(40), default="no_hit", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)

    __table_args__ = (Index("ix_knowledge_gaps_topic_reason", "topic", "reason"),)
