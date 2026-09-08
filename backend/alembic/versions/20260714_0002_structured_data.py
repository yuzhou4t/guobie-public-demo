"""add structured datasets and versioned observations

Revision ID: 20260714_0002
Revises: 20260713_0001
Create Date: 2026-07-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260714_0002"
down_revision: str | None = "20260713_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "structured_datasets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("dataset_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("frequency", sa.String(length=16), nullable=False),
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
            "length(trim(dataset_key)) > 0",
            name=op.f("ck_structured_datasets_dataset_key_nonempty"),
        ),
        sa.CheckConstraint(
            "length(trim(scope_id)) > 0",
            name=op.f("ck_structured_datasets_scope_id_nonempty"),
        ),
        sa.CheckConstraint(
            "frequency = 'annual'",
            name=op.f("ck_structured_datasets_frequency"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["source_channels.id"],
            name=op.f("fk_structured_datasets_channel_id_source_channels"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_structured_datasets")),
        sa.UniqueConstraint(
            "channel_id",
            name=op.f("uq_structured_datasets_channel"),
        ),
        sa.UniqueConstraint(
            "dataset_key",
            name=op.f("uq_structured_datasets_key"),
        ),
    )

    op.create_table(
        "structured_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("collection_run_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "query_manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("query_signature", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_version", sa.Text(), nullable=False),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("returned_count", sa.Integer(), nullable=False),
        sa.Column("valued_count", sa.Integer(), nullable=False),
        sa.Column("source_null_count", sa.Integer(), nullable=False),
        sa.Column("not_returned_count", sa.Integer(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "length(query_signature) = 64",
            name=op.f("ck_structured_snapshots_query_signature_length"),
        ),
        sa.CheckConstraint(
            "length(snapshot_hash) = 64",
            name=op.f("ck_structured_snapshots_snapshot_hash_length"),
        ),
        sa.CheckConstraint(
            "expected_count >= 0 and returned_count >= 0 and valued_count >= 0 "
            "and source_null_count >= 0 and not_returned_count >= 0",
            name=op.f("ck_structured_snapshots_counts_nonnegative"),
        ),
        sa.CheckConstraint(
            "expected_count = returned_count + not_returned_count",
            name=op.f("ck_structured_snapshots_expected_count_balance"),
        ),
        sa.CheckConstraint(
            "returned_count = valued_count + source_null_count",
            name=op.f("ck_structured_snapshots_returned_count_balance"),
        ),
        sa.ForeignKeyConstraint(
            ["collection_run_id"],
            ["collection_runs.id"],
            name=op.f("fk_structured_snapshots_collection_run_id_collection_runs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["structured_datasets.id"],
            name=op.f("fk_structured_snapshots_dataset_id_structured_datasets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_structured_snapshots")),
        sa.UniqueConstraint(
            "collection_run_id",
            name=op.f("uq_structured_snapshots_collection_run"),
        ),
    )
    op.create_index(
        op.f("ix_structured_snapshots_dataset_retrieved"),
        "structured_snapshots",
        ["dataset_id", "retrieved_at"],
        unique=False,
    )

    op.create_table(
        "structured_observations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("observation_key", sa.String(length=64), nullable=False),
        sa.Column("country_iso3", sa.String(length=3), nullable=False),
        sa.Column("partner_iso3", sa.String(length=3), nullable=True),
        sa.Column("indicator_code", sa.Text(), nullable=True),
        sa.Column("commodity_classification", sa.Text(), nullable=True),
        sa.Column("commodity_code", sa.Text(), nullable=True),
        sa.Column("trade_flow", sa.String(length=1), nullable=True),
        sa.Column("partner2_code", sa.Text(), nullable=True),
        sa.Column("customs_code", sa.Text(), nullable=True),
        sa.Column("mot_code", sa.Text(), nullable=True),
        sa.Column("frequency", sa.String(length=16), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("metric_code", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest_version_no", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "length(observation_key) = 64",
            name=op.f("ck_structured_observations_observation_key_length"),
        ),
        sa.CheckConstraint(
            "length(country_iso3) = 3",
            name=op.f("ck_structured_observations_country_iso3_length"),
        ),
        sa.CheckConstraint(
            "partner_iso3 is null or length(partner_iso3) = 3",
            name=op.f("ck_structured_observations_partner_iso3_length"),
        ),
        sa.CheckConstraint(
            "frequency = 'annual'",
            name=op.f("ck_structured_observations_frequency"),
        ),
        sa.CheckConstraint(
            "period between 1900 and 2100",
            name=op.f("ck_structured_observations_period"),
        ),
        sa.CheckConstraint(
            "length(trim(metric_code)) > 0",
            name=op.f("ck_structured_observations_metric_code_nonempty"),
        ),
        sa.CheckConstraint(
            "latest_version_no >= 0",
            name=op.f("ck_structured_observations_latest_version_nonnegative"),
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name=op.f("ck_structured_observations_seen_time_range"),
        ),
        sa.CheckConstraint(
            "(indicator_code is not null and length(trim(indicator_code)) > 0 "
            "and partner_iso3 is null and commodity_classification is null "
            "and commodity_code is null and trade_flow is null and partner2_code is null "
            "and customs_code is null and mot_code is null) "
            "or (indicator_code is null and partner_iso3 is not null "
            "and commodity_classification is not null "
            "and length(trim(commodity_classification)) > 0 and commodity_code is not null "
            "and length(trim(commodity_code)) > 0 and trade_flow in ('M', 'X') "
            "and partner2_code is not null and length(trim(partner2_code)) > 0 "
            "and customs_code is not null and length(trim(customs_code)) > 0 "
            "and mot_code is not null and length(trim(mot_code)) > 0)",
            name=op.f("ck_structured_observations_dimensions"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["structured_datasets.id"],
            name=op.f("fk_structured_observations_dataset_id_structured_datasets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_structured_observations")),
        sa.UniqueConstraint(
            "dataset_id",
            "observation_key",
            name=op.f("uq_structured_observations_dataset_key"),
        ),
    )
    op.create_index(
        op.f("ix_structured_observations_dataset_country_period"),
        "structured_observations",
        ["dataset_id", "country_iso3", "period"],
        unique=False,
    )

    op.create_table(
        "structured_observation_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("observation_hash", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("price_basis", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_release_date", sa.Date(), nullable=True),
        sa.Column("source_status", sa.String(length=24), nullable=False),
        sa.Column("missing_reason", sa.Text(), nullable=True),
        sa.Column("is_reported", sa.Boolean(), nullable=True),
        sa.Column("is_aggregate", sa.Boolean(), nullable=True),
        sa.Column(
            "quality_flags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_no > 0",
            name=op.f("ck_structured_observation_versions_version_positive"),
        ),
        sa.CheckConstraint(
            "length(observation_hash) = 64",
            name=op.f("ck_structured_observation_versions_observation_hash_length"),
        ),
        sa.CheckConstraint(
            "length(trim(unit)) > 0",
            name=op.f("ck_structured_observation_versions_unit_nonempty"),
        ),
        sa.CheckConstraint(
            "length(trim(source_url)) > 0",
            name=op.f("ck_structured_observation_versions_source_url_nonempty"),
        ),
        sa.CheckConstraint(
            "length(trim(source_status)) > 0",
            name=op.f("ck_structured_observation_versions_source_status_nonempty"),
        ),
        sa.CheckConstraint(
            "(value is not null and missing_reason is null) "
            "or (value is null and missing_reason is not null "
            "and length(trim(missing_reason)) > 0)",
            name=op.f("ck_structured_observation_versions_value_or_missing"),
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["structured_observations.id"],
            name=op.f("fk_structured_observation_versions_observation_id_structured_observations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["structured_snapshots.id"],
            name=op.f("fk_structured_observation_versions_snapshot_id_structured_snapshots"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_structured_observation_versions")),
        sa.UniqueConstraint(
            "observation_id",
            "version_no",
            name=op.f("uq_structured_observation_versions_number"),
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            "observation_id",
            name=op.f("uq_structured_observation_versions_snapshot_observation"),
        ),
    )


def downgrade() -> None:
    op.drop_table("structured_observation_versions")

    op.drop_index(
        op.f("ix_structured_observations_dataset_country_period"),
        table_name="structured_observations",
    )
    op.drop_table("structured_observations")

    op.drop_index(
        op.f("ix_structured_snapshots_dataset_retrieved"),
        table_name="structured_snapshots",
    )
    op.drop_table("structured_snapshots")

    op.drop_table("structured_datasets")
