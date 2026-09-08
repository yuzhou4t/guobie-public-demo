"""add auditable agent runs

Revision ID: 20260826_0012
Revises: 20260821_0011
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260826_0012"
down_revision: str | None = "20260821_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("country_iso3", sa.String(length=3), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("workflow", sa.String(length=24), nullable=False),
        sa.Column("runtime", sa.String(length=24), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("tool_trace", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "workflow in ('evidence_synthesis', 'bounded_agent')",
            name=op.f("ck_agent_runs_workflow"),
        ),
        sa.CheckConstraint(
            "runtime in ('evidence_only', 'codex_local', 'coze_test', 'openai_responses')",
            name=op.f("ck_agent_runs_runtime"),
        ),
        sa.CheckConstraint(
            "status in ('running', 'succeeded', 'failed')",
            name=op.f("ck_agent_runs_status"),
        ),
        sa.CheckConstraint(
            "finished_at is null or finished_at >= started_at",
            name=op.f("ck_agent_runs_valid_time_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )
    op.create_index(op.f("ix_agent_runs_country_iso3"), "agent_runs", ["country_iso3"])
    op.create_index(
        "ix_agent_runs_country_created",
        "agent_runs",
        ["country_iso3", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_country_created", table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_country_iso3"), table_name="agent_runs")
    op.drop_table("agent_runs")
