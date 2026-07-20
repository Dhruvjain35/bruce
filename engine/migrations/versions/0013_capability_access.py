"""capability access model (Bite 1.5 keystone) — DB-backed entitlement / enrollment / kill + audit

Four tables that replace per-user Cloud Run env editing as the conversation-capability access gate:

  * production_account_entitlements — PERSISTENT production access (no expiry).
  * staging_test_enrollments        — TEMPORARY internal enrollment (optional expiry, immediate revoke).
  * capability_global_state         — singleton per (capability, environment): rollout + emergency kill.
  * capability_audit                — append-only audit of every access mutation.

RLS class is ADMIN-WRITE / WORKER-READ, NOT tenant isolation. The three state tables are readable in a
worker_session (the runtime gate reads them ACROSS users) AND an admin_session (operator management), and
writable ONLY in an admin_session — a tenant must never see or change who is entitled, enrolled, or the
kill state. capability_audit is admin INSERT+SELECT only (no UPDATE/DELETE policy) and is additionally
protected by a BEFORE UPDATE/DELETE trigger that raises, so it is append-only even for a BYPASSRLS/owner
role.

bruce_app holds a blanket DML grant (0002 GRANT ... ON ALL TABLES), so a table created with RLS OFF is
default-ALLOW for the app role — a cross-tenant catastrophe. Therefore ENABLE + FORCE ROW LEVEL SECURITY
is applied UNCONDITIONALLY and idempotently on all four tables (the ALTERs are idempotent), SEPARATE from
the pg_policies-keyed guard that only protects the non-idempotent CREATE POLICY. Grants are re-issued
explicitly on the new tables because the 0002 ALL-TABLES grant does not cover tables created later.

app_is_admin() mirrors app_is_worker(): true iff the transaction-local app.admin GUC is 'on' (set only by
db.admin_session in operator code, never from a request). Conditional CREATE TABLE because 0001 runs
Base.metadata.create_all() and would otherwise build these policy-less on a fresh DB. The global-state
singleton for (conversation, <BRUCE_ENV>) is UPSERT-seeded (ON CONFLICT DO NOTHING) so a re-run never
resets a live kill / rollout state. BRUCE_ENV is the SAME single environment source the runtime gate and
the kill CLI resolve, so the seeded row is the one they read.

Revision ID: 0013_capability_access
Revises: 0012_school_connector
Create Date: 2026-07-19
"""
import os

import sqlalchemy as sa
from alembic import op

revision = "0013_capability_access"
down_revision = "0012_school_connector"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
UUID = sa.dialects.postgresql.UUID
JSONB = sa.dialects.postgresql.JSONB

STATE_TABLES = ("production_account_entitlements", "staging_test_enrollments", "capability_global_state")
AUDIT_TABLE = "capability_audit"
ALL_TABLES = STATE_TABLES + (AUDIT_TABLE,)

# app.admin == 'on' -> true, anything else (incl. unset/malformed) -> false. Never raises. Mirrors
# app_is_worker() (migration 0005) exactly — same shape, a different transaction-local GUC.
_ADMIN_FN = """
CREATE OR REPLACE FUNCTION app_is_admin() RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN coalesce(current_setting('app.admin', true), '') = 'on';
EXCEPTION WHEN others THEN
    RETURN false;
END $$;
"""

# capability_audit is append-only: block UPDATE/DELETE at the row level, even for a BYPASSRLS/owner role.
_AUDIT_GUARD_FN = """
CREATE OR REPLACE FUNCTION capability_audit_no_mutate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'capability_audit is append-only (% blocked)', TG_OP;
END $$;
"""


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def _pk():
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _owner():
    return sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)


def _has_policy(table: str, name: str) -> bool:
    return name in op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()


def _env() -> str:
    return (os.environ.get("BRUCE_ENV", "local") or "local").strip() or "local"


def upgrade() -> None:
    present = set(sa.inspect(op.get_bind()).get_table_names())

    op.execute(_ADMIN_FN)

    if "production_account_entitlements" not in present:
        op.create_table(
            "production_account_entitlements", _pk(), _owner(),
            sa.Column("account_status", sa.String(24), nullable=False, server_default="active"),
            sa.Column("plan", sa.String(32), nullable=False, server_default="alpha"),
            sa.Column("messaging_enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("verified_identity", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("entitlement_reason", sa.String(200), nullable=True),
            sa.Column("capability_availability", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            *_ts(),
            sa.UniqueConstraint("user_id", name="uq_prod_entitlement_user"))

    if "staging_test_enrollments" not in present:
        op.create_table(
            "staging_test_enrollments", _pk(), _owner(),
            sa.Column("capability", sa.String(48), nullable=False),
            sa.Column("environment", sa.String(24), nullable=False),
            sa.Column("enabled_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("enabled_by", sa.String(200), nullable=True),
            sa.Column("audit_reason", sa.String(200), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            *_ts(),
            sa.Index("ix_staging_enrollment_lookup", "user_id", "capability", "environment"))

    if "capability_global_state" not in present:
        op.create_table(
            "capability_global_state", _pk(),
            sa.Column("capability", sa.String(48), nullable=False),
            sa.Column("environment", sa.String(24), nullable=False),
            sa.Column("rollout_state", sa.String(16), nullable=False, server_default="default_off"),
            sa.Column("killed", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("capability", "environment", name="uq_capability_global_state"))

    if AUDIT_TABLE not in present:
        op.create_table(
            AUDIT_TABLE, _pk(),
            sa.Column("actor", sa.String(200), nullable=False),
            sa.Column("action", sa.String(48), nullable=False),
            sa.Column("capability", sa.String(48), nullable=True),
            sa.Column("environment", sa.String(24), nullable=True),
            sa.Column("target_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

    # RLS: UNCONDITIONAL + idempotent ENABLE/FORCE (+ explicit grant) on all four. create_all builds them
    # RLS-OFF, and bruce_app has a blanket DML grant -> RLS-OFF = default-ALLOW = cross-tenant hole. This
    # is SEPARATE from the pg_policies guard below (which only protects the non-idempotent CREATE POLICY).
    for t in ALL_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {APP_ROLE}")

    # admin-write / worker-read on the three state tables (SELECT for worker OR admin; writes admin-only).
    for t in STATE_TABLES:
        if not _has_policy(t, "state_read"):
            op.execute(f"CREATE POLICY state_read ON {t} FOR SELECT USING (app_is_worker() OR app_is_admin())")
        if not _has_policy(t, "state_insert"):
            op.execute(f"CREATE POLICY state_insert ON {t} FOR INSERT WITH CHECK (app_is_admin())")
        if not _has_policy(t, "state_update"):
            op.execute(f"CREATE POLICY state_update ON {t} FOR UPDATE USING (app_is_admin()) WITH CHECK (app_is_admin())")
        if not _has_policy(t, "state_delete"):
            op.execute(f"CREATE POLICY state_delete ON {t} FOR DELETE USING (app_is_admin())")

    # capability_audit: admin INSERT + SELECT only (no UPDATE/DELETE policy -> RLS denies for the app
    # role), plus a BEFORE UPDATE/DELETE trigger that raises -> append-only even for a BYPASSRLS/owner.
    if not _has_policy(AUDIT_TABLE, "audit_select"):
        op.execute(f"CREATE POLICY audit_select ON {AUDIT_TABLE} FOR SELECT USING (app_is_admin())")
    if not _has_policy(AUDIT_TABLE, "audit_insert"):
        op.execute(f"CREATE POLICY audit_insert ON {AUDIT_TABLE} FOR INSERT WITH CHECK (app_is_admin())")
    op.execute(_AUDIT_GUARD_FN)
    op.execute(f"DROP TRIGGER IF EXISTS capability_audit_append_only ON {AUDIT_TABLE}")
    op.execute(f"CREATE TRIGGER capability_audit_append_only BEFORE UPDATE OR DELETE ON {AUDIT_TABLE} "
               f"FOR EACH ROW EXECUTE FUNCTION capability_audit_no_mutate()")

    # UPSERT-seed the global-state singleton for (conversation, <env>) without clobbering a live state.
    # The seed INSERT is subject to FORCE RLS. On a DB where the migration role is NOT a superuser (e.g.
    # Cloud SQL's `postgres`, which lacks BYPASSRLS), FORCE RLS applies to the owner too, so the
    # state_insert WITH CHECK (app_is_admin()) would reject the seed. Set app.admin='on' transaction-
    # locally around the seed to satisfy it, then reset. (On a superuser migration role — local/CI — RLS
    # is bypassed entirely, so this is a harmless no-op.)
    bind = op.get_bind()
    bind.execute(sa.text("SELECT set_config('app.admin', 'on', true)"))
    bind.execute(
        sa.text("INSERT INTO capability_global_state (capability, environment, rollout_state, killed) "
                "VALUES ('conversation', :env, 'default_off', false) "
                "ON CONFLICT (capability, environment) DO NOTHING"),
        {"env": _env()})
    bind.execute(sa.text("SELECT set_config('app.admin', '', true)"))


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS capability_audit_append_only ON {AUDIT_TABLE}")
    for t in STATE_TABLES:
        for p in ("state_read", "state_insert", "state_update", "state_delete"):
            op.execute(f"DROP POLICY IF EXISTS {p} ON {t}")
    for p in ("audit_select", "audit_insert"):
        op.execute(f"DROP POLICY IF EXISTS {p} ON {AUDIT_TABLE}")
    for t in reversed(ALL_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS capability_audit_no_mutate()")
    op.execute("DROP FUNCTION IF EXISTS app_is_admin()")
