"""create evolution_events and knowledge_gaps tables

Revision ID: 8d2c5e7b1a94
Revises: 5b8e1f4a7c23
Create Date: 2026-10-01

自进化（技术方案第 8 章）：
  · evolution_events —— 法规变更的全过程与时间戳，G8 的"≤24 小时"靠它度量
  · knowledge_gaps   —— 未命中/低置信问题聚类出的缺口清单
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "8d2c5e7b1a94"
down_revision = "5b8e1f4a7c23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evolution_events",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="discovered"),
        sa.Column("title", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("document_number", sa.String(length=200), nullable=True),
        sa.Column("source_url", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("impact", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_evolution_events_event_type", "evolution_events", ["event_type"])
    op.create_index("ix_evolution_events_status", "evolution_events", ["status"])

    op.create_table(
        "knowledge_gaps",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("topic", sa.String(length=120), nullable=False),
        sa.Column("tax_type", sa.String(length=40), nullable=True),
        sa.Column("sample_questions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reason", sa.String(length=40), nullable=False, server_default="no_hit"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_gaps_topic", "knowledge_gaps", ["topic"])
    op.create_index("ix_knowledge_gaps_tax_type", "knowledge_gaps", ["tax_type"])
    op.create_index("ix_knowledge_gaps_status", "knowledge_gaps", ["status"])
    op.create_index("ix_knowledge_gaps_topic_reason", "knowledge_gaps", ["topic", "reason"])


def downgrade() -> None:
    op.drop_table("knowledge_gaps")
    op.drop_table("evolution_events")
