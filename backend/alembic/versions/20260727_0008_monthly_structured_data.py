"""allow reviewed monthly structured datasets

Revision ID: 20260727_0008
Revises: 20260719_0007
Create Date: 2026-07-27

"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260727_0008"
down_revision: str | None = "20260719_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_structured_datasets_frequency"),
        "structured_datasets",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_datasets_frequency"),
        "structured_datasets",
        "frequency in ('annual', 'monthly')",
    )

    op.drop_constraint(
        op.f("ck_structured_observations_frequency"),
        "structured_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_observations_frequency"),
        "structured_observations",
        "frequency in ('annual', 'monthly')",
    )
    op.drop_constraint(
        op.f("ck_structured_observations_period"),
        "structured_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_observations_period"),
        "structured_observations",
        "(frequency = 'annual' and period between 1900 and 2100) "
        "or (frequency = 'monthly' and period between 190001 and 210012 "
        "and period % 100 between 1 and 12)",
    )
    op.drop_constraint(
        op.f("ck_structured_observations_dimensions"),
        "structured_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_observations_dimensions"),
        "structured_observations",
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
        "and partner_iso3 is not null and (commodity_classification is null "
        "or length(trim(commodity_classification)) > 0) and commodity_code is not null "
        "and length(trim(commodity_code)) > 0 and trade_flow is null "
        "and partner2_code is null and customs_code is null and mot_code is null)",
    )
    op.drop_constraint(
        op.f("ck_structured_observations_period"),
        "structured_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_observations_period"),
        "structured_observations",
        "period between 1900 and 2100",
    )
    op.drop_constraint(
        op.f("ck_structured_observations_frequency"),
        "structured_observations",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_observations_frequency"),
        "structured_observations",
        "frequency = 'annual'",
    )
    op.drop_constraint(
        op.f("ck_structured_datasets_frequency"),
        "structured_datasets",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_structured_datasets_frequency"),
        "structured_datasets",
        "frequency = 'annual'",
    )
