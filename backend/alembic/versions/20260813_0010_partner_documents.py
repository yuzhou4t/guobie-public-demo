"""add exact partner document releases

Revision ID: 20260813_0010
Revises: 20260813_0009
Create Date: 2026-08-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260813_0010"
down_revision: str | None = "20260813_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "partner_document_releases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("abstract_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "abstract_count >= 0",
            name=op.f("ck_partner_document_releases_abstract_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "item_count >= 0",
            name=op.f("ck_partner_document_releases_item_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "length(manifest_sha256) = 64",
            name=op.f("ck_partner_document_releases_manifest_sha256_length"),
        ),
        sa.CheckConstraint(
            "status in ('published', 'withdrawn')",
            name=op.f("ck_partner_document_releases_status"),
        ),
        sa.CheckConstraint(
            "(status = 'published' and withdrawn_at is null) "
            "or (status = 'withdrawn' and withdrawn_at is not null)",
            name=op.f("ck_partner_document_releases_withdrawn_state"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_document_releases")),
        sa.UniqueConstraint(
            "manifest_sha256",
            name=op.f("uq_partner_document_releases_manifest_sha256"),
        ),
    )
    op.create_index(
        "uq_partner_document_releases_current",
        "partner_document_releases",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
        sqlite_where=sa.text("status = 'published'"),
    )
    op.create_table(
        "partner_document_release_items",
        sa.Column("release_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("document_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("document_version_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_partner_document_release_items_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_partner_document_release_items_document_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["partner_document_releases.id"],
            name=op.f("fk_partner_document_release_items_release_id_partner_document_releases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "release_id",
            "document_id",
            name=op.f("pk_partner_document_release_items"),
        ),
        sa.UniqueConstraint(
            "release_id",
            "document_version_id",
            name="uq_partner_document_release_items_version",
        ),
    )
    op.create_index(
        "ix_partner_document_release_items_document",
        "partner_document_release_items",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_partner_document_release_items_document",
        table_name="partner_document_release_items",
    )
    op.drop_table("partner_document_release_items")
    op.drop_index(
        "uq_partner_document_releases_current",
        table_name="partner_document_releases",
    )
    op.drop_table("partner_document_releases")
