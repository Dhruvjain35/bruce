"""relay device bootstrap (Bite 1.5 A4 gap 1) — short-lived single-use registration tokens + audit

The installer registers the relay device over an AUTHENTICATED, short-lived, single-use BOOTSTRAP token
(operator-minted), and the permanent device credential moves straight into the Mac Keychain — never shown.
Two worker-only tables:

  * relay_bootstrap_tokens  — a short-lived, single-use token bound to (environment, intended device
                              name). Only the sha256 HASH is stored. Consumed on first successful use.
  * relay_registration_audit — append-only audit of mint/register/rotate/revoke/deny with actor /
                              environment / device / result / time. Never stores a secret.

Same keystone trap fix as 0013/0014/0015: unconditional ENABLE+FORCE RLS + explicit GRANT (create_all
builds RLS-OFF; bruce_app has a blanket DML grant), separate from the pg_policies CREATE-POLICY guard.
Worker-only RLS (infra, like relay_devices). The audit is append-only (worker SELECT+INSERT, no
update/delete policy, plus a BEFORE UPDATE/DELETE trigger).

Revision ID: 0016_relay_bootstrap
Revises: 0015_relay_control_audit
Create Date: 2026-07-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_relay_bootstrap"
down_revision = "0015_relay_control_audit"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
UUID = sa.dialects.postgresql.UUID
JSONB = sa.dialects.postgresql.JSONB
TOKENS = "relay_bootstrap_tokens"
AUDIT = "relay_registration_audit"

_GUARD_FN = """
CREATE OR REPLACE FUNCTION relay_registration_audit_no_mutate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'relay_registration_audit is append-only (% blocked)', TG_OP;
END $$;
"""


def _insp():
    return sa.inspect(op.get_bind())


def _has_policy(table: str, name: str) -> bool:
    return name in op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()


def upgrade() -> None:
    present = set(_insp().get_table_names())

    if TOKENS not in present:
        op.create_table(
            TOKENS,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("token_hash", sa.String(64), nullable=False, index=True),   # sha256 of the bootstrap token
            sa.Column("environment", sa.String(24), nullable=False),              # binds the token to an env
            sa.Column("device_name", sa.String(120), nullable=False),             # binds it to the intended device
            sa.Column("max_uses", sa.Integer, nullable=False, server_default=sa.text("1")),  # single-use
            sa.Column("used_count", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),  # short-lived
            sa.Column("consumed", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("created_by", sa.String(200), nullable=True),               # server-derived operator actor
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("token_hash", name="uq_relay_bootstrap_token_hash"))

    if AUDIT not in present:
        op.create_table(
            AUDIT,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("actor", sa.String(200), nullable=True),
            sa.Column("action", sa.String(32), nullable=False),   # mint|register|rotate|revoke|deny|rate_limited|replay
            sa.Column("environment", sa.String(24), nullable=False),
            sa.Column("device_name", sa.String(120), nullable=True),
            sa.Column("device_id", UUID(as_uuid=True),
                      sa.ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("result", sa.String(24), nullable=False),   # ok|denied
            sa.Column("reason", sa.String(120), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

    for t in (TOKENS, AUDIT):
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO {APP_ROLE}")

    # bootstrap tokens: worker-only (mint + validate/consume run in a worker session).
    if not _has_policy(TOKENS, "worker_only"):
        op.execute(f"CREATE POLICY worker_only ON {TOKENS} USING (app_is_worker()) WITH CHECK (app_is_worker())")

    # registration audit: append-only (worker SELECT + INSERT, no update/delete policy, + trigger).
    if not _has_policy(AUDIT, "audit_worker_select"):
        op.execute(f"CREATE POLICY audit_worker_select ON {AUDIT} FOR SELECT USING (app_is_worker())")
    if not _has_policy(AUDIT, "audit_worker_insert"):
        op.execute(f"CREATE POLICY audit_worker_insert ON {AUDIT} FOR INSERT WITH CHECK (app_is_worker())")
    op.execute(_GUARD_FN)
    op.execute(f"DROP TRIGGER IF EXISTS relay_registration_audit_append_only ON {AUDIT}")
    op.execute(f"CREATE TRIGGER relay_registration_audit_append_only BEFORE UPDATE OR DELETE ON {AUDIT} "
               f"FOR EACH ROW EXECUTE FUNCTION relay_registration_audit_no_mutate()")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS relay_registration_audit_append_only ON {AUDIT}")
    for p in ("audit_worker_select", "audit_worker_insert"):
        op.execute(f"DROP POLICY IF EXISTS {p} ON {AUDIT}")
    op.execute(f"DROP POLICY IF EXISTS worker_only ON {TOKENS}")
    for t in (AUDIT, TOKENS):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS relay_registration_audit_no_mutate()")
