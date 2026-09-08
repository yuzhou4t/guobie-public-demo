"""allow reviewed official abstracts without enabling full-text storage

Revision ID: 20260718_0006
Revises: 20260715_0005
Create Date: 2026-07-18

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260718_0006"
down_revision: str | None = "20260715_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_STORAGE_SCOPE_CHECK = "storage_scope in ('none', 'metadata', 'extracted_text', 'full_content')"
_NEW_STORAGE_SCOPE_CHECK = (
    "storage_scope in ('none', 'metadata', 'official_abstract', 'extracted_text', 'full_content')"
)


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_source_channel_policies_storage_scope"),
        "source_channel_policies",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_source_channel_policies_storage_scope"),
        "source_channel_policies",
        _NEW_STORAGE_SCOPE_CHECK,
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "update source_channel_policies "
            "set storage_scope = 'metadata' where storage_scope = 'official_abstract'"
        )
    )
    op.drop_constraint(
        op.f("ck_source_channel_policies_storage_scope"),
        "source_channel_policies",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_source_channel_policies_storage_scope"),
        "source_channel_policies",
        _OLD_STORAGE_SCOPE_CHECK,
    )
