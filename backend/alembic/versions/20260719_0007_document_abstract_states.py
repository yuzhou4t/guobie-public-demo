"""track bounded and resumable document abstract backfill

Revision ID: 20260719_0007
Revises: 20260718_0006
Create Date: 2026-07-19

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260719_0007"
down_revision: str | None = "20260718_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_abstract_states",
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_document_abstract_states_attempt_count_nonnegative")
        ),
        sa.CheckConstraint(
            "status in ('pending', 'stored', 'unavailable', 'retryable', 'blocked')",
            name=op.f("ck_document_abstract_states_status"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["source_channels.id"],
            name=op.f("fk_document_abstract_states_channel_id_source_channels"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_abstract_states_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", name=op.f("pk_document_abstract_states")),
    )
    op.create_index(
        op.f("ix_document_abstract_states_channel_id"),
        "document_abstract_states",
        ["channel_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_abstract_states_due",
        "document_abstract_states",
        ["status", "next_retry_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_abstract_states_due", table_name="document_abstract_states")
    op.drop_index(
        op.f("ix_document_abstract_states_channel_id"),
        table_name="document_abstract_states",
    )
    op.drop_table("document_abstract_states")
