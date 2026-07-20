"""relay control-plane audit (Bite 1.5 A1 hardening) — append-only, worker-only

Adds ``relay_control_audit``: an append-only record of every relay control-plane CHANGE (global
pause/resume, per-device pause/resume/stop) with actor / action / environment / device / reason /
previous_state / new_state / timestamp — so an emergency stop or a device pause is always attributable.

Same keystone trap fix as 0013/0014: 0001 runs create_all() and would build the table RLS-OFF, and
bruce_app holds a blanket DML grant (0002), so RLS-OFF == default-ALLOW for the app role. Therefore
ENABLE + FORCE ROW LEVEL SECURITY + explicit GRANT are applied UNCONDITIONALLY and idempotently, SEPARATE
from the pg_policies-keyed CREATE POLICY guard. Append-only is enforced two ways (like capability_audit in
0013): worker SELECT + INSERT policies with NO update/delete policy (RLS denies mutation for the app role),
PLUS a BEFORE UPDATE/DELETE trigger that raises (so it holds even for a BYPASSRLS/owner role). TRUNCATE is
exempt from the trigger, so the test harness can still truncate between tests.

Revision ID: 0015_relay_control_audit
Revises: 0014_relay_control
Create Date: 2026-07-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_relay_control_audit"
down_revision = "0014_relay_control"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
UUID = sa.dialects.postgresql.UUID
JSONB = sa.dialects.postgresql.JSONB
AUDIT_TABLE = "relay_control_audit"

# BEFORE UPDATE/DELETE guard: append-only even for a BYPASSRLS/owner role.
_GUARD_FN = """
CREATE OR REPLACE FUNCTION relay_control_audit_no_mutate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'relay_control_audit is append-only (% blocked)', TG_OP;
END $$;
"""


def _insp():
    return sa.inspect(op.get_bind())


def _has_policy(table: str, name: str) -> bool:
    return name in op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()


def upgrade() -> None:
    present = set(_insp().get_table_names())

    # Conditional CREATE: 0001 create_all builds it on a fresh DB; ALTER path skips.
    if AUDIT_TABLE not in present:
        op.create_table(
            AUDIT_TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("actor", sa.String(200), nullable=True),
            sa.Column("action", sa.String(32), nullable=False),
            sa.Column("environment", sa.String(24), nullable=False),
            sa.Column("device_id", UUID(as_uuid=True),
                      sa.ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reason", sa.String(200), nullable=True),
            sa.Column("previous_state", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("new_state", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

    # RLS: UNCONDITIONAL + idempotent ENABLE/FORCE (+ explicit grant), SEPARATE from the pg_policies
    # guard below (same trap 0013/0014 fixed). create_all builds it RLS-OFF -> default-ALLOW otherwise.
    op.execute(f"ALTER TABLE {AUDIT_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {AUDIT_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {AUDIT_TABLE} TO {APP_ROLE}")

    # Append-only: worker SELECT + INSERT only (no update/delete policy -> RLS denies for the app role).
    if not _has_policy(AUDIT_TABLE, "audit_worker_select"):
        op.execute(f"CREATE POLICY audit_worker_select ON {AUDIT_TABLE} FOR SELECT USING (app_is_worker())")
    if not _has_policy(AUDIT_TABLE, "audit_worker_insert"):
        op.execute(f"CREATE POLICY audit_worker_insert ON {AUDIT_TABLE} FOR INSERT WITH CHECK (app_is_worker())")

    # ... plus a BEFORE UPDATE/DELETE trigger that raises -> append-only even for a BYPASSRLS/owner.
    op.execute(_GUARD_FN)
    op.execute(f"DROP TRIGGER IF EXISTS relay_control_audit_append_only ON {AUDIT_TABLE}")
    op.execute(f"CREATE TRIGGER relay_control_audit_append_only BEFORE UPDATE OR DELETE ON {AUDIT_TABLE} "
               f"FOR EACH ROW EXECUTE FUNCTION relay_control_audit_no_mutate()")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS relay_control_audit_append_only ON {AUDIT_TABLE}")
    for p in ("audit_worker_select", "audit_worker_insert"):
        op.execute(f"DROP POLICY IF EXISTS {p} ON {AUDIT_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {AUDIT_TABLE} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS relay_control_audit_no_mutate()")
