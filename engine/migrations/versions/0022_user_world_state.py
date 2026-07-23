"""user_world_state — runtime lane opener (R3): per-user world state (timezone/preferences).

One tenant-isolated row per user backing the general agent runtime's UserWorldState. Starts with the
IANA timezone (stored canonically, never the abbreviation) so temporal resolution + calendar writes use
the student's real zone instead of a hard-coded default. tenant_isolation RLS (a user reads/writes only
their own) + FORCE ROW LEVEL SECURITY. Conditional-create because 0001 runs create_all() on a fresh DB.

Revision ID: 0022_user_world_state
Revises: 0019_oauth_states_worker_rls
Create Date: 2026-07-23
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_user_world_state"
down_revision = "0019_oauth_states_worker_rls"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
_TABLE = "user_world_state"


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
        op.create_table(
            _TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("timezone", sa.String(64), nullable=True),
            sa.Column("timezone_source", sa.String(32), nullable=True),
            sa.Column("locale", sa.String(16), nullable=True),
            sa.Column("preferences", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("user_id", name="uq_user_world_state_user"))
    _rls()


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
