from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

ID_TYPE = BigInteger().with_variant(Integer, "sqlite")
JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role in ('user', 'admin')", name="role"),
        CheckConstraint("status in ('active', 'disabled')", name="status"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class ReaderAccessKey(TimestampMixin, Base):
    __tablename__ = "reader_access_keys"
    __table_args__ = (
        UniqueConstraint("key_sha256", name="uq_reader_access_keys_sha256"),
        CheckConstraint("status in ('active', 'revoked')", name="status"),
        Index("ix_reader_access_keys_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False, default="本机访问密钥")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Source(TimestampMixin, Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("name", "organization_name", name="uq_sources_name_organization"),
        CheckConstraint("status in ('active', 'paused', 'retired')", name="status"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    organization_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    country_or_region: Mapped[str] = mapped_column(Text, nullable=False, default="")
    primary_language: Mapped[str] = mapped_column(Text, nullable=False, default="")
    homepage_url: Mapped[str | None] = mapped_column(Text)
    authority_level: Mapped[str] = mapped_column(Text, nullable=False, default="unrated")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    channels: Mapped[list[SourceChannel]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class SourceChannel(TimestampMixin, Base):
    __tablename__ = "source_channels"
    __table_args__ = (
        UniqueConstraint("source_id", "entry_url", name="uq_source_channels_source_url"),
        CheckConstraint(
            "status in ('draft', 'probing', 'shadow', 'active', 'paused', 'blocked', 'retired')",
            name="status",
        ),
        CheckConstraint("collector_type in ('rss', 'api', 'html', 'pdf')", name="collector_type"),
        CheckConstraint("link_role in ('official', 'discovery', 'unknown')", name="link_role"),
        CheckConstraint(
            "poll_interval_seconds is null or poll_interval_seconds >= 300", name="poll_interval"
        ),
        Index(
            "ix_source_channels_due",
            "next_run_at",
            postgresql_where=text("status = 'active' and next_run_at is not null"),
            sqlite_where=text("status = 'active' and next_run_at is not null"),
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    entry_url: Mapped[str] = mapped_column(Text, nullable=False)
    collector_type: Mapped[str] = mapped_column(String(16), nullable=False)
    collector_config: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    adapter_version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")
    link_role: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    poll_interval_seconds: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_state: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[Source] = relationship(back_populates="channels")
    policy: Mapped[SourceChannelPolicy | None] = relationship(
        back_populates="channel", cascade="all, delete-orphan", uselist=False
    )


class SourceChannelPolicy(Base):
    __tablename__ = "source_channel_policies"
    __table_args__ = (
        CheckConstraint(
            "robots_state in ('unknown', 'allowed', 'disallowed', 'review_required')",
            name="robots_state",
        ),
        CheckConstraint(
            "storage_scope in ('none', 'metadata', 'official_abstract', 'extracted_text', 'full_content')",
            name="storage_scope",
        ),
        CheckConstraint(
            "rag_scope in ('none', 'metadata', 'extracted_text', 'full_content')",
            name="rag_scope",
        ),
    )

    channel_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("source_channels.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    robots_state: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    terms_state: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    terms_url: Mapped[str | None] = mapped_column(Text)
    storage_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="metadata")
    rag_scope: Mapped[str] = mapped_column(String(24), nullable=False, default="none")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    channel: Mapped[SourceChannel] = relationship(back_populates="policy")


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    __table_args__ = (
        CheckConstraint("trigger_kind in ('manual', 'scheduled', 'probe', 'retry')", name="trigger_kind"),
        CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "error_category is null or error_category in "
            "('network', 'http', 'access', 'policy', 'parser', 'quality', 'storage', 'internal', 'unknown')",
            name="error_category",
        ),
        CheckConstraint("items_discovered >= 0", name="items_discovered_nonnegative"),
        CheckConstraint("items_persisted >= 0", name="items_persisted_nonnegative"),
        CheckConstraint("retry_count >= 0", name="retry_count_nonnegative"),
        CheckConstraint(
            "finished_at is null or started_at is null or finished_at >= started_at",
            name="valid_time_range",
        ),
        Index("ix_collection_runs_channel_created", "channel_id", "created_at"),
        Index(
            "uq_collection_runs_active_channel",
            "channel_id",
            unique=True,
            postgresql_where=text("status in ('queued', 'running')"),
            sqlite_where=text("status in ('queued', 'running')"),
        ),
        Index(
            "ix_collection_runs_stale_running",
            "heartbeat_at",
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("source_channels.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_persisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_category: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    celery_task_id: Mapped[str | None] = mapped_column(Text)
    report: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CollectionItem(Base):
    __tablename__ = "collection_items"
    __table_args__ = (
        UniqueConstraint("run_id", "normalized_url", name="uq_collection_items_run_url"),
        CheckConstraint(
            "status in ('discovered', 'fetched', 'not_modified', 'skipped', 'failed')",
            name="status",
        ),
        CheckConstraint("http_status is null or http_status between 100 and 599", name="http_status"),
        Index("ix_collection_items_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("collection_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_url: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_from_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="discovered")
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RawAsset(Base):
    __tablename__ = "raw_assets"
    __table_args__ = (CheckConstraint("byte_size >= 0", name="byte_size_nonnegative"),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    collection_item_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("collection_items.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    response_headers: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    request_url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_documents_source_external_id"),
        UniqueConstraint("canonical_url", name="uq_documents_canonical_url"),
        UniqueConstraint("doi", name="uq_documents_doi"),
        CheckConstraint("latest_version_no >= 0", name="latest_version_nonnegative"),
        Index("ix_documents_source_published", "source_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    canonical_url: Mapped[str | None] = mapped_column(Text)
    discovery_url: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    doi: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="und")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issue_date_text: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_document_versions_number"),
        UniqueConstraint("document_id", "content_sha256", name="uq_document_versions_hash"),
        CheckConstraint("version_no > 0", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    raw_asset_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("raw_assets.id", ondelete="RESTRICT"), index=True
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    abstract: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="und")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issue_date_text: Mapped[str | None] = mapped_column(Text)
    extractor_name: Mapped[str] = mapped_column(Text, nullable=False)
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DocumentAbstractState(TimestampMixin, Base):
    __tablename__ = "document_abstract_states"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending', 'stored', 'unavailable', 'retryable', 'blocked')",
            name="status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_nonnegative"),
        Index("ix_document_abstract_states_due", "status", "next_retry_at"),
    )

    document_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    channel_id: Mapped[int | None] = mapped_column(
        ID_TYPE,
        ForeignKey("source_channels.id", ondelete="SET NULL"),
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reason_code: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartnerDocumentRelease(TimestampMixin, Base):
    __tablename__ = "partner_document_releases"
    __table_args__ = (
        CheckConstraint("status in ('published', 'withdrawn')", name="status"),
        CheckConstraint("item_count >= 0", name="item_count_nonnegative"),
        CheckConstraint("abstract_count >= 0", name="abstract_count_nonnegative"),
        CheckConstraint("length(manifest_sha256) = 64", name="manifest_sha256_length"),
        CheckConstraint(
            "(status = 'published' and withdrawn_at is null) "
            "or (status = 'withdrawn' and withdrawn_at is not null)",
            name="withdrawn_state",
        ),
        Index(
            "uq_partner_document_releases_current",
            "status",
            unique=True,
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    abstract_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PartnerDocumentReleaseItem(Base):
    __tablename__ = "partner_document_release_items"
    __table_args__ = (
        UniqueConstraint(
            "release_id",
            "document_version_id",
            name="uq_partner_document_release_items_version",
        ),
        Index("ix_partner_document_release_items_document", "document_id"),
    )

    release_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("partner_document_releases.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    document_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("documents.id", ondelete="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )
    document_version_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )


class StructuredDataset(TimestampMixin, Base):
    __tablename__ = "structured_datasets"
    __table_args__ = (
        UniqueConstraint("channel_id", name="uq_structured_datasets_channel"),
        UniqueConstraint("dataset_key", name="uq_structured_datasets_key"),
        CheckConstraint("length(trim(dataset_key)) > 0", name="dataset_key_nonempty"),
        CheckConstraint("length(trim(scope_id)) > 0", name="scope_id_nonempty"),
        CheckConstraint("frequency in ('annual', 'monthly')", name="frequency"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("source_channels.id", ondelete="RESTRICT"), nullable=False
    )
    dataset_key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="annual")


class PartnerApiClient(TimestampMixin, Base):
    __tablename__ = "partner_api_clients"
    __table_args__ = (
        CheckConstraint("status in ('active', 'revoked')", name="status"),
        CheckConstraint("length(key_prefix) = 16", name="key_prefix_length"),
        CheckConstraint("length(key_sha256) = 64", name="key_sha256_length"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    key_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StructuredDatasetPublication(TimestampMixin, Base):
    __tablename__ = "structured_dataset_publications"
    __table_args__ = (
        UniqueConstraint("dataset_id", "snapshot_id", name="uq_dataset_publications_snapshot"),
        CheckConstraint("status in ('published', 'withdrawn')", name="status"),
        CheckConstraint(
            "(status = 'published' and withdrawn_at is null) "
            "or (status = 'withdrawn' and withdrawn_at is not null)",
            name="withdrawn_state",
        ),
        Index(
            "uq_dataset_publications_current",
            "dataset_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
            sqlite_where=text("status = 'published'"),
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("structured_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("structured_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    approved_by: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StructuredSnapshot(Base):
    __tablename__ = "structured_snapshots"
    __table_args__ = (
        UniqueConstraint("collection_run_id", name="uq_structured_snapshots_collection_run"),
        CheckConstraint("length(query_signature) = 64", name="query_signature_length"),
        CheckConstraint("length(snapshot_hash) = 64", name="snapshot_hash_length"),
        CheckConstraint(
            "expected_count >= 0 and returned_count >= 0 and valued_count >= 0 "
            "and source_null_count >= 0 and not_returned_count >= 0",
            name="counts_nonnegative",
        ),
        CheckConstraint(
            "expected_count = returned_count + not_returned_count",
            name="expected_count_balance",
        ),
        CheckConstraint(
            "returned_count = valued_count + source_null_count",
            name="returned_count_balance",
        ),
        Index("ix_structured_snapshots_dataset_retrieved", "dataset_id", "retrieved_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("structured_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    collection_run_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("collection_runs.id", ondelete="RESTRICT"), nullable=False
    )
    query_manifest: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    query_signature: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_version: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    returned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    valued_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_null_count: Mapped[int] = mapped_column(Integer, nullable=False)
    not_returned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StructuredObservation(Base):
    __tablename__ = "structured_observations"
    __table_args__ = (
        UniqueConstraint("dataset_id", "observation_key", name="uq_structured_observations_dataset_key"),
        CheckConstraint("length(observation_key) = 64", name="observation_key_length"),
        CheckConstraint("length(country_iso3) = 3", name="country_iso3_length"),
        CheckConstraint("partner_iso3 is null or length(partner_iso3) = 3", name="partner_iso3_length"),
        CheckConstraint("frequency in ('annual', 'monthly')", name="frequency"),
        CheckConstraint(
            "(frequency = 'annual' and period between 1900 and 2100) "
            "or (frequency = 'monthly' and period between 190001 and 210012 "
            "and period % 100 between 1 and 12)",
            name="period",
        ),
        CheckConstraint("length(trim(metric_code)) > 0", name="metric_code_nonempty"),
        CheckConstraint("latest_version_no >= 0", name="latest_version_nonnegative"),
        CheckConstraint("last_seen_at >= first_seen_at", name="seen_time_range"),
        CheckConstraint(
            "(indicator_code is not null and length(trim(indicator_code)) > 0 "
            "and partner_iso3 is null and commodity_classification is null "
            "and commodity_code is null and trade_flow is null and partner2_code is null "
            "and customs_code is null and mot_code is null) "
            "or (indicator_code is null and partner_iso3 is not null "
            "and (commodity_classification is null "
            "or length(trim(commodity_classification)) > 0) and commodity_code is not null "
            "and length(trim(commodity_code)) > 0 and trade_flow in ('M', 'X') "
            "and partner2_code is not null and length(trim(partner2_code)) > 0 "
            "and customs_code is not null and length(trim(customs_code)) > 0 "
            "and mot_code is not null and length(trim(mot_code)) > 0) "
            "or (indicator_code is not null and length(trim(indicator_code)) > 0 "
            "and partner_iso3 is not null "
            "and ((frequency = 'monthly' and commodity_code is null "
            "and commodity_classification is null) "
            "or (commodity_code is not null and length(trim(commodity_code)) > 0 "
            "and (commodity_classification is null "
            "or length(trim(commodity_classification)) > 0))) and trade_flow is null "
            "and partner2_code is null and customs_code is null and mot_code is null)",
            name="dimensions",
        ),
        Index(
            "ix_structured_observations_dataset_country_period",
            "dataset_id",
            "country_iso3",
            "period",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("structured_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    observation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    country_iso3: Mapped[str] = mapped_column(String(3), nullable=False)
    partner_iso3: Mapped[str | None] = mapped_column(String(3))
    indicator_code: Mapped[str | None] = mapped_column(Text)
    commodity_classification: Mapped[str | None] = mapped_column(Text)
    source_dataset_code: Mapped[str | None] = mapped_column(Text)
    commodity_code: Mapped[str | None] = mapped_column(Text)
    trade_flow: Mapped[str | None] = mapped_column(String(1))
    partner2_code: Mapped[str | None] = mapped_column(Text)
    customs_code: Mapped[str | None] = mapped_column(Text)
    mot_code: Mapped[str | None] = mapped_column(Text)
    frequency: Mapped[str] = mapped_column(String(16), nullable=False, default="annual")
    period: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_code: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class StructuredObservationVersion(Base):
    __tablename__ = "structured_observation_versions"
    __table_args__ = (
        UniqueConstraint(
            "observation_id",
            "version_no",
            name="uq_structured_observation_versions_number",
        ),
        UniqueConstraint(
            "snapshot_id",
            "observation_id",
            name="uq_structured_observation_versions_snapshot_observation",
        ),
        UniqueConstraint(
            "observation_id",
            "id",
            name="uq_structured_observation_versions_observation_id_id",
        ),
        CheckConstraint("version_no > 0", name="version_positive"),
        CheckConstraint("length(observation_hash) = 64", name="observation_hash_length"),
        CheckConstraint("unit is null or length(trim(unit)) > 0", name="unit_valid"),
        CheckConstraint("value is null or unit is not null", name="valued_unit_required"),
        CheckConstraint("length(trim(source_url)) > 0", name="source_url_nonempty"),
        CheckConstraint("length(trim(source_status)) > 0", name="source_status_nonempty"),
        CheckConstraint(
            "(value is not null and missing_reason is null) "
            "or (value is null and missing_reason is not null "
            "and length(trim(missing_reason)) > 0)",
            name="value_or_missing",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    observation_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("structured_observations.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("structured_snapshots.id", ondelete="RESTRICT"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    unit: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(String(3))
    price_basis: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_release_date: Mapped[date | None] = mapped_column(Date)
    source_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    missing_reason: Mapped[str | None] = mapped_column(Text)
    is_reported: Mapped[bool | None] = mapped_column(Boolean)
    is_aggregate: Mapped[bool | None] = mapped_column(Boolean)
    quality_flags: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)


class StructuredSnapshotObservation(Base):
    __tablename__ = "structured_snapshot_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id"],
            ["structured_snapshots.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["observation_id", "observation_version_id"],
            [
                "structured_observation_versions.observation_id",
                "structured_observation_versions.id",
            ],
            ondelete="RESTRICT",
        ),
        Index(
            "ix_structured_snapshot_observations_observation_version_id",
            "observation_version_id",
        ),
    )

    snapshot_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    observation_id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True)
    observation_version_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)


class ResearchCase(TimestampMixin, Base):
    __tablename__ = "research_cases"
    __table_args__ = (
        CheckConstraint("status in ('active', 'archived')", name="status"),
        Index("ix_research_cases_owner_status", "owner_id", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    research_question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    brief_original: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    brief_confirmed: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    brief_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")


class ResearchCaseMember(TimestampMixin, Base):
    __tablename__ = "research_case_members"
    __table_args__ = (
        UniqueConstraint("research_case_id", "user_id", name="uq_research_case_members_user"),
        CheckConstraint("role in ('owner', 'reviewer', 'editor', 'viewer')", name="role"),
        Index("ix_research_case_members_case_role", "research_case_id", "role"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    added_by: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )


class ResearchContribution(Base):
    __tablename__ = "research_contributions"
    __table_args__ = (Index("ix_research_contributions_case_created", "research_case_id", "created_at"),)

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(48), nullable=False)
    object_type: Mapped[str] = mapped_column(String(48), nullable=False)
    object_key: Mapped[str] = mapped_column(String(200), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchCaseDocument(Base):
    __tablename__ = "research_case_documents"
    __table_args__ = (
        UniqueConstraint(
            "research_case_id", "document_version_id", name="uq_research_case_documents_version"
        ),
        CheckConstraint("usage_type in ('support', 'background', 'refute', 'to_verify')", name="usage_type"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("document_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    added_by: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    usage_type: Mapped[str] = mapped_column(String(16), nullable=False, default="background")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FieldMaterial(TimestampMixin, Base):
    __tablename__ = "field_materials"
    __table_args__ = (
        CheckConstraint(
            "material_type in ('field_note', 'interview_transcript', 'photo', 'supporting_document')",
            name="material_type",
        ),
        CheckConstraint(
            "privacy_level in ('restricted', 'anonymized', 'shareable')",
            name="privacy_level",
        ),
        CheckConstraint(
            "evidence_status = 'user_provided_unreviewed'",
            name="evidence_status",
        ),
        CheckConstraint("byte_size > 0", name="byte_size_positive"),
        CheckConstraint("length(sha256) = 64", name="sha256_length"),
        CheckConstraint(
            "country_iso3 is not null or research_case_id is not null",
            name="scope_required",
        ),
        Index("ix_field_materials_country_created", "country_iso3", "created_at"),
        Index("ix_field_materials_case_created", "research_case_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    research_case_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="SET NULL"), index=True
    )
    country_iso3: Mapped[str | None] = mapped_column(String(3), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(80), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    material_type: Mapped[str] = mapped_column(String(32), nullable=False)
    privacy_level: Mapped[str] = mapped_column(String(24), nullable=False, default="restricted")
    evidence_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="user_provided_unreviewed"
    )
    authorization_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    captured_on: Mapped[date | None] = mapped_column(Date)
    method_note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ResearchEntity(TimestampMixin, Base):
    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("entity_type", "canonical_key", name="uq_entities_type_key"),
        CheckConstraint(
            "entity_type in "
            "('country', 'place', 'organization', 'person', 'armed_group', "
            "'commodity', 'policy')",
            name="entity_type",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)


class DocumentEntity(Base):
    __tablename__ = "document_entities"
    __table_args__ = (
        CheckConstraint(
            "role in ('about', 'actor', 'location', 'commodity', 'policy', 'affected')",
            name="role",
        ),
        CheckConstraint(
            "extraction_method in ('manual', 'imported', 'rule')",
            name="extraction_method",
        ),
        CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name="review_status",
        ),
        Index("ix_document_entities_entity_role", "entity_id", "role"),
    )

    document_version_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    entity_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    role: Mapped[str] = mapped_column(String(24), primary_key=True)
    extraction_method: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Topic(TimestampMixin, Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parent_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("topics.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    __table_args__ = (CheckConstraint("status in ('active', 'retired')", name="status"),)


class DocumentTopic(Base):
    __tablename__ = "document_topics"
    __table_args__ = (
        CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name="review_status",
        ),
    )

    document_version_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    topic_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ResearchEvent(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "event_type in ('policy', 'conflict', 'market', 'accident', 'other')",
            name="event_type",
        ),
        CheckConstraint(
            "date_precision in ('day', 'month', 'year', 'unknown')",
            name="date_precision",
        ),
        CheckConstraint("review_status in ('draft', 'reviewed')", name="review_status"),
        CheckConstraint(
            "end_at is null or start_at is null or end_at >= start_at",
            name="valid_time_range",
        ),
        Index("ix_events_country_start", "country_entity_id", "start_at"),
        Index("ix_events_series_start", "series_key", "start_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    series_key: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False)
    country_entity_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("entities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    primary_place_entity_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("entities.id", ondelete="SET NULL"), index=True
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_precision: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)


class EventEntity(Base):
    __tablename__ = "event_entities"
    __table_args__ = (
        CheckConstraint(
            "role in ('actor', 'location', 'commodity', 'policy', 'affected_country')",
            name="role",
        ),
    )

    event_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("events.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    entity_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("entities.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    role: Mapped[str] = mapped_column(String(24), primary_key=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class EventMention(TimestampMixin, Base):
    __tablename__ = "event_mentions"
    __table_args__ = (
        UniqueConstraint("event_id", "document_version_id", name="uq_event_mentions_version"),
        CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name="review_status",
        ),
        CheckConstraint(
            "match_score is null or (match_score >= 0 and match_score <= 1)",
            name="match_score_range",
        ),
        CheckConstraint(
            "source_reported_end_at is null or source_reported_start_at is null "
            "or source_reported_end_at >= source_reported_start_at",
            name="valid_time_range",
        ),
        Index("ix_event_mentions_review", "review_status", "event_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    event_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_version_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("document_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_reported_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_reported_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_reported_place: Mapped[str | None] = mapped_column(Text)
    mention_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_locator: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    source_fields: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    match_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    match_reasons: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    aggregation_version: Mapped[str] = mapped_column(Text, nullable=False, default="manual-v1")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")


class EvidenceClaim(TimestampMixin, Base):
    __tablename__ = "claims"
    __table_args__ = (
        UniqueConstraint("event_mention_id", "claim_key", name="uq_claims_mention_key"),
        CheckConstraint("claim_kind in ('factual', 'numeric', 'position')", name="claim_kind"),
        CheckConstraint(
            "review_status in ('pending', 'confirmed', 'rejected')",
            name="review_status",
        ),
        Index("ix_claims_comparison_key", "comparison_key", "event_mention_id"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    event_mention_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("event_mentions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    claim_key: Mapped[str] = mapped_column(String(64), nullable=False)
    claim_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    claimant_entity_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("entities.id", ondelete="SET NULL"), index=True
    )
    claimant_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    subject_text: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    value_text: Mapped[str] = mapped_column(Text, nullable=False)
    numeric_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    unit: Mapped[str | None] = mapped_column(Text)
    time_scope: Mapped[str | None] = mapped_column(Text)
    comparison_key: Mapped[str] = mapped_column(String(240), nullable=False)
    position_summary: Mapped[str | None] = mapped_column(Text)
    evidence_locator: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")


class EventRelation(Base):
    __tablename__ = "event_relations"
    __table_args__ = (
        UniqueConstraint(
            "source_event_id", "target_event_id", "relation_type", name="uq_event_relations_pair"
        ),
        CheckConstraint("source_event_id <> target_event_id", name="different_events"),
        CheckConstraint(
            "relation_type in ('precedes', 'extends', 'replaces', 'responds_to', 'same_series')",
            name="relation_type",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    source_event_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_event_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ResearchCaseEvent(Base):
    __tablename__ = "research_case_events"
    __table_args__ = (
        UniqueConstraint("research_case_id", "event_id", name="uq_research_case_events_event"),
        CheckConstraint(
            "usage_type in ('primary', 'background', 'monitoring')",
            name="usage_type",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    usage_type: Mapped[str] = mapped_column(String(16), nullable=False, default="primary")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ResearchCaseDataSlice(TimestampMixin, Base):
    __tablename__ = "research_case_data_slices"
    __table_args__ = (
        UniqueConstraint("research_case_id", "label", name="uq_research_case_data_slices_label"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dataset_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("structured_datasets.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("structured_snapshots.id", ondelete="RESTRICT"), index=True
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    filter_spec: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)


class CapabilityTemplate(TimestampMixin, Base):
    __tablename__ = "capability_templates"
    __table_args__ = (
        UniqueConstraint("slug", "version", name="uq_capability_templates_slug_version"),
        UniqueConstraint("catalog_key", name="uq_capability_templates_catalog_key"),
        Index("ix_capability_templates_catalog_key", "catalog_key"),
        CheckConstraint("status in ('draft', 'active', 'retired')", name="status"),
        CheckConstraint(
            "category in ('research_workflow', 'data_collection', 'field_collaboration')",
            name="category",
        ),
        CheckConstraint("visibility in ('public', 'internal', 'private')", name="visibility"),
        CheckConstraint(
            "validation_status in ('pending', 'verified', 'failed')",
            name="validation_status",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    catalog_key: Mapped[str | None] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="research_workflow")
    owner_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="internal")
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    execution_plan: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    slug: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    default_config: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class CapabilityConfig(TimestampMixin, Base):
    __tablename__ = "capability_configs"
    __table_args__ = (
        CheckConstraint(
            "status in ('draft', 'active', 'pending_review', 'published', "
            "'paused', 'expired', 'archived', 'disabled')",
            name="status",
        ),
        CheckConstraint("scope_type in ('personal', 'research_case')", name="scope_type"),
        Index(
            "uq_capability_configs_personal_name",
            "owner_id",
            "name",
            unique=True,
            postgresql_where=text("scope_type = 'personal'"),
            sqlite_where=text("scope_type = 'personal'"),
        ),
        Index(
            "uq_capability_configs_case_name",
            "research_case_id",
            "name",
            unique=True,
            postgresql_where=text("scope_type = 'research_case'"),
            sqlite_where=text("scope_type = 'research_case'"),
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("capability_templates.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    owner_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, default="personal")
    research_case_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class CapabilityRun(TimestampMixin, Base):
    __tablename__ = "capability_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('queued', 'running', 'succeeded', 'insufficient_data', 'failed')",
            name="status",
        ),
        CheckConstraint(
            "finished_at is null or started_at is null or finished_at >= started_at",
            name="valid_time_range",
        ),
        Index("ix_capability_runs_case_created", "research_case_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("capability_configs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    research_case_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="SET NULL"), index=True
    )
    config_revision_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("capability_config_revisions.id", ondelete="RESTRICT"), index=True
    )
    schedule_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("capability_schedules.id", ondelete="SET NULL"), index=True
    )
    requested_by: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reviewed_by: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_summary: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    artifact_ref: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CapabilityRunRevision(Base):
    __tablename__ = "capability_run_revisions"
    __table_args__ = (
        UniqueConstraint("capability_run_id", "revision_no", name="uq_capability_run_revisions_number"),
        Index("ix_capability_run_revisions_run_created", "capability_run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    capability_run_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("capability_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CapabilityConfigRevision(Base):
    __tablename__ = "capability_config_revisions"
    __table_args__ = (
        UniqueConstraint("config_id", "revision_no", name="uq_capability_config_revisions_number"),
        Index("ix_capability_config_revisions_config_created", "config_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("capability_configs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    template_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_by: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CapabilityReview(TimestampMixin, Base):
    __tablename__ = "capability_reviews"
    __table_args__ = (
        CheckConstraint(
            "review_type in ('validation', 'team_publish', 'public_publish', 'run')", name="review_type"
        ),
        CheckConstraint("status in ('pending', 'approved', 'rejected')", name="status"),
        Index("ix_capability_reviews_target", "target_type", "target_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    review_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    checklist: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_by: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CapabilityAuditEvent(Base):
    __tablename__ = "capability_audit_events"
    __table_args__ = (
        Index("ix_capability_audit_events_target_created", "target_type", "target_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[int] = mapped_column(ID_TYPE, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CapabilitySchedule(TimestampMixin, Base):
    __tablename__ = "capability_schedules"
    __table_args__ = (
        CheckConstraint("status in ('active', 'paused', 'archived')", name="status"),
        UniqueConstraint("idempotency_key", name="uq_capability_schedules_idempotency"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    config_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("capability_configs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    cadence: Mapped[str] = mapped_column(String(80), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)


class CapabilityRunTarget(TimestampMixin, Base):
    __tablename__ = "capability_run_targets"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_capability_run_targets_idempotency"),
        UniqueConstraint(
            "capability_run_id",
            "target_type",
            "target_key",
            name="uq_capability_run_targets_target",
        ),
        CheckConstraint(
            "target_type in ('country', 'event', 'research_case', 'document_version')",
            name="target_type",
        ),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    capability_run_id: Mapped[int] = mapped_column(
        ID_TYPE,
        ForeignKey("capability_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_key: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    confirmed_by: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ResearchExport(TimestampMixin, Base):
    __tablename__ = "research_exports"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_research_exports_idempotency"),
        CheckConstraint("format in ('markdown', 'json', 'csv')", name="format"),
        Index("ix_research_exports_case_created", "research_case_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    research_case_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("research_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    capability_run_target_id: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("capability_run_targets.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    requested_by: Mapped[int] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_scope: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    manifest_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    rights_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "workflow in ('evidence_synthesis', 'bounded_agent')",
            name="workflow",
        ),
        CheckConstraint(
            "runtime in ('evidence_only', 'codex_local', 'coze_test', 'openai_responses')",
            name="runtime",
        ),
        CheckConstraint(
            "status in ('running', 'succeeded', 'failed')",
            name="status",
        ),
        CheckConstraint(
            "finished_at is null or finished_at >= started_at",
            name="valid_time_range",
        ),
        Index("ix_agent_runs_country_created", "country_iso3", "created_at"),
        Index("ix_agent_runs_conversation_scope", "conversation_id", "scope_type", "scope_key"),
    )

    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    requested_by: Mapped[int | None] = mapped_column(
        ID_TYPE, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    country_iso3: Mapped[str] = mapped_column(String(3), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), index=True)
    scope_type: Mapped[str] = mapped_column(String(24), nullable=False, default="country")
    scope_key: Mapped[str] = mapped_column(String(160), nullable=False, default="COD")
    scope_context: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    workflow: Mapped[str] = mapped_column(String(24), nullable=False)
    runtime: Mapped[str] = mapped_column(String(24), nullable=False)
    model_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    plan: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    tool_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON_TYPE, nullable=False, default=list)
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
