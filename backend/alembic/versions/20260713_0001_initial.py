"""create the initial backend schema

Revision ID: 20260713_0001
Revises:
Create Date: 2026-07-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260713_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role in ('user', 'admin')", name=op.f("ck_users_role")),
        sa.CheckConstraint("status in ('active', 'disabled')", name=op.f("ck_users_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("organization_name", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("country_or_region", sa.Text(), nullable=False),
        sa.Column("primary_language", sa.Text(), nullable=False),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("authority_level", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'paused', 'retired')",
            name=op.f("ck_sources_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint(
            "name",
            "organization_name",
            name=op.f("uq_sources_name_organization"),
        ),
    )

    op.create_table(
        "source_channels",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entry_url", sa.Text(), nullable=False),
        sa.Column("collector_type", sa.String(length=16), nullable=False),
        sa.Column(
            "collector_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("adapter_version", sa.Text(), nullable=False),
        sa.Column("link_role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cursor_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('draft', 'probing', 'shadow', 'active', 'paused', 'blocked', 'retired')",
            name=op.f("ck_source_channels_status"),
        ),
        sa.CheckConstraint(
            "collector_type in ('rss', 'api', 'html', 'pdf')",
            name=op.f("ck_source_channels_collector_type"),
        ),
        sa.CheckConstraint(
            "link_role in ('official', 'discovery', 'unknown')",
            name=op.f("ck_source_channels_link_role"),
        ),
        sa.CheckConstraint(
            "poll_interval_seconds is null or poll_interval_seconds >= 300",
            name=op.f("ck_source_channels_poll_interval"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_source_channels_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_channels")),
        sa.UniqueConstraint(
            "source_id",
            "entry_url",
            name=op.f("uq_source_channels_source_url"),
        ),
    )
    op.create_index(
        op.f("ix_source_channels_source_id"),
        "source_channels",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_source_channels_due"),
        "source_channels",
        ["next_run_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active' and next_run_at is not null"),
    )

    op.create_table(
        "source_channel_policies",
        sa.Column("channel_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("robots_state", sa.String(length=24), nullable=False),
        sa.Column("terms_state", sa.Text(), nullable=False),
        sa.Column("terms_url", sa.Text(), nullable=True),
        sa.Column("storage_scope", sa.String(length=24), nullable=False),
        sa.Column("rag_scope", sa.String(length=24), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "robots_state in ('unknown', 'allowed', 'disallowed', 'review_required')",
            name=op.f("ck_source_channel_policies_robots_state"),
        ),
        sa.CheckConstraint(
            "storage_scope in ('none', 'metadata', 'extracted_text', 'full_content')",
            name=op.f("ck_source_channel_policies_storage_scope"),
        ),
        sa.CheckConstraint(
            "rag_scope in ('none', 'metadata', 'extracted_text', 'full_content')",
            name=op.f("ck_source_channel_policies_rag_scope"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["source_channels.id"],
            name=op.f("fk_source_channel_policies_channel_id_source_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("channel_id", name=op.f("pk_source_channel_policies")),
    )

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_discovered", sa.Integer(), nullable=False),
        sa.Column("items_persisted", sa.Integer(), nullable=False),
        sa.Column("error_category", sa.String(length=16), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column(
            "report",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "trigger_kind in ('manual', 'scheduled', 'probe', 'retry')",
            name=op.f("ck_collection_runs_trigger_kind"),
        ),
        sa.CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')",
            name=op.f("ck_collection_runs_status"),
        ),
        sa.CheckConstraint(
            "error_category is null or error_category in "
            "('network', 'http', 'access', 'policy', 'parser', 'quality', 'storage', 'internal', 'unknown')",
            name=op.f("ck_collection_runs_error_category"),
        ),
        sa.CheckConstraint(
            "items_discovered >= 0",
            name=op.f("ck_collection_runs_items_discovered_nonnegative"),
        ),
        sa.CheckConstraint(
            "items_persisted >= 0",
            name=op.f("ck_collection_runs_items_persisted_nonnegative"),
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name=op.f("ck_collection_runs_retry_count_nonnegative"),
        ),
        sa.CheckConstraint(
            "finished_at is null or started_at is null or finished_at >= started_at",
            name=op.f("ck_collection_runs_valid_time_range"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["source_channels.id"],
            name=op.f("fk_collection_runs_channel_id_source_channels"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_runs")),
    )
    op.create_index(
        op.f("ix_collection_runs_channel_id"),
        "collection_runs",
        ["channel_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_collection_runs_channel_created"),
        "collection_runs",
        ["channel_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("uq_collection_runs_active_channel"),
        "collection_runs",
        ["channel_id"],
        unique=True,
        postgresql_where=sa.text("status in ('queued', 'running')"),
    )
    op.create_index(
        op.f("ix_collection_runs_stale_running"),
        "collection_runs",
        ["heartbeat_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "collection_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("request_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("discovered_from_url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(length=16), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('discovered', 'fetched', 'not_modified', 'skipped', 'failed')",
            name=op.f("ck_collection_items_status"),
        ),
        sa.CheckConstraint(
            "http_status is null or http_status between 100 and 599",
            name=op.f("ck_collection_items_http_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["collection_runs.id"],
            name=op.f("fk_collection_items_run_id_collection_runs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collection_items")),
        sa.UniqueConstraint(
            "run_id",
            "normalized_url",
            name=op.f("uq_collection_items_run_url"),
        ),
    )
    op.create_index(
        op.f("ix_collection_items_run_id"),
        "collection_items",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_collection_items_run_status"),
        "collection_items",
        ["run_id", "status"],
        unique=False,
    )

    op.create_table(
        "raw_assets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("collection_item_id", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "response_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("request_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "byte_size >= 0",
            name=op.f("ck_raw_assets_byte_size_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["collection_item_id"],
            ["collection_items.id"],
            name=op.f("fk_raw_assets_collection_item_id_collection_items"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_assets")),
        sa.UniqueConstraint(
            "collection_item_id",
            name=op.f("uq_raw_assets_collection_item_id"),
        ),
    )
    op.create_index(
        op.f("ix_raw_assets_sha256"),
        "raw_assets",
        ["sha256"],
        unique=False,
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("discovery_url", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("doi", sa.Text(), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issue_date_text", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_version_no", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "latest_version_no >= 0",
            name=op.f("ck_documents_latest_version_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_documents_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "source_id",
            "external_id",
            name=op.f("uq_documents_source_external_id"),
        ),
        sa.UniqueConstraint("canonical_url", name=op.f("uq_documents_canonical_url")),
        sa.UniqueConstraint("doi", name=op.f("uq_documents_doi")),
    )
    op.create_index(
        op.f("ix_documents_source_id"),
        "documents",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_documents_source_published"),
        "documents",
        ["source_id", "published_at"],
        unique=False,
    )

    op.create_table(
        "document_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("raw_asset_id", sa.BigInteger(), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issue_date_text", sa.Text(), nullable=True),
        sa.Column("extractor_name", sa.Text(), nullable=False),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_document_versions_version_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["raw_asset_id"],
            ["raw_assets.id"],
            name=op.f("fk_document_versions_raw_asset_id_raw_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id",
            "version_no",
            name=op.f("uq_document_versions_number"),
        ),
        sa.UniqueConstraint(
            "document_id",
            "content_sha256",
            name=op.f("uq_document_versions_hash"),
        ),
    )
    op.create_index(
        op.f("ix_document_versions_document_id"),
        "document_versions",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_versions_raw_asset_id"),
        "document_versions",
        ["raw_asset_id"],
        unique=False,
    )

    op.create_table(
        "research_cases",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("research_question", sa.Text(), nullable=False),
        sa.Column(
            "scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'archived')",
            name=op.f("ck_research_cases_status"),
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_research_cases_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_cases")),
    )
    op.create_index(
        op.f("ix_research_cases_owner_id"),
        "research_cases",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_cases_owner_status"),
        "research_cases",
        ["owner_id", "status", "updated_at"],
        unique=False,
    )

    op.create_table(
        "research_case_documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("research_case_id", sa.BigInteger(), nullable=False),
        sa.Column("document_version_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by", sa.BigInteger(), nullable=False),
        sa.Column("usage_type", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "usage_type in ('support', 'background', 'refute', 'to_verify')",
            name=op.f("ck_research_case_documents_usage_type"),
        ),
        sa.ForeignKeyConstraint(
            ["added_by"],
            ["users.id"],
            name=op.f("fk_research_case_documents_added_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_research_case_documents_document_version_id_document_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["research_case_id"],
            ["research_cases.id"],
            name=op.f("fk_research_case_documents_research_case_id_research_cases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_case_documents")),
        sa.UniqueConstraint(
            "research_case_id",
            "document_version_id",
            name=op.f("uq_research_case_documents_version"),
        ),
    )
    op.create_index(
        op.f("ix_research_case_documents_research_case_id"),
        "research_case_documents",
        ["research_case_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_case_documents_document_version_id"),
        "research_case_documents",
        ["document_version_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_research_case_documents_added_by"),
        "research_case_documents",
        ["added_by"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_research_case_documents_added_by"),
        table_name="research_case_documents",
    )
    op.drop_index(
        op.f("ix_research_case_documents_document_version_id"),
        table_name="research_case_documents",
    )
    op.drop_index(
        op.f("ix_research_case_documents_research_case_id"),
        table_name="research_case_documents",
    )
    op.drop_table("research_case_documents")

    op.drop_index(op.f("ix_research_cases_owner_status"), table_name="research_cases")
    op.drop_index(op.f("ix_research_cases_owner_id"), table_name="research_cases")
    op.drop_table("research_cases")

    op.drop_index(op.f("ix_document_versions_raw_asset_id"), table_name="document_versions")
    op.drop_index(op.f("ix_document_versions_document_id"), table_name="document_versions")
    op.drop_table("document_versions")

    op.drop_index(op.f("ix_documents_source_published"), table_name="documents")
    op.drop_index(op.f("ix_documents_source_id"), table_name="documents")
    op.drop_table("documents")

    op.drop_index(op.f("ix_raw_assets_sha256"), table_name="raw_assets")
    op.drop_table("raw_assets")

    op.drop_index(op.f("ix_collection_items_run_status"), table_name="collection_items")
    op.drop_index(op.f("ix_collection_items_run_id"), table_name="collection_items")
    op.drop_table("collection_items")

    op.drop_index(op.f("ix_collection_runs_stale_running"), table_name="collection_runs")
    op.drop_index(op.f("uq_collection_runs_active_channel"), table_name="collection_runs")
    op.drop_index(op.f("ix_collection_runs_channel_created"), table_name="collection_runs")
    op.drop_index(op.f("ix_collection_runs_channel_id"), table_name="collection_runs")
    op.drop_table("collection_runs")

    op.drop_table("source_channel_policies")

    op.drop_index(op.f("ix_source_channels_due"), table_name="source_channels")
    op.drop_index(op.f("ix_source_channels_source_id"), table_name="source_channels")
    op.drop_table("source_channels")

    op.drop_table("sources")
    op.drop_table("users")
