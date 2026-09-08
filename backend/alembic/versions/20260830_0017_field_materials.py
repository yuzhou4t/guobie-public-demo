"""add user-provided field materials

Revision ID: 20260830_0017
Revises: 20260830_0016
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260830_0017"
down_revision: str | None = "20260830_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "field_materials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=True),
        sa.Column("country_iso3", sa.String(length=3), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=80), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("material_type", sa.String(length=32), nullable=False),
        sa.Column("privacy_level", sa.String(length=24), nullable=False),
        sa.Column("evidence_status", sa.String(length=32), nullable=False),
        sa.Column(
            "authorization_confirmed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("captured_on", sa.Date(), nullable=True),
        sa.Column("method_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("byte_size > 0", name=op.f("ck_field_materials_byte_size_positive")),
        sa.CheckConstraint(
            "country_iso3 is not null or research_case_id is not null",
            name=op.f("ck_field_materials_scope_required"),
        ),
        sa.CheckConstraint(
            "evidence_status = 'user_provided_unreviewed'",
            name=op.f("ck_field_materials_evidence_status"),
        ),
        sa.CheckConstraint(
            "material_type in ('field_note', 'interview_transcript', 'photo', 'supporting_document')",
            name=op.f("ck_field_materials_material_type"),
        ),
        sa.CheckConstraint(
            "privacy_level in ('restricted', 'anonymized', 'shareable')",
            name=op.f("ck_field_materials_privacy_level"),
        ),
        sa.CheckConstraint("length(sha256) = 64", name=op.f("ck_field_materials_sha256_length")),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["research_case_id"],
            ["research_cases.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_field_materials_case_created",
        "field_materials",
        ["research_case_id", "created_at"],
    )
    op.create_index(
        "ix_field_materials_country_created",
        "field_materials",
        ["country_iso3", "created_at"],
    )
    op.create_index(op.f("ix_field_materials_country_iso3"), "field_materials", ["country_iso3"])
    op.create_index(op.f("ix_field_materials_owner_id"), "field_materials", ["owner_id"])
    op.create_index(
        op.f("ix_field_materials_research_case_id"),
        "field_materials",
        ["research_case_id"],
    )
    op.create_index(op.f("ix_field_materials_sha256"), "field_materials", ["sha256"])


def downgrade() -> None:
    op.drop_index(op.f("ix_field_materials_sha256"), table_name="field_materials")
    op.drop_index(op.f("ix_field_materials_research_case_id"), table_name="field_materials")
    op.drop_index(op.f("ix_field_materials_owner_id"), table_name="field_materials")
    op.drop_index(op.f("ix_field_materials_country_iso3"), table_name="field_materials")
    op.drop_index("ix_field_materials_country_created", table_name="field_materials")
    op.drop_index("ix_field_materials_case_created", table_name="field_materials")
    op.drop_table("field_materials")
