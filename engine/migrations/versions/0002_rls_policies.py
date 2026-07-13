"""row-level security: ENABLE+FORCE RLS, USING+WITH CHECK policies, least-privilege app grants

Revision ID: 0002_rls
Revises: 0001_initial
Create Date: 2026-07-13
"""
from alembic import op

revision = "0002_rls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"

# user-owned tables keyed on a user_id column
USER_ID_TABLES = [
    "sources", "source_spans", "opportunities", "tasks", "calendar_proposals",
    "briefs", "missions", "mission_phase_events", "approvals", "receipts",
    "model_costs", "audit_events",
]

# app.user_id -> uuid, but malformed/missing yields NULL (policy denies), never an exception.
_FN = """
CREATE OR REPLACE FUNCTION app_current_user() RETURNS uuid
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN nullif(current_setting('app.user_id', true), '')::uuid;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;
"""


def _enable(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.execute(_FN)

    # users: a row is visible/writable only to itself (id == authenticated subject)
    _enable("users")
    op.execute(
        "CREATE POLICY tenant_self ON users "
        "USING (id = app_current_user()) WITH CHECK (id = app_current_user())"
    )

    # every other user-owned table: scoped on user_id, for BOTH reads (USING) and writes (WITH CHECK)
    for t in USER_ID_TABLES:
        _enable(t)
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())"
        )

    # least-privilege for the app role: DML only, no DDL/CREATE, no ownership, no BYPASSRLS.
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {APP_ROLE}")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute("DROP POLICY IF EXISTS tenant_self ON users")
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users DISABLE ROW LEVEL SECURITY")
    for t in USER_ID_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS app_current_user()")
