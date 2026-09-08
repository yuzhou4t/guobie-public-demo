"""Persist project briefs, task plans and evidence adoption.

Revision ID: 20260905_0021
Revises: 20260902_0020
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20260905_0021"
down_revision = "20260902_0020"
branch_labels = None
depends_on = None


def timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade():
    op.add_column("research_cases", sa.Column("brief_original", sa.Text(), nullable=False, server_default=""))
    op.add_column(
        "research_cases", sa.Column("brief_confirmed", JSONB(), nullable=False, server_default="{}")
    )
    op.add_column(
        "research_cases", sa.Column("brief_revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.create_table(
        "project_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "research_case_id",
            sa.BigInteger(),
            sa.ForeignKey("research_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="planning"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("messages", JSONB(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text()),
        *timestamps(),
        sa.CheckConstraint(
            "status in ('planning','awaiting_confirmation','running','review',"
            "'completed','failed','cancelled')",
            name="status",
        ),
    )
    op.create_index("ix_project_tasks_research_case_id", "project_tasks", ["research_case_id"])
    op.create_table(
        "project_plans",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id", sa.String(36), sa.ForeignKey("project_tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", JSONB(), nullable=False, server_default="{}"),
        sa.Column("confirmed_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("execution_key", sa.String(64), unique=True),
        *timestamps(),
        sa.UniqueConstraint("task_id", "revision", name="uq_project_plans_revision"),
    )
    op.create_index("ix_project_plans_task_id", "project_plans", ["task_id"])
    op.create_table(
        "project_evidence",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "research_case_id",
            sa.BigInteger(),
            sa.ForeignKey("research_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("project_tasks.id", ondelete="SET NULL")),
        sa.Column("object_type", sa.String(48), nullable=False),
        sa.Column("object_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False, server_default="{}"),
        sa.Column("decision", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("reviewed_by", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="RESTRICT")),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint(
            "research_case_id", "object_type", "object_id", name="uq_project_evidence_object"
        ),
        sa.CheckConstraint("decision in ('pending','accepted','rejected','needs_revision')", name="decision"),
    )
    op.create_index("ix_project_evidence_research_case_id", "project_evidence", ["research_case_id"])
    op.create_index("ix_project_evidence_task_id", "project_evidence", ["task_id"])
    # Old links remain implicit adoption. Never invent historical reviews or plans.


def downgrade():
    op.drop_table("project_evidence")
    op.drop_table("project_plans")
    op.drop_table("project_tasks")
    op.drop_column("research_cases", "brief_revision")
    op.drop_column("research_cases", "brief_confirmed")
    op.drop_column("research_cases", "brief_original")
