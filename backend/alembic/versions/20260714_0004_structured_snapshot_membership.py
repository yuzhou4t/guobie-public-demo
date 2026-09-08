"""add Comtrade dataset identity and exact snapshot membership

Revision ID: 20260714_0004
Revises: 20260714_0003
Create Date: 2026-07-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260714_0004"
down_revision: str | None = "20260714_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AMBIGUOUS_DATASETS = sa.text(
    """
    select count(*)
    from (
        select observations.dataset_id
        from structured_observations as observations
        left join structured_snapshots as snapshots
            on snapshots.dataset_id = observations.dataset_id
        group by observations.dataset_id
        having count(distinct snapshots.id) <> 1
    ) as ambiguous_datasets
    """
)

_AMBIGUOUS_OBSERVATIONS = sa.text(
    """
    select count(*)
    from (
        select observations.id
        from structured_observations as observations
        left join structured_observation_versions as versions
            on versions.observation_id = observations.id
        left join structured_snapshots as snapshots
            on snapshots.id = versions.snapshot_id
            and snapshots.dataset_id = observations.dataset_id
        group by observations.id
        having count(versions.id) <> 1 or count(snapshots.id) <> 1
    ) as ambiguous_observations
    """
)


def _ensure_backfill_is_unambiguous() -> None:
    connection = op.get_bind()
    ambiguous_dataset_count = connection.execute(_AMBIGUOUS_DATASETS).scalar_one()
    ambiguous_observation_count = connection.execute(_AMBIGUOUS_OBSERVATIONS).scalar_one()
    if ambiguous_dataset_count or ambiguous_observation_count:
        raise RuntimeError(
            "cannot reconstruct exact structured snapshot membership; "
            "each populated dataset must have exactly one snapshot and one matching version per observation"
        )


def upgrade() -> None:
    op.add_column(
        "structured_observations",
        sa.Column("source_dataset_code", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_structured_observation_versions_observation_id_id",
        "structured_observation_versions",
        ["observation_id", "id"],
    )
    op.create_table(
        "structured_snapshot_observations",
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("observation_id", sa.BigInteger(), nullable=False),
        sa.Column("observation_version_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["structured_snapshots.id"],
            name=op.f("fk_structured_snapshot_observations_snapshot_id_structured_snapshots"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "observation_version_id"],
            [
                "structured_observation_versions.observation_id",
                "structured_observation_versions.id",
            ],
            name=op.f("fk_structured_snapshot_observations_observation_id_structured_observation_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id",
            "observation_id",
            name=op.f("pk_structured_snapshot_observations"),
        ),
    )
    op.create_index(
        "ix_structured_snapshot_observations_observation_version_id",
        "structured_snapshot_observations",
        ["observation_version_id"],
        unique=False,
    )

    _ensure_backfill_is_unambiguous()
    op.execute(
        sa.text(
            """
            insert into structured_snapshot_observations (
                snapshot_id,
                observation_id,
                observation_version_id
            )
            select snapshot_id, observation_id, id
            from structured_observation_versions
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_structured_snapshot_observations_observation_version_id",
        table_name="structured_snapshot_observations",
    )
    op.drop_table("structured_snapshot_observations")
    op.drop_constraint(
        "uq_structured_observation_versions_observation_id_id",
        "structured_observation_versions",
        type_="unique",
    )
    op.drop_column("structured_observations", "source_dataset_code")
