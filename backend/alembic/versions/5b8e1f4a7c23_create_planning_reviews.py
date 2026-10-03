"""create planning_reviews table

Revision ID: 5b8e1f4a7c23
Revises: 3f6a9c2d5e10
Create Date: 2026-10-01

专家复核台（技术方案 4.13）。🔴 高风险方案只进这张表、不发给用户，
`delivered_to_user` 字段就是这条规则的落库凭证。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "5b8e1f4a7c23"
down_revision = "3f6a9c2d5e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planning_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("profile", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("technique_code", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("risk_level", sa.String(length=10), nullable=False, server_default="red"),
        sa.Column("delivered_to_user", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("reviewer", sa.String(length=80), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_planning_reviews_technique_code", "planning_reviews", ["technique_code"])
    op.create_index("ix_planning_reviews_risk_level", "planning_reviews", ["risk_level"])
    op.create_index("ix_planning_reviews_status", "planning_reviews", ["status"])


def downgrade() -> None:
    op.drop_table("planning_reviews")
