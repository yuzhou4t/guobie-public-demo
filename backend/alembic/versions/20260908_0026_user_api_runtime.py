"""Allow guest API runs in the existing auditable research record."""

from alembic import op

revision = "20260908_0026"
down_revision = "20260908_0025"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_constraint(op.f("ck_agent_runs_runtime"), type_="check")
        batch.create_check_constraint(
            op.f("ck_agent_runs_runtime"),
            "runtime in ('evidence_only', 'codex_local', 'coze_test', 'openai_responses', 'user_api')",
        )


def downgrade():
    # Existing user_api rows deliberately prevent a lossy downgrade.
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_constraint(op.f("ck_agent_runs_runtime"), type_="check")
        batch.create_check_constraint(
            op.f("ck_agent_runs_runtime"),
            "runtime in ('evidence_only', 'codex_local', 'coze_test', 'openai_responses')",
        )
