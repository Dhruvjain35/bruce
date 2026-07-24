"""calendar_event_entities — canonical verified calendar events (R7).

One row per verified provider event so "move guitar class" / "delete that" resolve to a real entity
instead of re-parsing. tenant_isolation RLS + FORCE RLS. Unique per (user, provider, provider_event_id).

Revision ID: 0024_calendar_event_entities
Revises: 0023_agent_runs
Create Date: 2026-07-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0024_calendar_event_entities"
down_revision = "0023_agent_runs"
branch_labels = None
depends_on = None
APP_ROLE = "bruce_app"
_TABLE = "calendar_event_entities"


def _rls() -> None:
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": _TABLE}).scalars().all()
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {APP_ROLE}")
    if "tenant_isolation" not in policies:
        op.execute(f"CREATE POLICY tenant_isolation ON {_TABLE} "
                   f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())")


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    JSONB = sa.dialects.postgresql.JSONB
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if _TABLE not in present:
        op.create_table(_TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("normalized_title", sa.String(500), nullable=False, index=True),
            sa.Column("start", sa.String(40), nullable=False),
            sa.Column("end", sa.String(40), nullable=True),
            sa.Column("timezone", sa.String(64), nullable=True),
            sa.Column("location", sa.String(500), nullable=True),
            sa.Column("provider", sa.String(32), nullable=False, server_default="google_calendar"),
            sa.Column("provider_account_id", sa.String(320), nullable=True),
            sa.Column("provider_event_id", sa.String(255), nullable=False),
            sa.Column("calendar_id", sa.String(255), nullable=False, server_default="primary"),
            sa.Column("source_message_ids", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("agent_run_id", UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("receipt_id", UUID(as_uuid=True), sa.ForeignKey("receipts.id", ondelete="SET NULL"), nullable=True),
            sa.Column("provider_version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("user_id", "provider", "provider_event_id", name="uq_cal_entity_provider_event"))
    _rls()


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
