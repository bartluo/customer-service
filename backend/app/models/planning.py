"""筹划手法库（技术方案 4.11）。

筹划能力不来自"读得多"，而来自**结构化的手法库**——
这是财税域第二个必须新建的知识资产（第一个是法规库）。

每个手法必须记录六个字段，其中**"滥用边界"是最有价值的一条**：
同一手法在合规使用和滥用之间只差一条线，把这条件明确写出来，
系统才能在方案里自动加上"这样用是合法的，一旦 X 就变成违法"的提示。
这是用户真正需要的专业价值，也是普通资料里没有的东西。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.models.knowledge import KnowledgeTimestampMixin


class PlanningTechnique(Base, KnowledgeTimestampMixin):
    """一条筹划手法。"""

    __tablename__ = "planning_techniques"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 七类之一：政策适用型 / 主体架构型 / 业务模式型 / 时点安排型 / 资产与投资型 / 区域型 / 递延型
    category: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    # ① 适用条件：可机器判定
    conditions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # ② 法律依据：到条款级
    citations: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # ③ 节税机理：为什么能省钱（税基 / 税率 / 时点）
    mechanism: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # ④ 风险等级：green / yellow / red
    risk_level: Mapped[str] = mapped_column(String(10), default="yellow", nullable=False, index=True)
    # ⑤ 滥用边界：什么情况下会从合法变成违法
    abuse_boundary: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # ⑥ 被否案例：已知被税务机关调整或否定的情形
    rejected_cases: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    # 方案落地用的操作信息（每个方案 = 具体动作 + 依据 + 前置条件 + 实施成本）。
    # 放一个 JSONB 而不是拆成四列：这四项总是一起用，拆开只会让表更宽、查询更碎。
    #   {"actions": [...], "steps": [...], "evidence": [...], "cost": "低"}
    playbook: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # 可量化测算的效果。结构化表达，交给计算引擎算，不让模型估。
    #   {"tax": "vat", "kind": "exempt"}           增值税免征
    #   {"tax": "surcharges", "kind": "halve"}     附加税费减半
    #   {"tax": "cit", "kind": "small_low_profit"} 小型微利企业优惠
    measure: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # 上线控制：手法素材需财税专家确认后才能用于对外出方案
    review_state: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (Index("ix_planning_techniques_category_risk", "category", "risk_level"),)


class PlanningReview(Base, KnowledgeTimestampMixin):
    """专家复核台的一条待办（技术方案 4.13）。

    两类东西会进这里：
      · 🔴 高风险方案 —— **只进这里，不发给用户**；
      · 🟡 审慎方案与 🟢 抽检方案 —— 给用户的同时抄送，供事后复核。

    为什么要存"当时给用户看的是什么"（`delivered_to_user`）：
    专家事后抽检时，要看的是**用户实际收到的那份**，而不是系统现在重新生成的结果。
    两者的差异本身就是线索——如果重新生成的变了，说明知识库或规则动过了。
    """

    __tablename__ = "planning_reviews"

    id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    question: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 企业画像快照（一屏看清要用）
    profile: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # 方案快照（含依据、测算、风险点、滥用边界）
    plan: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    technique_code: Mapped[str] = mapped_column(String(80), default="", nullable=False, index=True)
    risk_level: Mapped[str] = mapped_column(String(10), default="red", nullable=False, index=True)
    # 是否已发给用户。🔴 恒为 False——这条字段就是"高风险不外泄"的落库凭证。
    delivered_to_user: Mapped[bool] = mapped_column(default=False, nullable=False)

    # pending / approved / approved_with_changes / rejected
    status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    reviewer: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 专家改了什么（一键修正的记录：改前 / 改后）
    changes: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
