"""add partner API clients and exact snapshot publications

Revision ID: 20260813_0009
Revises: 20260727_0008
Create Date: 2026-08-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260813_0009"
down_revision: str | None = "20260727_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "partner_api_clients",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "length(key_prefix) = 16",
            name=op.f("ck_partner_api_clients_key_prefix_length"),
        ),
        sa.CheckConstraint(
            "length(key_sha256) = 64",
            name=op.f("ck_partner_api_clients_key_sha256_length"),
        ),
        sa.CheckConstraint(
            "status in ('active', 'revoked')",
            name=op.f("ck_partner_api_clients_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_api_clients")),
        sa.UniqueConstraint("key_prefix", name=op.f("uq_partner_api_clients_key_prefix")),
        sa.UniqueConstraint("key_sha256", name=op.f("uq_partner_api_clients_key_sha256")),
        sa.UniqueConstraint("name", name=op.f("uq_partner_api_clients_name")),
    )
    op.create_table(
        "structured_dataset_publications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status in ('published', 'withdrawn')",
            name=op.f("ck_structured_dataset_publications_status"),
        ),
        sa.CheckConstraint(
            "(status = 'published' and withdrawn_at is null) "
            "or (status = 'withdrawn' and withdrawn_at is not null)",
            name=op.f("ck_structured_dataset_publications_withdrawn_state"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["structured_datasets.id"],
            name=op.f("fk_structured_dataset_publications_dataset_id_structured_datasets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["structured_snapshots.id"],
            name=op.f("fk_structured_dataset_publications_snapshot_id_structured_snapshots"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_structured_dataset_publications")),
        sa.UniqueConstraint(
            "dataset_id",
            "snapshot_id",
            name="uq_dataset_publications_snapshot",
        ),
    )
    op.create_index(
        "uq_dataset_publications_current",
        "structured_dataset_publications",
        ["dataset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_dataset_publications_current",
        table_name="structured_dataset_publications",
    )
    op.drop_table("structured_dataset_publications")
    op.drop_table("partner_api_clients")
