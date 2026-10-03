"""add playbook and measure columns to planning_techniques

Revision ID: 3f6a9c2d5e10
Revises: 9c1d4b7e2f88
Create Date: 2026-10-01

方案落地与测算所需的两列：
  · playbook —— 动作 / 步骤 / 所需材料 / 实施成本
  · measure  —— 结构化的节税效果，交给计算引擎算（不允许模型估金额）
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "3f6a9c2d5e10"
down_revision = "9c1d4b7e2f88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "planning_techniques",
        sa.Column(
            "playbook",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "planning_techniques",
        sa.Column(
            "measure",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("planning_techniques", "measure")
    op.drop_column("planning_techniques", "playbook")
