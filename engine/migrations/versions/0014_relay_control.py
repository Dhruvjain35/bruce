"""relay control plane (Bite 1.5 A1) — worker-only outbound kill switch + per-device directives

Adds the SERVER-SIDE authoritative outbound kill for the self-hosted iMessage relay:

  * relay_control  — singleton-per-environment global outbound kill switch (worker-only RLS). When
                     outbound_paused is true for the running BRUCE_ENV, /v1/relay/outbound/claim hands
                     out nothing — a paused fleet can NEVER be given a message to send, enforced in the
                     claim path independent of any relay client.
  * relay_devices  — per-device directive/pause columns + CONTENT-FREE supervisor telemetry
                     (agent_commit = a pinned relay commit; supervisor_seen_at = a liveness stamp).

relay_control rides the SAME keystone trap fix as migration 0013: 0001 runs Base.metadata.create_all()
and would build relay_control RLS-OFF, and bruce_app holds a blanket DML grant (0002 GRANT ... ON ALL
TABLES), so an RLS-OFF table is default-ALLOW for the app role — a tenant could read or flip the kill
switch. Therefore ENABLE + FORCE ROW LEVEL SECURITY + an explicit GRANT are applied UNCONDITIONALLY and
idempotently, SEPARATE from the pg_policies-keyed guard that only protects the non-idempotent CREATE
POLICY. The singleton for the running BRUCE_ENV is UPSERT-seeded (ON CONFLICT DO NOTHING) so a re-run
never resets a live pause. BRUCE_ENV is the SAME single environment source relay_control.py and the
killswitch CLI resolve, so the seeded row is the one they read.

relay_devices is already worker-only (migration 0007); its new columns are added via a conditional ALTER
(create_all adds them on a fresh DB, so guard on the inspector) and inherit the existing worker_only RLS.

Revision ID: 0014_relay_control
Revises: 0013_capability_access
Create Date: 2026-07-19
"""
import os

import sqlalchemy as sa
from alembic import op

revision = "0014_relay_control"
down_revision = "0013_capability_access"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
UUID = sa.dialects.postgresql.UUID

# per-device control-plane columns (name -> factory); ALTER-added when absent (create_all builds them
# on a fresh DB). directive is the intended state; the resolved pause is outbound_paused/paused_*.
_DEVICE_COLS = ("directive", "outbound_paused", "paused_reason", "paused_at",
                "supervisor_seen_at", "agent_commit")


def _insp():
    return sa.inspect(op.get_bind())


def _cols(table: str) -> set[str]:
    return {c["name"] for c in _insp().get_columns(table)} if table in _insp().get_table_names() else set()


def _has_policy(table: str, name: str) -> bool:
    return name in op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()


def _env() -> str:
    return (os.environ.get("BRUCE_ENV", "local") or "local").strip() or "local"


def upgrade() -> None:
    present = set(_insp().get_table_names())

    # Conditional CREATE: 0001 create_all builds relay_control on a fresh DB; ALTER path skips it.
    if "relay_control" not in present:
        op.create_table(
            "relay_control",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("environment", sa.String(24), nullable=False),
            sa.Column("outbound_paused", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("reason", sa.String(200), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("environment", name="uq_relay_control_environment"))

    # RLS: UNCONDITIONAL + idempotent ENABLE/FORCE (+ explicit grant). create_all builds relay_control
    # RLS-OFF and bruce_app has a blanket DML grant -> RLS-OFF == default-ALLOW == a tenant could read/
    # flip the kill switch. This is SEPARATE from the pg_policies guard below, which only protects the
    # non-idempotent CREATE POLICY (same trap migration 0013 fixed for the capability tables).
    op.execute("ALTER TABLE relay_control ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE relay_control FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON relay_control TO {APP_ROLE}")
    if not _has_policy("relay_control", "worker_only"):
        op.execute("CREATE POLICY worker_only ON relay_control USING (app_is_worker()) WITH CHECK (app_is_worker())")

    # relay_devices: per-device directive/pause + content-free supervisor telemetry. create_all adds
    # these on a fresh DB; ALTER on a migrated one. relay_devices already carries worker_only RLS (0007).
    have = _cols("relay_devices")
    with op.batch_alter_table("relay_devices") as b:
        if "directive" not in have:
            b.add_column(sa.Column("directive", sa.String(16), nullable=False, server_default="run"))
        if "outbound_paused" not in have:
            b.add_column(sa.Column("outbound_paused", sa.Boolean, nullable=False, server_default=sa.text("false")))
        if "paused_reason" not in have:
            b.add_column(sa.Column("paused_reason", sa.String(200), nullable=True))
        if "paused_at" not in have:
            b.add_column(sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
        if "supervisor_seen_at" not in have:
            b.add_column(sa.Column("supervisor_seen_at", sa.DateTime(timezone=True), nullable=True))
        if "agent_commit" not in have:
            b.add_column(sa.Column("agent_commit", sa.String(64), nullable=True))

    # UPSERT-seed the singleton for the running BRUCE_ENV without clobbering a live pause.
    op.get_bind().execute(
        sa.text("INSERT INTO relay_control (environment, outbound_paused) VALUES (:env, false) "
                "ON CONFLICT (environment) DO NOTHING"),
        {"env": _env()})


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS worker_only ON relay_control")
    op.execute("DROP TABLE IF EXISTS relay_control CASCADE")
    for c in _DEVICE_COLS:
        op.execute(f"ALTER TABLE relay_devices DROP COLUMN IF EXISTS {c}")
