"""add confirmed capability run targets

Revision ID: 20260830_0016
Revises: 20260828_0015
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0016"
down_revision: str | None = "20260828_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capability_run_targets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("capability_run_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.String(length=24), nullable=False),
        sa.Column("target_key", sa.String(length=200), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("artifact_snapshot", sa.JSON(), nullable=False),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "target_type in ('country', 'event', 'research_case', 'document_version')",
            name=op.f("ck_capability_run_targets_target_type"),
        ),
        sa.ForeignKeyConstraint(["capability_run_id"], ["capability_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_capability_run_targets_idempotency"),
        sa.UniqueConstraint(
            "capability_run_id",
            "target_type",
            "target_key",
            name="uq_capability_run_targets_target",
        ),
    )
    op.create_index(
        op.f("ix_capability_run_targets_capability_run_id"),
        "capability_run_targets",
        ["capability_run_id"],
    )
    op.create_index(
        op.f("ix_capability_run_targets_confirmed_by"),
        "capability_run_targets",
        ["confirmed_by"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_capability_run_targets_confirmed_by"),
        table_name="capability_run_targets",
    )
    op.drop_index(
        op.f("ix_capability_run_targets_capability_run_id"),
        table_name="capability_run_targets",
    )
    op.drop_table("capability_run_targets")
