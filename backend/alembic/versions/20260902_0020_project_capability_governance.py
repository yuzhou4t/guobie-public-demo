"""add project capability governance and frozen exports

Revision ID: 20260902_0020
Revises: 20260901_0019
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260902_0020"
down_revision: str | None = "20260901_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("capability_templates", sa.Column("catalog_key", sa.String(length=160)))
    op.add_column(
        "capability_templates",
        sa.Column("category", sa.String(length=32), server_default="research_workflow", nullable=False),
    )
    op.add_column("capability_templates", sa.Column("owner_id", sa.BigInteger()))
    op.add_column(
        "capability_templates",
        sa.Column("visibility", sa.String(length=16), server_default="internal", nullable=False),
    )
    op.add_column(
        "capability_templates",
        sa.Column("validation_status", sa.String(length=16), server_default="pending", nullable=False),
    )
    op.add_column(
        "capability_templates",
        sa.Column(
            "execution_plan",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_capability_templates_catalog_key", "capability_templates", ["catalog_key"]
    )
    op.create_index(op.f("ix_capability_templates_catalog_key"), "capability_templates", ["catalog_key"])
    op.create_index(op.f("ix_capability_templates_owner_id"), "capability_templates", ["owner_id"])
    op.create_foreign_key(
        op.f("fk_capability_templates_owner_id_users"),
        "capability_templates",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_capability_templates_category"),
        "capability_templates",
        "category in ('research_workflow', 'data_collection', 'field_collaboration')",
    )
    op.create_check_constraint(
        op.f("ck_capability_templates_visibility"),
        "capability_templates",
        "visibility in ('public', 'internal', 'private')",
    )
    op.create_check_constraint(
        op.f("ck_capability_templates_validation_status"),
        "capability_templates",
        "validation_status in ('pending', 'verified', 'failed')",
    )

    op.drop_constraint("uq_capability_configs_owner_name", "capability_configs", type_="unique")
    op.drop_constraint(op.f("ck_capability_configs_status"), "capability_configs", type_="check")
    op.add_column(
        "capability_configs",
        sa.Column("scope_type", sa.String(length=16), server_default="personal", nullable=False),
    )
    op.add_column("capability_configs", sa.Column("research_case_id", sa.BigInteger()))
    op.create_index(
        op.f("ix_capability_configs_research_case_id"), "capability_configs", ["research_case_id"]
    )
    op.create_foreign_key(
        op.f("fk_capability_configs_research_case_id_research_cases"),
        "capability_configs",
        "research_cases",
        ["research_case_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_capability_configs_scope_type"),
        "capability_configs",
        "scope_type in ('personal', 'research_case')",
    )
    op.create_check_constraint(
        op.f("ck_capability_configs_status"),
        "capability_configs",
        "status in ('draft', 'active', 'pending_review', 'published', "
        "'paused', 'expired', 'archived', 'disabled')",
    )
    op.create_index(
        "uq_capability_configs_personal_name",
        "capability_configs",
        ["owner_id", "name"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'personal'"),
    )
    op.create_index(
        "uq_capability_configs_case_name",
        "capability_configs",
        ["research_case_id", "name"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'research_case'"),
    )

    op.create_table(
        "capability_config_revisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("config_id", sa.BigInteger(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "config_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "template_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["capability_configs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_config_revisions")),
        sa.UniqueConstraint("config_id", "revision_no", name="uq_capability_config_revisions_number"),
    )
    op.create_index(
        op.f("ix_capability_config_revisions_config_id"), "capability_config_revisions", ["config_id"]
    )
    op.create_index(
        op.f("ix_capability_config_revisions_created_by"), "capability_config_revisions", ["created_by"]
    )
    op.create_index(
        "ix_capability_config_revisions_config_created",
        "capability_config_revisions",
        ["config_id", "created_at"],
    )
    op.execute(
        """
        insert into capability_config_revisions
            (config_id, revision_no, name, config_snapshot, template_snapshot, created_by, created_at)
        select c.id, 1, c.name, c.config,
               jsonb_build_object('template_id', t.id, 'slug', t.slug, 'version', t.version,
                                  'name', t.name, 'input_schema', t.input_schema,
                                  'output_schema', t.output_schema, 'default_config', t.default_config),
               c.owner_id, c.created_at
        from capability_configs c
        join capability_templates t on t.id = c.template_id
        """
    )

    op.create_table(
        "capability_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("review_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "checklist",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.BigInteger()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "review_type in ('validation', 'team_publish', 'public_publish', 'run')",
            name=op.f("ck_capability_reviews_review_type"),
        ),
        sa.CheckConstraint(
            "status in ('pending', 'approved', 'rejected')", name=op.f("ck_capability_reviews_status")
        ),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_reviews")),
    )
    op.create_index(op.f("ix_capability_reviews_reviewed_by"), "capability_reviews", ["reviewed_by"])
    op.create_index(
        "ix_capability_reviews_target", "capability_reviews", ["target_type", "target_id", "created_at"]
    )

    op.create_table(
        "capability_audit_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_id", sa.BigInteger()),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_audit_events")),
    )
    op.create_index(op.f("ix_capability_audit_events_actor_id"), "capability_audit_events", ["actor_id"])
    op.create_index(
        "ix_capability_audit_events_target_created",
        "capability_audit_events",
        ["target_type", "target_id", "created_at"],
    )

    op.create_table(
        "capability_schedules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("config_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("cadence", sa.String(length=80), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status in ('active', 'paused', 'archived')", name=op.f("ck_capability_schedules_status")
        ),
        sa.ForeignKeyConstraint(["config_id"], ["capability_configs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_schedules")),
        sa.UniqueConstraint("idempotency_key", name="uq_capability_schedules_idempotency"),
    )
    op.create_index(op.f("ix_capability_schedules_config_id"), "capability_schedules", ["config_id"])
    op.create_index(op.f("ix_capability_schedules_owner_id"), "capability_schedules", ["owner_id"])
    op.create_index(op.f("ix_capability_schedules_next_run_at"), "capability_schedules", ["next_run_at"])

    op.add_column("capability_runs", sa.Column("config_revision_id", sa.BigInteger()))
    op.add_column("capability_runs", sa.Column("schedule_id", sa.BigInteger()))
    op.add_column(
        "capability_runs",
        sa.Column("review_status", sa.String(length=16), server_default="pending", nullable=False),
    )
    op.add_column("capability_runs", sa.Column("reviewed_by", sa.BigInteger()))
    op.add_column("capability_runs", sa.Column("reviewed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "capability_runs",
        sa.Column(
            "execution_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    for column, table, ondelete in (
        ("config_revision_id", "capability_config_revisions", "RESTRICT"),
        ("schedule_id", "capability_schedules", "SET NULL"),
        ("reviewed_by", "users", "SET NULL"),
    ):
        op.create_index(op.f(f"ix_capability_runs_{column}"), "capability_runs", [column])
        op.create_foreign_key(
            op.f(f"fk_capability_runs_{column}_{table}"),
            "capability_runs",
            table,
            [column],
            ["id"],
            ondelete=ondelete,
        )

    op.add_column("agent_runs", sa.Column("requested_by", sa.BigInteger()))
    op.create_index(op.f("ix_agent_runs_requested_by"), "agent_runs", ["requested_by"])
    op.create_foreign_key(
        op.f("fk_agent_runs_requested_by_users"),
        "agent_runs",
        "users",
        ["requested_by"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "research_exports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=False),
        sa.Column("capability_run_target_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_by", sa.BigInteger(), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column(
            "frozen_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "manifest_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "rights_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "artifact",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("format in ('markdown', 'json', 'csv')", name=op.f("ck_research_exports_format")),
        sa.ForeignKeyConstraint(
            ["capability_run_target_id"], ["capability_run_targets.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["research_case_id"], ["research_cases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_exports")),
        sa.UniqueConstraint("idempotency_key", name="uq_research_exports_idempotency"),
    )
    op.create_index(op.f("ix_research_exports_research_case_id"), "research_exports", ["research_case_id"])
    op.create_index(
        op.f("ix_research_exports_capability_run_target_id"), "research_exports", ["capability_run_target_id"]
    )
    op.create_index(op.f("ix_research_exports_requested_by"), "research_exports", ["requested_by"])
    op.create_index(
        "ix_research_exports_case_created", "research_exports", ["research_case_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_research_exports_case_created", table_name="research_exports")
    op.drop_index(op.f("ix_research_exports_requested_by"), table_name="research_exports")
    op.drop_index(op.f("ix_research_exports_capability_run_target_id"), table_name="research_exports")
    op.drop_index(op.f("ix_research_exports_research_case_id"), table_name="research_exports")
    op.drop_table("research_exports")
    op.drop_constraint(op.f("fk_agent_runs_requested_by_users"), "agent_runs", type_="foreignkey")
    op.drop_index(op.f("ix_agent_runs_requested_by"), table_name="agent_runs")
    op.drop_column("agent_runs", "requested_by")
    for column, table in (
        ("reviewed_by", "users"),
        ("schedule_id", "capability_schedules"),
        ("config_revision_id", "capability_config_revisions"),
    ):
        op.drop_constraint(
            op.f(f"fk_capability_runs_{column}_{table}"), "capability_runs", type_="foreignkey"
        )
        op.drop_index(op.f(f"ix_capability_runs_{column}"), table_name="capability_runs")
    for column in (
        "execution_summary",
        "reviewed_at",
        "reviewed_by",
        "review_status",
        "schedule_id",
        "config_revision_id",
    ):
        op.drop_column("capability_runs", column)
    op.drop_index(op.f("ix_capability_schedules_next_run_at"), table_name="capability_schedules")
    op.drop_index(op.f("ix_capability_schedules_owner_id"), table_name="capability_schedules")
    op.drop_index(op.f("ix_capability_schedules_config_id"), table_name="capability_schedules")
    op.drop_table("capability_schedules")
    op.drop_index("ix_capability_audit_events_target_created", table_name="capability_audit_events")
    op.drop_index(op.f("ix_capability_audit_events_actor_id"), table_name="capability_audit_events")
    op.drop_table("capability_audit_events")
    op.drop_index("ix_capability_reviews_target", table_name="capability_reviews")
    op.drop_index(op.f("ix_capability_reviews_reviewed_by"), table_name="capability_reviews")
    op.drop_table("capability_reviews")
    op.drop_index("ix_capability_config_revisions_config_created", table_name="capability_config_revisions")
    op.drop_index(op.f("ix_capability_config_revisions_created_by"), table_name="capability_config_revisions")
    op.drop_index(op.f("ix_capability_config_revisions_config_id"), table_name="capability_config_revisions")
    op.drop_table("capability_config_revisions")
    op.drop_index("uq_capability_configs_case_name", table_name="capability_configs")
    op.drop_index("uq_capability_configs_personal_name", table_name="capability_configs")
    op.drop_constraint(op.f("ck_capability_configs_status"), "capability_configs", type_="check")
    op.drop_constraint(op.f("ck_capability_configs_scope_type"), "capability_configs", type_="check")
    op.drop_constraint(
        op.f("fk_capability_configs_research_case_id_research_cases"),
        "capability_configs",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_capability_configs_research_case_id"), table_name="capability_configs")
    op.drop_column("capability_configs", "research_case_id")
    op.drop_column("capability_configs", "scope_type")
    op.create_unique_constraint(
        "uq_capability_configs_owner_name", "capability_configs", ["owner_id", "name"]
    )
    op.create_check_constraint(
        op.f("ck_capability_configs_status"), "capability_configs", "status in ('active', 'disabled')"
    )
    op.drop_constraint(
        op.f("ck_capability_templates_validation_status"), "capability_templates", type_="check"
    )
    op.drop_constraint(op.f("ck_capability_templates_visibility"), "capability_templates", type_="check")
    op.drop_constraint(op.f("ck_capability_templates_category"), "capability_templates", type_="check")
    op.drop_constraint(
        op.f("fk_capability_templates_owner_id_users"), "capability_templates", type_="foreignkey"
    )
    op.drop_index(op.f("ix_capability_templates_owner_id"), table_name="capability_templates")
    op.drop_index(op.f("ix_capability_templates_catalog_key"), table_name="capability_templates")
    op.drop_constraint("uq_capability_templates_catalog_key", "capability_templates", type_="unique")
    for column in (
        "execution_plan",
        "validation_status",
        "visibility",
        "owner_id",
        "category",
        "catalog_key",
    ):
        op.drop_column("capability_templates", column)
