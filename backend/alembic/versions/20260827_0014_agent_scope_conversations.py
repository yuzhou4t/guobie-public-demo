"""add scoped assistant conversations

Revision ID: 20260827_0014
Revises: 20260826_0013
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260827_0014"
down_revision: str | None = "20260826_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("conversation_id", sa.String(length=36), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("scope_type", sa.String(length=24), server_default="country", nullable=False),
    )
    op.add_column("agent_runs", sa.Column("scope_key", sa.String(length=160), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column(
            "scope_context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.execute("update agent_runs set scope_key = country_iso3 where scope_key is null")
    op.alter_column("agent_runs", "scope_key", existing_type=sa.String(length=160), nullable=False)
    op.create_index(op.f("ix_agent_runs_conversation_id"), "agent_runs", ["conversation_id"])
    op.create_index(
        "ix_agent_runs_conversation_scope",
        "agent_runs",
        ["conversation_id", "scope_type", "scope_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_conversation_scope", table_name="agent_runs")
    op.drop_index(op.f("ix_agent_runs_conversation_id"), table_name="agent_runs")
    op.drop_column("agent_runs", "scope_context")
    op.drop_column("agent_runs", "scope_key")
    op.drop_column("agent_runs", "scope_type")
    op.drop_column("agent_runs", "conversation_id")
