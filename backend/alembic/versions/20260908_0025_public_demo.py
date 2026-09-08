"""Separate public Reader session and dataset marker tables; no demo data in internal DBs."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260908_0025"
down_revision = "20260907_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "public_demo_installation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sample_version", sa.String(80), nullable=False),
    )
    op.create_table(
        "public_demo_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("users.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("reader_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("provider_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("connection", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("key_expires_at", sa.BigInteger(), nullable=True),
        sa.Column("busy_until", sa.BigInteger(), nullable=False),
        sa.Column("quota_window", sa.BigInteger(), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
    )
    op.create_index("ix_public_demo_sessions_expires_at", "public_demo_sessions", ["expires_at"])


def downgrade():
    op.drop_table("public_demo_sessions")
    op.drop_table("public_demo_installation")
