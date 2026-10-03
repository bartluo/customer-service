"""create planning_techniques table

Revision ID: 9c1d4b7e2f88
Revises: 7a3f2c8e5b41
Create Date: 2026-10-01

筹划手法库（技术方案 4.11）。每个手法六个字段，
其中"滥用边界"与"被否案例"是筹划能力真正的核心资产。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9c1d4b7e2f88"
down_revision = "7a3f2c8e5b41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planning_techniques",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column(
            "conditions", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column(
            "citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column("mechanism", sa.Text(), nullable=False, server_default=""),
        sa.Column("risk_level", sa.String(length=10), nullable=False, server_default="yellow"),
        sa.Column(
            "abuse_boundary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "rejected_cases",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("review_state", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("source", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_planning_techniques_code", "planning_techniques", ["code"], unique=True)
    op.create_index("ix_planning_techniques_category", "planning_techniques", ["category"])
    op.create_index("ix_planning_techniques_risk_level", "planning_techniques", ["risk_level"])
    op.create_index("ix_planning_techniques_review_state", "planning_techniques", ["review_state"])
    op.create_index(
        "ix_planning_techniques_category_risk", "planning_techniques", ["category", "risk_level"]
    )


def downgrade() -> None:
    op.drop_table("planning_techniques")
