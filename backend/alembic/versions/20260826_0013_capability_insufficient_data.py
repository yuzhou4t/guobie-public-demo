"""distinguish capability runs with insufficient evidence

Revision ID: 20260826_0013
Revises: 20260826_0012
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260826_0013"
down_revision: str | None = "20260826_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_capability_runs_status"),
        "capability_runs",
        type_="check",
    )
    op.alter_column(
        "capability_runs",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=24),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_capability_runs_status"),
        "capability_runs",
        "status in ('queued', 'running', 'succeeded', 'insufficient_data', 'failed')",
    )


def downgrade() -> None:
    op.execute(
        "update capability_runs set status = 'failed', "
        "error_code = coalesce(error_code, 'insufficient_data') "
        "where status = 'insufficient_data'"
    )
    op.drop_constraint(
        op.f("ck_capability_runs_status"),
        "capability_runs",
        type_="check",
    )
    op.alter_column(
        "capability_runs",
        "status",
        existing_type=sa.String(length=24),
        type_=sa.String(length=16),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_capability_runs_status"),
        "capability_runs",
        "status in ('queued', 'running', 'succeeded', 'failed')",
    )
