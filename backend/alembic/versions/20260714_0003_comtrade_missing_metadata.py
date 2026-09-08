"""allow explicit missing Comtrade classification and unit metadata

Revision ID: 20260714_0003
Revises: 20260714_0002
Create Date: 2026-07-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260714_0003"
down_revision: str | None = "20260714_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ORIGINAL_DIMENSIONS_CHECK = (
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
    "and mot_code is not null and length(trim(mot_code)) > 0)"
)

_COMTRADE_DIMENSIONS_CHECK = (
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
    "and mot_code is not null and length(trim(mot_code)) > 0)"
)


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_structured_observations_dimensions"),
        "structured_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_observations_dimensions"),
        "structured_observations",
        _COMTRADE_DIMENSIONS_CHECK,
    )

    op.drop_constraint(
        op.f("ck_structured_observation_versions_unit_nonempty"),
        "structured_observation_versions",
        type_="check",
    )
    op.alter_column(
        "structured_observation_versions",
        "unit",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.create_check_constraint(
        op.f("ck_structured_observation_versions_unit_valid"),
        "structured_observation_versions",
        "unit is null or length(trim(unit)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_structured_observation_versions_valued_unit_required"),
        "structured_observation_versions",
        "value is null or unit is not null",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_structured_observation_versions_valued_unit_required"),
        "structured_observation_versions",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_structured_observation_versions_unit_valid"),
        "structured_observation_versions",
        type_="check",
    )
    op.alter_column(
        "structured_observation_versions",
        "unit",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_structured_observation_versions_unit_nonempty"),
        "structured_observation_versions",
        "length(trim(unit)) > 0",
    )

    op.drop_constraint(
        op.f("ck_structured_observations_dimensions"),
        "structured_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_observations_dimensions"),
        "structured_observations",
        _ORIGINAL_DIMENSIONS_CHECK,
    )
