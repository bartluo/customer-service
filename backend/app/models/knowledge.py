"""财税法规结构化模型（技术方案 v3.3 第 10.2 节）。

四张表把一份原始法规变成"带时效、可精确定位、可追溯"的条文库：

  regulations                  法规主表：文号、标题、发文机关、位阶、地域、税种
  regulation_articles          条文主表：章 / 节 / 条 / 款 / 项 的层级结构
  regulation_article_versions  条文版本表：内容 + [valid_from, valid_to) + 效力状态
  regulation_relations         关系边：based_on / amended_by / repealed_by / references

三条硬约束写进数据库，不靠应用层自觉：
  · 没有来源留痕（source_url + retrieved_at）的法规不入库 —— 技术方案 10.3
  · 每个条文版本必须带 valid_from，不允许空区间 —— ontology time_range_required
  · 同一条文的任意两个版本区间不得重叠 —— ontology no_overlapping_ranges

关于区间重叠：用 PostgreSQL 排斥约束（EXCLUDE USING gist）实现，不用应用层检查。
理由是并发导入时两个事务各自"查一下没重叠"再插入会竞态，
只有数据库级的排斥约束能真正堵住漏洞。
"""

from __future__ import annotations
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import (
    JSONB,
    ExcludeConstraint,
    UUID as PGUUID,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.knowledge.ontology import (
    EFFECT_STATUSES,
    GRANULARITY_LEVELS,
    HIERARCHY_LEVELS,
    REGION_SCOPES,
    RELATION_TYPES,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _sql_in_list(values: tuple[str, ...]) -> str:
    """把枚举转成 SQL 的 IN (...) 字面量。

    这些枚举全部是模块内写死的常量（不含用户输入），因此内联拼接安全。
    用数据库 CHECK 约束而不是只靠应用层枚举校验，是为了防止绕过应用写脏数据。
    """

    return ",".join("'" + value + "'" for value in values)


class KnowledgeTimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Regulation(Base, KnowledgeTimestampMixin):
    """法规主表。

    effect_status 是整部法规当前的效力状态（ADR-0006 六种之一）。
    整部法规的 effect_status 与条文级 effect_status 是两个层级：
    某部法规整体有效、但其中一条被单独废止时，整部标 effective、
    那一条的版本标 repealed，两边并不冲突。
    """

    __tablename__ = "regulations"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    # 域包标识：财税为 finance_tax
    domain_id: Mapped[str] = mapped_column(
        String(64), default="finance_tax", nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    # 文号，如 财税〔2019〕39号。识别不出时留空并转人工复核，绝不编造
    document_number: Mapped[str | None] = mapped_column(String(200), index=True)
    issuer: Mapped[str | None] = mapped_column(String(500))
    hierarchy_level: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    region_scope: Mapped[str] = mapped_column(String(40), default="national", nullable=False)

    publish_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tax_types: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    applies_to: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)

    source_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 原件在对象存储里的键（SeaweedFS / S3 兼容），供追溯与复核
    source_file_key: Mapped[str | None] = mapped_column(String(500))
    content_hash: Mapped[str | None] = mapped_column(String(64))

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    effect_status: Mapped[str] = mapped_column(String(40), default="effective", nullable=False, index=True)
    # 导入流水线状态：pending_review（待专家审核）/ published / rejected
    review_state: Mapped[str] = mapped_column(String(40), default="pending_review", nullable=False, index=True)
    is_draft: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    articles: Mapped[list["RegulationArticle"]] = relationship(
        back_populates="regulation", cascade="all, delete-orphan"
    )
    relations: Mapped[list["RegulationRelation"]] = relationship(
        back_populates="source_regulation",
        cascade="all, delete-orphan",
        # regulation_relations 有两个指向 regulations 的外键（source / target），
        # 必须显式指定哪一个是本关系的来源，否则 ORM 无法判断
        foreign_keys="RegulationRelation.source_regulation_id",
    )

    __table_args__ = (
        CheckConstraint(
            "hierarchy_level IN (" + _sql_in_list(HIERARCHY_LEVELS) + ")",
            name="ck_regulations_hierarchy_level",
        ),
        CheckConstraint(
            "effect_status IN (" + _sql_in_list(EFFECT_STATUSES) + ")",
            name="ck_regulations_effect_status",
        ),
        CheckConstraint(
            "region_scope IN (" + _sql_in_list(REGION_SCOPES) + ")",
            name="ck_regulations_region_scope",
        ),
        Index("idx_regulations_effect", "effect_status", "publish_date"),
        Index("idx_regulations_tax_types", "tax_types", postgresql_using="gin"),
    )


class RegulationArticle(Base, KnowledgeTimestampMixin):
    """条文主表：章 / 节 / 条 / 款 / 项 的层级结构。

    用 level_code + full_no + order_index + parent_article_id 表达多层结构。
    parent_article_id 指向上一层级（如"款"指向它所属的"条"），
    这样才能精确定位到"第七条第二款"。
    """

    __tablename__ = "regulation_articles"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    regulation_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("regulations.id", ondelete="CASCADE"), index=True
    )
    # chapter / section / article / paragraph / item / subitem
    level_code: Mapped[str] = mapped_column(String(20), nullable=False)
    # 层级内的原始编号，如 "第一条"、"（二）"、"3."
    article_no: Mapped[str] = mapped_column(String(40), nullable=False)
    # 完整编号路径，如 "第一章 总则 第七条 第二款"
    full_no: Mapped[str] = mapped_column(String(200), nullable=False)
    # 章节目录路径，如 "第一章 总则/第一节 纳税义务人"
    heading_path: Mapped[str | None] = mapped_column(String(1000))
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_article_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("regulation_articles.id", ondelete="CASCADE")
    )

    regulation: Mapped[Regulation] = relationship(back_populates="articles")
    versions: Mapped[list["RegulationArticleVersion"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    children: Mapped[list["RegulationArticle"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped["RegulationArticle | None"] = relationship(
        back_populates="children", remote_side=[id]
    )

    __table_args__ = (
        CheckConstraint(
            "level_code IN (" + _sql_in_list(GRANULARITY_LEVELS) + ")",
            name="ck_articles_level_code",
        ),
        UniqueConstraint("regulation_id", "full_no", name="uq_article_reg_full_no"),
    )


class RegulationArticleVersion(Base, KnowledgeTimestampMixin):
    """条文版本表：内容 + [valid_from, valid_to) + 效力状态。

    为什么版本而不是直接改条文内容：一部法规的某一条会被后续文件多次修改，
    直接覆盖会丢失历史，导致"这条以前怎么规定的""什么时候变的"无法回答，
    也无法做政策变化提醒。每个版本是一个左闭右开的时间区间。
    """

    __tablename__ = "regulation_article_versions"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    article_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("regulation_articles.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 本版本生效日（含）。不允许为空 —— ontology time_range_required
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 本版本失效日（不含）。None 表示仍在有效期内（ontology open_valid_to）
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    effect_status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # 废止本版本的依据，如 "财税〔2019〕39号"
    repeal_basis: Mapped[str | None] = mapped_column(String(200))
    # 修改本版本的依据文号，如 "财税〔2019〕39号"
    amend_basis: Mapped[str | None] = mapped_column(String(200))

    source_file_key: Mapped[str | None] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))

    article: Mapped[RegulationArticle] = relationship(back_populates="versions")

    __table_args__ = (
        CheckConstraint(
            "effect_status IN (" + _sql_in_list(EFFECT_STATUSES) + ")",
            name="ck_article_versions_effect_status",
        ),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_version_valid_range"),
        UniqueConstraint("article_id", "version", name="uq_article_version"),
        # 技术方案 9.3：时效查询是最高频操作，必须有这两个索引
        Index("idx_article_version_range", "article_id", "valid_from", "valid_to"),
        Index("idx_article_effect", "effect_status", "valid_from"),
        # 数据库级排斥约束：同一条文任意两个版本区间不得重叠。
        # 需要 btree_gist 扩展，让 UUID 与时间范围能进同一个 GiST 索引。
        ExcludeConstraint(
            ("article_id", "="),
            (text("tstzrange(valid_from, valid_to, '[)')"), "&&"),
            using="gist",
            name="uq_article_version_no_overlap",
        ),
    )


class RegulationRelation(Base, KnowledgeTimestampMixin):
    """关系边表：为什么能"顺着查"。

    有了这些边，才能回答"这条被哪条改过""这个优惠有什么前提""谁废了它"。
    目标是法规级或条文级二选一；target_ref 保留原始提及文本，便于人工核对。
    """

    __tablename__ = "regulation_relations"

    id: Mapped[str] = mapped_column(PGUUID(as_uuid=False), primary_key=True, default=_uuid)
    source_article_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("regulation_articles.id", ondelete="CASCADE"), index=True
    )
    source_regulation_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("regulations.id", ondelete="CASCADE"), index=True
    )
    target_article_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("regulation_articles.id", ondelete="CASCADE"), index=True
    )
    target_regulation_id: Mapped[str | None] = mapped_column(
        PGUUID(as_uuid=False), ForeignKey("regulations.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # 原始提及文本，如 "财税〔2019〕39号 第一条"；无法精确到 ID 时保留原文
    target_ref: Mapped[str | None] = mapped_column(String(500))
    # 触发判定的原文片段，作为抽取可信度证据
    evidence: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    source_regulation: Mapped[Regulation | None] = relationship(
        back_populates="relations",
        # 与 Regulation.relations 对称：明确指向 source_regulation_id 这条外键
        foreign_keys=[source_regulation_id],
    )

    __table_args__ = (
        CheckConstraint(
            "relation_type IN (" + _sql_in_list(RELATION_TYPES) + ")",
            name="ck_relations_type",
        ),
        CheckConstraint(
            "source_article_id IS NOT NULL OR source_regulation_id IS NOT NULL",
            name="ck_relations_has_source",
        ),
    )
