"""allow commodity-dimension tariff observations

Revision ID: 20260715_0005
Revises: 20260714_0004
Create Date: 2026-07-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260715_0005"
down_revision: str | None = "20260714_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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

_WITS_DIMENSIONS_CHECK = (
    f"{_COMTRADE_DIMENSIONS_CHECK} "
    "or (indicator_code is not null and length(trim(indicator_code)) > 0 "
    "and partner_iso3 is not null and (commodity_classification is null "
    "or length(trim(commodity_classification)) > 0) and commodity_code is not null "
    "and length(trim(commodity_code)) > 0 and trade_flow is null "
    "and partner2_code is null and customs_code is null and mot_code is null)"
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
        _WITS_DIMENSIONS_CHECK,
    )


def downgrade() -> None:
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
