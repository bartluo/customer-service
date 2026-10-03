"""create regulation knowledge tables

Revision ID: 2b7c91d4e6a3
Revises: 1d84519a9a5e
Create Date: 2026-09-29 23:40:00.000000

财税知识结构化：四张表（技术方案 9.2）。
  · regulations                  法规主表
  · regulation_articles          条文主表（章/节/条/款/项）
  · regulation_article_versions  条文版本表（内容 + [valid_from, valid_to) + 效力状态）
  · regulation_relations         关系边表

关键点：区间不重叠用 PostgreSQL 排除约束实现（EXCLUDE USING gist），
需要 btree_gist 扩展让 UUID 能进 GiST 索引。此迁移在删表顺序上先删扩展引用方，
downgrade 时保留扩展（扩展可能被其他对象使用，不擅自删除）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "2b7c91d4e6a3"
down_revision: Union[str, None] = "1d84519a9a5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EFFECT_STATUSES = (
    "not_yet_effective",
    "effective",
    "partially_repealed",
    "repealed",
    "superseded",
    "draft",
)
HIERARCHY_LEVELS = (
    "law",
    "administrative_regulation",
    "departmental_rule",
    "normative_document",
    "local_normative",
    "normative_reply",
)
REGION_SCOPES = ("national", "provincial", "municipal", "district")
GRANULARITY_LEVELS = ("chapter", "section", "article", "paragraph", "item", "subitem")
RELATION_TYPES = (
    "based_on",
    "amends",
    "amended_by",
    "repeals",
    "repealed_by",
    "references",
    "referenced_by",
    "excepts",
    "excepted_by",
)


def _in_list(values: tuple[str, ...]) -> str:
    return ",".join("'" + value + "'" for value in values)


def upgrade() -> None:
    # btree_gist：让 UUID 的等值比较能进 GiST 索引，排除约束才能用
    op.execute("create extension if not exists btree_gist")

    op.create_table(
        "regulations",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("domain_id", sa.String(length=64), server_default="finance_tax", nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("document_number", sa.String(length=200), nullable=True),
        sa.Column("issuer", sa.String(length=500), nullable=True),
        sa.Column("hierarchy_level", sa.String(length=40), nullable=False),
        sa.Column("region_scope", sa.String(length=40), server_default="national", nullable=False),
        sa.Column("publish_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiry_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tax_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applies_to", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_file_key", sa.String(length=500), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("effect_status", sa.String(length=40), server_default="effective", nullable=False),
        sa.Column("review_state", sa.String(length=40), server_default="pending_review", nullable=False),
        sa.Column("is_draft", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "hierarchy_level IN (" + _in_list(HIERARCHY_LEVELS) + ")",
            name="ck_regulations_hierarchy_level",
        ),
        sa.CheckConstraint(
            "effect_status IN (" + _in_list(EFFECT_STATUSES) + ")",
            name="ck_regulations_effect_status",
        ),
        sa.CheckConstraint(
            "region_scope IN (" + _in_list(REGION_SCOPES) + ")",
            name="ck_regulations_region_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_regulations_domain_id"), "regulations", ["domain_id"])
    op.create_index(op.f("ix_regulations_title"), "regulations", ["title"])
    op.create_index(op.f("ix_regulations_document_number"), "regulations", ["document_number"])
    op.create_index(op.f("ix_regulations_hierarchy_level"), "regulations", ["hierarchy_level"])
    op.create_index(op.f("ix_regulations_effect_status"), "regulations", ["effect_status"])
    op.create_index(op.f("ix_regulations_review_state"), "regulations", ["review_state"])
    op.create_index("idx_regulations_effect", "regulations", ["effect_status", "publish_date"])
    op.create_index("idx_regulations_tax_types", "regulations", ["tax_types"], postgresql_using="gin")

    op.create_table(
        "regulation_articles",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("regulation_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("level_code", sa.String(length=20), nullable=False),
        sa.Column("article_no", sa.String(length=40), nullable=False),
        sa.Column("full_no", sa.String(length=200), nullable=False),
        sa.Column("heading_path", sa.String(length=1000), nullable=True),
        sa.Column("order_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parent_article_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "level_code IN (" + _in_list(GRANULARITY_LEVELS) + ")",
            name="ck_articles_level_code",
        ),
        sa.ForeignKeyConstraint(["regulation_id"], ["regulations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_article_id"], ["regulation_articles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("regulation_id", "full_no", name="uq_article_reg_full_no"),
    )
    op.create_index(op.f("ix_regulation_articles_regulation_id"), "regulation_articles", ["regulation_id"])

    op.create_table(
        "regulation_article_versions",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("article_id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effect_status", sa.String(length=40), nullable=False),
        sa.Column("repeal_basis", sa.String(length=200), nullable=True),
        sa.Column("amend_basis", sa.String(length=200), nullable=True),
        sa.Column("source_file_key", sa.String(length=500), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "effect_status IN (" + _in_list(EFFECT_STATUSES) + ")",
            name="ck_article_versions_effect_status",
        ),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_version_valid_range"),
        sa.ForeignKeyConstraint(["article_id"], ["regulation_articles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id", "version", name="uq_article_version"),
    )
    op.create_index(op.f("ix_regulation_article_versions_article_id"), "regulation_article_versions", ["article_id"])
    op.create_index(op.f("ix_regulation_article_versions_effect_status"), "regulation_article_versions", ["effect_status"])
    op.create_index("idx_article_version_range", "regulation_article_versions", ["article_id", "valid_from", "valid_to"])
    op.create_index("idx_article_effect", "regulation_article_versions", ["effect_status", "valid_from"])
    # 区间不重叠：同一条文的任意两个版本区间不得交叠，左闭右开。
    # NULL 生效失败会被排除掉，这是本约束的第二道作用
    op.execute(
        """
        ALTER TABLE regulation_article_versions
        ADD CONSTRAINT uq_article_version_no_overlap
        EXCLUDE USING gist (article_id WITH =, tstzrange(valid_from, valid_to, '[)') WITH &&)
        """
    )

    op.create_table(
        "regulation_relations",
        sa.Column("id", sa.UUID(as_uuid=False), nullable=False),
        sa.Column("source_article_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("source_regulation_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("target_article_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("target_regulation_id", sa.UUID(as_uuid=False), nullable=True),
        sa.Column("relation_type", sa.String(length=40), nullable=False),
        sa.Column("target_ref", sa.String(length=500), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "relation_type IN (" + _in_list(RELATION_TYPES) + ")",
            name="ck_relations_type",
        ),
        sa.CheckConstraint(
            "source_article_id IS NOT NULL OR source_regulation_id IS NOT NULL",
            name="ck_relations_has_source",
        ),
        sa.ForeignKeyConstraint(["source_article_id"], ["regulation_articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_regulation_id"], ["regulations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_article_id"], ["regulation_articles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_regulation_id"], ["regulations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_regulation_relations_source_article_id"), "regulation_relations", ["source_article_id"])
    op.create_index(op.f("ix_regulation_relations_source_regulation_id"), "regulation_relations", ["source_regulation_id"])
    op.create_index(op.f("ix_regulation_relations_target_article_id"), "regulation_relations", ["target_article_id"])
    op.create_index(op.f("ix_regulation_relations_target_regulation_id"), "regulation_relations", ["target_regulation_id"])
    op.create_index(op.f("ix_regulation_relations_relation_type"), "regulation_relations", ["relation_type"])


def downgrade() -> None:
    op.drop_table("regulation_relations")
    op.drop_table("regulation_article_versions")
    op.drop_table("regulation_articles")
    op.drop_table("regulations")
    # 扩展可能被其他对象使用，不在这里删除
