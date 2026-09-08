"""align capability run artifact snapshots with PostgreSQL JSONB

Revision ID: 20260830_0018
Revises: 20260830_0017
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260830_0018"
down_revision: str | None = "20260830_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "capability_run_targets",
        "artifact_snapshot",
        existing_type=sa.JSON(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
        postgresql_using="artifact_snapshot::jsonb",
    )


def downgrade() -> None:
    op.alter_column(
        "capability_run_targets",
        "artifact_snapshot",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.JSON(),
        existing_nullable=False,
        postgresql_using="artifact_snapshot::json",
    )
