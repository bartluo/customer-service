"""create eval_cases, eval_runs, gate_decisions tables

Revision ID: 1e7b3d9f6c48
Revises: 8d2c5e7b1a94
Create Date: 2026-10-01

评测与门禁（技术方案第 11 章）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "1e7b3d9f6c48"
down_revision = "8d2c5e7b1a94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_cases",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("domain_id", sa.String(length=64), nullable=False, server_default="finance_tax"),
        sa.Column("case_type", sa.String(length=30), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected_citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("forbidden_citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("expected_behavior", sa.String(length=20), nullable=False, server_default="answer"),
        sa.Column("tax_type", sa.String(length=40), nullable=True),
        sa.Column("difficulty", sa.String(length=10), nullable=False, server_default="medium"),
        sa.Column("expected_points", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("review_state", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("source", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_eval_cases_domain_id", "eval_cases", ["domain_id"])
    op.create_index("ix_eval_cases_case_type", "eval_cases", ["case_type"])
    op.create_index("ix_eval_cases_tax_type", "eval_cases", ["tax_type"])
    op.create_index("ix_eval_cases_review_state", "eval_cases", ["review_state"])
    op.create_index("ix_eval_cases_type_state", "eval_cases", ["case_type", "review_state"])

    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("gate", sa.String(length=20), nullable=False, server_default="offline"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("total_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_eval_runs_gate", "eval_runs", ["gate"])
    op.create_index("ix_eval_runs_status", "eval_runs", ["status"])

    op.create_table(
        "gate_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("gate", sa.String(length=20), nullable=False, server_default="offline"),
        sa.Column("eval_run_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("veto_items", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"),
        sa.Column("release_ref", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rolled_back", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_gate_decisions_gate", "gate_decisions", ["gate"])
    op.create_index("ix_gate_decisions_decision", "gate_decisions", ["decision"])


def downgrade() -> None:
    op.drop_table("gate_decisions")
    op.drop_table("eval_runs")
    op.drop_table("eval_cases")
