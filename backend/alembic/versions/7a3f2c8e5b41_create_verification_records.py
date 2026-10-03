"""create verification_records table

Revision ID: 7a3f2c8e5b41
Revises: 2b7c91d4e6a3
Create Date: 2026-10-01

验证记录表（技术方案 6.4）。只增不改：每次答案验证都写一条，
用于统计各验证器拦截率与失败原因分布。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "7a3f2c8e5b41"
down_revision = "2b7c91d4e6a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "verification_records",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "issue_codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_verification_records_outcome", "verification_records", ["outcome"])


def downgrade() -> None:
    op.drop_index("ix_verification_records_outcome", table_name="verification_records")
    op.drop_table("verification_records")
