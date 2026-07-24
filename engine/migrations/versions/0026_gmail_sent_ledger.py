"""gmail_sent_ledger — the exactly-once ledger for Gmail sends (Phase G).

Unlike a calendar upsert (client-set id + provider 409), Google assigns the message id and Gmail cannot be
searched by a custom header, so a send's idempotency is enforced HERE: one row per (user, marker), written
after a verified send. A retry looks the marker up and returns the already-sent message id instead of
sending again; a concurrent double-send loses on the UNIQUE constraint. tenant_isolation RLS + FORCE RLS.

Revision ID: 0026_gmail_sent_ledger
Revises: 0025_agent_run_leases
Create Date: 2026-07-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0026_gmail_sent_ledger"
down_revision = "0025_agent_run_leases"
branch_labels = None
depends_on = None
APP_ROLE = "bruce_app"
_TABLE = "gmail_sent_ledger"


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
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if _TABLE not in present:
        op.create_table(_TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("marker", sa.String(64), nullable=False),
            sa.Column("message_id", sa.String(255), nullable=False),
            sa.Column("thread_id", sa.String(255), nullable=True),
            sa.Column("to_addr", sa.String(320), nullable=True),
            sa.Column("subject", sa.String(998), nullable=True),
            sa.Column("agent_run_id", UUID(as_uuid=True), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("user_id", "marker", name="uq_gmail_ledger_user_marker"))
    _rls()


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
