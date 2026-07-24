"""agent_runs + agent_run_events — general agent runtime durable state (R2).

One row per active run holds GoalSpec/TemporalSpec/current NextAction/last tool result/active decision, so
execution state survives messages/restarts/corrections without reconstructing from chat. tenant_or_worker
RLS (a resuming worker writes across users for a linked user) + FORCE RLS. Conditional-create (0001 runs
create_all on a fresh DB).

Revision ID: 0023_agent_runs
Revises: 0022_user_world_state
Create Date: 2026-07-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0023_agent_runs"
down_revision = "0022_user_world_state"
branch_labels = None
depends_on = None
APP_ROLE = "bruce_app"
_TABLES = ("agent_runs", "agent_run_events")


def _rls(table: str) -> None:
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    if "tenant_or_worker" not in policies:
        op.execute(f"CREATE POLICY tenant_or_worker ON {table} "
                   f"USING (user_id = app_current_user() OR app_is_worker()) "
                   f"WITH CHECK (user_id = app_current_user() OR app_is_worker())")


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    JSONB = sa.dialects.postgresql.JSONB
    present = set(sa.inspect(op.get_bind()).get_table_names())

    def _base():
        return [
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        ]

    if "agent_runs" not in present:
        op.create_table("agent_runs", *_base(),
            sa.Column("conversation_id", UUID(as_uuid=True), nullable=True),
            sa.Column("mission_id", UUID(as_uuid=True), sa.ForeignKey("missions.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("domain", sa.String(32), nullable=False, server_default="calendar"),
            sa.Column("status", sa.String(32), nullable=False, server_default="understanding"),
            sa.Column("goal", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("temporal", JSONB, nullable=True),
            sa.Column("selected_entity_id", UUID(as_uuid=True), nullable=True),
            sa.Column("selected_provider_account", sa.String(320), nullable=True),
            sa.Column("current_action", JSONB, nullable=True),
            sa.Column("last_tool_result", JSONB, nullable=True),
            sa.Column("verification_result", JSONB, nullable=True),
            sa.Column("active_decision", JSONB, nullable=True),
            sa.Column("recovery_state", JSONB, nullable=True),
            sa.Column("blocked_reason", sa.String(200), nullable=True),
            sa.Column("idempotency_key", sa.String(200), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("user_id", "idempotency_key", name="uq_agent_run_idem"))

    if "agent_run_events" not in present:
        op.create_table("agent_run_events", *_base(),
            sa.Column("agent_run_id", UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

    for t in _TABLES:
        _rls(t)


def downgrade() -> None:
    for t in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_or_worker ON {t}")
        op.execute(f"DROP TABLE IF EXISTS {t}")
