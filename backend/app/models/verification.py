"""验证记录（技术方案 6.4）。

每次验证都留痕，用途有三个（都写在规格里）：
  · 统计各验证器的拦截率；
  · 失败原因分类，找出系统性问题（比如"总引用某份老文件"）；
  · 衡量推理稳定性（重试次数与结果）。

**验证层的指标本身也是预警信号**：引用验证拦截率短期内上升，
说明知识库质量出问题了，比等用户投诉早得多。
"""

from __future__ import annotations

import uuid

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.knowledge import KnowledgeTimestampMixin


class VerificationRecord(Base, KnowledgeTimestampMixin):
    """一次验证的记录。只增不改。"""

    __tablename__ = "verification_records"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    question: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # passed / fixed / regenerated / degraded
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    passed: Mapped[bool] = mapped_column(default=True, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 各验证器的检查项数与问题清单
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # 失败原因码，单独抽出来便于按月统计"哪类问题最多"
    issue_codes: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
