"""add research evidence, events, claims, and declarative capabilities

Revision ID: 20260821_0011
Revises: 20260813_0010
Create Date: 2026-08-21

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260821_0011"
down_revision: str | None = "20260813_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=24), nullable=False),
        sa.Column("canonical_key", sa.String(length=160), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "entity_type in ('country', 'place', 'organization', 'person', 'armed_group', "
            "'commodity', 'policy')",
            name=op.f("ck_entities_entity_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
        sa.UniqueConstraint("entity_type", "canonical_key", name="uq_entities_type_key"),
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("status in ('active', 'retired')", name=op.f("ck_topics_status")),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["topics.id"], name=op.f("fk_topics_parent_id_topics"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_topics")),
        sa.UniqueConstraint("slug", name=op.f("uq_topics_slug")),
    )
    op.create_index(op.f("ix_topics_parent_id"), "topics", ["parent_id"], unique=False)
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("series_key", sa.String(length=200), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("country_entity_id", sa.BigInteger(), nullable=False),
        sa.Column("primary_place_entity_id", sa.BigInteger(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_precision", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "event_type in ('policy', 'conflict', 'market', 'accident', 'other')",
            name=op.f("ck_events_event_type"),
        ),
        sa.CheckConstraint(
            "date_precision in ('day', 'month', 'year', 'unknown')",
            name=op.f("ck_events_date_precision"),
        ),
        sa.CheckConstraint(
            "review_status in ('draft', 'reviewed')",
            name=op.f("ck_events_review_status"),
        ),
        sa.CheckConstraint(
            "end_at is null or start_at is null or end_at >= start_at",
            name=op.f("ck_events_valid_time_range"),
        ),
        sa.ForeignKeyConstraint(
            ["country_entity_id"],
            ["entities.id"],
            name=op.f("fk_events_country_entity_id_entities"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["primary_place_entity_id"],
            ["entities.id"],
            name=op.f("fk_events_primary_place_entity_id_entities"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint("event_key", name=op.f("uq_events_event_key")),
    )
    op.create_index(op.f("ix_events_country_entity_id"), "events", ["country_entity_id"], unique=False)
    op.create_index(
        op.f("ix_events_primary_place_entity_id"),
        "events",
        ["primary_place_entity_id"],
        unique=False,
    )
    op.create_index("ix_events_country_start", "events", ["country_entity_id", "start_at"], unique=False)
    op.create_index("ix_events_series_start", "events", ["series_key", "start_at"], unique=False)
    op.create_table(
        "document_entities",
        sa.Column("document_version_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("entity_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("extraction_method", sa.String(length=16), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role in ('about', 'actor', 'location', 'commodity', 'policy', 'affected')",
            name=op.f("ck_document_entities_role"),
        ),
        sa.CheckConstraint(
            "extraction_method in ('manual', 'imported', 'rule')",
            name=op.f("ck_document_entities_extraction_method"),
        ),
        sa.CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name=op.f("ck_document_entities_review_status"),
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_entities_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_document_entities_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "document_version_id", "entity_id", "role", name=op.f("pk_document_entities")
        ),
    )
    op.create_index(
        "ix_document_entities_entity_role", "document_entities", ["entity_id", "role"], unique=False
    )
    op.create_table(
        "document_topics",
        sa.Column("document_version_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("topic_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name=op.f("ck_document_topics_review_status"),
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_topics_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["topic_id"],
            ["topics.id"],
            name=op.f("fk_document_topics_topic_id_topics"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_version_id", "topic_id", name=op.f("pk_document_topics")),
    )
    op.create_table(
        "event_entities",
        sa.Column("event_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("entity_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "role in ('actor', 'location', 'commodity', 'policy', 'affected_country')",
            name=op.f("ck_event_entities_role"),
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_event_entities_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_entities_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", "entity_id", "role", name=op.f("pk_event_entities")),
    )
    op.create_table(
        "event_mentions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("document_version_id", sa.BigInteger(), nullable=False),
        sa.Column("source_reported_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_reported_end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_reported_place", sa.Text(), nullable=True),
        sa.Column("mention_summary", sa.Text(), nullable=False),
        sa.Column("evidence_locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("match_score", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("match_reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("aggregation_version", sa.Text(), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name=op.f("ck_event_mentions_review_status"),
        ),
        sa.CheckConstraint(
            "match_score is null or (match_score >= 0 and match_score <= 1)",
            name=op.f("ck_event_mentions_match_score_range"),
        ),
        sa.CheckConstraint(
            "source_reported_end_at is null or source_reported_start_at is null "
            "or source_reported_end_at >= source_reported_start_at",
            name=op.f("ck_event_mentions_valid_time_range"),
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_event_mentions_document_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_event_mentions_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_mentions")),
        sa.UniqueConstraint("event_id", "document_version_id", name="uq_event_mentions_version"),
    )
    op.create_index(op.f("ix_event_mentions_event_id"), "event_mentions", ["event_id"], unique=False)
    op.create_index(
        op.f("ix_event_mentions_document_version_id"),
        "event_mentions",
        ["document_version_id"],
        unique=False,
    )
    op.create_index("ix_event_mentions_review", "event_mentions", ["review_status", "event_id"], unique=False)
    op.create_table(
        "claims",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_mention_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_key", sa.String(length=64), nullable=False),
        sa.Column("claim_kind", sa.String(length=16), nullable=False),
        sa.Column("claimant_entity_id", sa.BigInteger(), nullable=True),
        sa.Column("claimant_name", sa.Text(), nullable=False),
        sa.Column("subject_text", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("value_text", sa.Text(), nullable=False),
        sa.Column("numeric_value", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("time_scope", sa.Text(), nullable=True),
        sa.Column("comparison_key", sa.String(length=240), nullable=False),
        sa.Column("position_summary", sa.Text(), nullable=True),
        sa.Column("evidence_locator", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "claim_kind in ('factual', 'numeric', 'position')",
            name=op.f("ck_claims_claim_kind"),
        ),
        sa.CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name=op.f("ck_claims_review_status"),
        ),
        sa.ForeignKeyConstraint(
            ["claimant_entity_id"],
            ["entities.id"],
            name=op.f("fk_claims_claimant_entity_id_entities"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_mention_id"],
            ["event_mentions.id"],
            name=op.f("fk_claims_event_mention_id_event_mentions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
        sa.UniqueConstraint("event_mention_id", "claim_key", name="uq_claims_mention_key"),
    )
    op.create_index(op.f("ix_claims_event_mention_id"), "claims", ["event_mention_id"], unique=False)
    op.create_index(op.f("ix_claims_claimant_entity_id"), "claims", ["claimant_entity_id"], unique=False)
    op.create_index(
        "ix_claims_comparison_key", "claims", ["comparison_key", "event_mention_id"], unique=False
    )
    op.create_table(
        "event_relations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_event_id", sa.BigInteger(), nullable=False),
        sa.Column("target_event_id", sa.BigInteger(), nullable=False),
        sa.Column("relation_type", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source_event_id <> target_event_id",
            name=op.f("ck_event_relations_different_events"),
        ),
        sa.CheckConstraint(
            "relation_type in ('precedes', 'extends', 'replaces', 'responds_to', 'same_series')",
            name=op.f("ck_event_relations_relation_type"),
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["events.id"],
            name=op.f("fk_event_relations_source_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_event_id"],
            ["events.id"],
            name=op.f("fk_event_relations_target_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_relations")),
        sa.UniqueConstraint(
            "source_event_id", "target_event_id", "relation_type", name="uq_event_relations_pair"
        ),
    )
    op.create_index(
        op.f("ix_event_relations_source_event_id"),
        "event_relations",
        ["source_event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_event_relations_target_event_id"),
        "event_relations",
        ["target_event_id"],
        unique=False,
    )
    op.create_table(
        "research_case_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("usage_type", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "usage_type in ('primary', 'background', 'monitoring')",
            name=op.f("ck_research_case_events_usage_type"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_research_case_events_event_id_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["research_case_id"],
            ["research_cases.id"],
            name=op.f("fk_research_case_events_research_case_id_research_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_case_events")),
        sa.UniqueConstraint("research_case_id", "event_id", name="uq_research_case_events_event"),
    )
    op.create_index(
        op.f("ix_research_case_events_research_case_id"),
        "research_case_events",
        ["research_case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_case_events_event_id"),
        "research_case_events",
        ["event_id"],
        unique=False,
    )
    op.create_table(
        "research_case_data_slices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("filter_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["structured_datasets.id"],
            name=op.f("fk_research_case_data_slices_dataset_id_structured_datasets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["research_case_id"],
            ["research_cases.id"],
            name=op.f("fk_research_case_data_slices_research_case_id_research_cases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["structured_snapshots.id"],
            name=op.f("fk_research_case_data_slices_snapshot_id_structured_snapshots"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_case_data_slices")),
        sa.UniqueConstraint("research_case_id", "label", name="uq_research_case_data_slices_label"),
    )
    op.create_index(
        op.f("ix_research_case_data_slices_research_case_id"),
        "research_case_data_slices",
        ["research_case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_case_data_slices_dataset_id"),
        "research_case_data_slices",
        ["dataset_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_case_data_slices_snapshot_id"),
        "research_case_data_slices",
        ["snapshot_id"],
        unique=False,
    )
    op.create_table(
        "capability_templates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("default_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('active', 'retired')",
            name=op.f("ck_capability_templates_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_templates")),
        sa.UniqueConstraint("slug", "version", name="uq_capability_templates_slug_version"),
    )
    op.create_table(
        "capability_configs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("status in ('active', 'disabled')", name=op.f("ck_capability_configs_status")),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_capability_configs_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["capability_templates.id"],
            name=op.f("fk_capability_configs_template_id_capability_templates"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_configs")),
        sa.UniqueConstraint("owner_id", "name", name="uq_capability_configs_owner_name"),
    )
    op.create_index(op.f("ix_capability_configs_owner_id"), "capability_configs", ["owner_id"], unique=False)
    op.create_index(
        op.f("ix_capability_configs_template_id"),
        "capability_configs",
        ["template_id"],
        unique=False,
    )
    op.create_table(
        "capability_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("config_id", sa.BigInteger(), nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("artifact_ref", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_capability_runs_status"),
        ),
        sa.CheckConstraint(
            "finished_at is null or started_at is null or finished_at >= started_at",
            name=op.f("ck_capability_runs_valid_time_range"),
        ),
        sa.ForeignKeyConstraint(
            ["config_id"],
            ["capability_configs.id"],
            name=op.f("fk_capability_runs_config_id_capability_configs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["research_case_id"],
            ["research_cases.id"],
            name=op.f("fk_capability_runs_research_case_id_research_cases"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_runs")),
    )
    op.create_index(op.f("ix_capability_runs_config_id"), "capability_runs", ["config_id"], unique=False)
    op.create_index(
        op.f("ix_capability_runs_research_case_id"),
        "capability_runs",
        ["research_case_id"],
        unique=False,
    )
    op.create_index(
        "ix_capability_runs_case_created",
        "capability_runs",
        ["research_case_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_capability_runs_case_created", table_name="capability_runs")
    op.drop_index(op.f("ix_capability_runs_research_case_id"), table_name="capability_runs")
    op.drop_index(op.f("ix_capability_runs_config_id"), table_name="capability_runs")
    op.drop_table("capability_runs")
    op.drop_index(op.f("ix_capability_configs_template_id"), table_name="capability_configs")
    op.drop_index(op.f("ix_capability_configs_owner_id"), table_name="capability_configs")
    op.drop_table("capability_configs")
    op.drop_table("capability_templates")
    op.drop_index(op.f("ix_research_case_data_slices_snapshot_id"), table_name="research_case_data_slices")
    op.drop_index(op.f("ix_research_case_data_slices_dataset_id"), table_name="research_case_data_slices")
    op.drop_index(
        op.f("ix_research_case_data_slices_research_case_id"),
        table_name="research_case_data_slices",
    )
    op.drop_table("research_case_data_slices")
    op.drop_index(op.f("ix_research_case_events_event_id"), table_name="research_case_events")
    op.drop_index(op.f("ix_research_case_events_research_case_id"), table_name="research_case_events")
    op.drop_table("research_case_events")
    op.drop_index(op.f("ix_event_relations_target_event_id"), table_name="event_relations")
    op.drop_index(op.f("ix_event_relations_source_event_id"), table_name="event_relations")
    op.drop_table("event_relations")
    op.drop_index("ix_claims_comparison_key", table_name="claims")
    op.drop_index(op.f("ix_claims_claimant_entity_id"), table_name="claims")
    op.drop_index(op.f("ix_claims_event_mention_id"), table_name="claims")
    op.drop_table("claims")
    op.drop_index("ix_event_mentions_review", table_name="event_mentions")
    op.drop_index(op.f("ix_event_mentions_document_version_id"), table_name="event_mentions")
    op.drop_index(op.f("ix_event_mentions_event_id"), table_name="event_mentions")
    op.drop_table("event_mentions")
    op.drop_table("event_entities")
    op.drop_table("document_topics")
    op.drop_index("ix_document_entities_entity_role", table_name="document_entities")
    op.drop_table("document_entities")
    op.drop_index("ix_events_series_start", table_name="events")
    op.drop_index("ix_events_country_start", table_name="events")
    op.drop_index(op.f("ix_events_primary_place_entity_id"), table_name="events")
    op.drop_index(op.f("ix_events_country_entity_id"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_topics_parent_id"), table_name="topics")
    op.drop_table("topics")
    op.drop_table("entities")
