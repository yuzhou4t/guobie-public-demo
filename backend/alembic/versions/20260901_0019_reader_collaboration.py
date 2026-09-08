"""add reader collaboration, access keys, and capability revisions

Revision ID: 20260901_0019
Revises: 20260830_0018
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260901_0019"
down_revision: str | None = "20260830_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reader_access_keys",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_sha256", sa.String(length=64), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status in ('active', 'revoked')", name=op.f("ck_reader_access_keys_status")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reader_access_keys")),
        sa.UniqueConstraint("key_sha256", name="uq_reader_access_keys_sha256"),
    )
    op.create_index(op.f("ix_reader_access_keys_key_prefix"), "reader_access_keys", ["key_prefix"])
    op.create_index(op.f("ix_reader_access_keys_user_id"), "reader_access_keys", ["user_id"])
    op.create_index("ix_reader_access_keys_user_status", "reader_access_keys", ["user_id", "status"])

    op.create_table(
        "research_case_members",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "role in ('owner', 'reviewer', 'editor', 'viewer')",
            name=op.f("ck_research_case_members_role"),
        ),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["research_case_id"], ["research_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_case_members")),
        sa.UniqueConstraint("research_case_id", "user_id", name="uq_research_case_members_user"),
    )
    op.create_index(op.f("ix_research_case_members_added_by"), "research_case_members", ["added_by"])
    op.create_index(
        op.f("ix_research_case_members_research_case_id"), "research_case_members", ["research_case_id"]
    )
    op.create_index(op.f("ix_research_case_members_user_id"), "research_case_members", ["user_id"])
    op.create_index(
        "ix_research_case_members_case_role", "research_case_members", ["research_case_id", "role"]
    )
    op.execute(
        """
        insert into research_case_members
            (research_case_id, user_id, role, added_by, created_at, updated_at)
        select id, owner_id, 'owner', owner_id, now(), now()
        from research_cases
        """
    )

    op.create_table(
        "research_contributions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("action_type", sa.String(length=48), nullable=False),
        sa.Column("object_type", sa.String(length=48), nullable=False),
        sa.Column("object_key", sa.String(length=200), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["research_case_id"], ["research_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_contributions")),
    )
    op.create_index(
        op.f("ix_research_contributions_research_case_id"), "research_contributions", ["research_case_id"]
    )
    op.create_index(op.f("ix_research_contributions_user_id"), "research_contributions", ["user_id"])
    op.create_index(
        "ix_research_contributions_case_created", "research_contributions", ["research_case_id", "created_at"]
    )

    op.add_column("capability_runs", sa.Column("requested_by", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_capability_runs_requested_by_users"),
        "capability_runs",
        "users",
        ["requested_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_capability_runs_requested_by"), "capability_runs", ["requested_by"])

    op.create_table(
        "capability_run_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("capability_run_id", sa.BigInteger(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column(
            "artifact",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["capability_run_id"], ["capability_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_run_revisions")),
        sa.UniqueConstraint("capability_run_id", "revision_no", name="uq_capability_run_revisions_number"),
    )
    op.create_index(
        op.f("ix_capability_run_revisions_capability_run_id"),
        "capability_run_revisions",
        ["capability_run_id"],
    )
    op.create_index(
        op.f("ix_capability_run_revisions_created_by"), "capability_run_revisions", ["created_by"]
    )
    op.create_index(
        "ix_capability_run_revisions_run_created",
        "capability_run_revisions",
        ["capability_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_capability_run_revisions_run_created", table_name="capability_run_revisions")
    op.drop_index(op.f("ix_capability_run_revisions_created_by"), table_name="capability_run_revisions")
    op.drop_index(
        op.f("ix_capability_run_revisions_capability_run_id"), table_name="capability_run_revisions"
    )
    op.drop_table("capability_run_revisions")
    op.drop_index(op.f("ix_capability_runs_requested_by"), table_name="capability_runs")
    op.drop_constraint(op.f("fk_capability_runs_requested_by_users"), "capability_runs", type_="foreignkey")
    op.drop_column("capability_runs", "requested_by")
    op.drop_index("ix_research_contributions_case_created", table_name="research_contributions")
    op.drop_index(op.f("ix_research_contributions_user_id"), table_name="research_contributions")
    op.drop_index(op.f("ix_research_contributions_research_case_id"), table_name="research_contributions")
    op.drop_table("research_contributions")
    op.drop_index("ix_research_case_members_case_role", table_name="research_case_members")
    op.drop_index(op.f("ix_research_case_members_user_id"), table_name="research_case_members")
    op.drop_index(op.f("ix_research_case_members_research_case_id"), table_name="research_case_members")
    op.drop_index(op.f("ix_research_case_members_added_by"), table_name="research_case_members")
    op.drop_table("research_case_members")
    op.drop_index("ix_reader_access_keys_user_status", table_name="reader_access_keys")
    op.drop_index(op.f("ix_reader_access_keys_user_id"), table_name="reader_access_keys")
    op.drop_index(op.f("ix_reader_access_keys_key_prefix"), table_name="reader_access_keys")
    op.drop_table("reader_access_keys")
