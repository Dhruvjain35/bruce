"""oauth_states: tenant_or_worker RLS so the OAuth callback can consume a state PRE-IDENTITY.

The callback receives an unguessable single-use `state` and must find its row + the user it belongs to
BEFORE any user context exists. oauth_states was `tenant_isolation` (user_id = app_current_user()), and
_consume_state read it via a raw "owner" asyncpg connection ASSUMING that bypasses RLS. On managed Cloud
SQL the owner role is NOT BYPASSRLS, so with no user context app_current_user() is NULL and the callback's
UPDATE matched ZERO rows -> every real OAuth callback failed with InvalidState (proven live).

Fix: give oauth_states the SAME tenant_or_worker policy the messaging / conversation-graph tables use, and
consume the state under a worker_session (app.worker). Security is unchanged — the state is a 48-byte
random token, single-use (consumed_at) and expiring; a user still reads only their own rows.
"""

from alembic import op

revision = "0019_oauth_states_worker_rls"
down_revision = "0018_conversation_context_graph"
branch_labels = None
depends_on = None

_TABLE = "oauth_states"


def _policies() -> set[str]:
    return {r[0] for r in op.get_bind().exec_driver_sql(
        f"SELECT policyname FROM pg_policies WHERE tablename = '{_TABLE}'").fetchall()}


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    if "tenant_or_worker" not in _policies():
        op.execute(f"CREATE POLICY tenant_or_worker ON {_TABLE} "
                   f"USING (user_id = app_current_user() OR app_is_worker()) "
                   f"WITH CHECK (user_id = app_current_user() OR app_is_worker())")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_or_worker ON {_TABLE}")
    if "tenant_isolation" not in _policies():
        op.execute(f"CREATE POLICY tenant_isolation ON {_TABLE} "
                   f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())")
