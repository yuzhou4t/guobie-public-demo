"""allow research skill drafts

Revision ID: 20260828_0015
Revises: 20260827_0014
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0015"
down_revision: str | None = "20260827_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_capability_templates_status"), "capability_templates", type_="check")
    op.create_check_constraint(
        op.f("ck_capability_templates_status"),
        "capability_templates",
        "status in ('draft', 'active', 'retired')",
    )


def downgrade() -> None:
    op.execute("update capability_templates set status = 'retired' where status = 'draft'")
    op.drop_constraint(op.f("ck_capability_templates_status"), "capability_templates", type_="check")
    op.create_check_constraint(
        op.f("ck_capability_templates_status"),
        "capability_templates",
        "status in ('active', 'retired')",
    )
