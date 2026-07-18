"""relay devices + delivery attempts + outbound queue lease columns (self-hosted iMessage alpha)

Adds the server-side pieces of the Mac relay boundary:
  - relay_devices     — a dedicated relay's credential HASH (infra, not user-owned); worker-only RLS.
  - delivery_attempts — one outbound delivery attempt (audit; no content); worker-or-owner RLS.
  - outbound_messages — lease columns so the relay claims one message at a time (crash-safe), like
                        intake_jobs. Added via conditional ALTER (create_all builds them on a fresh DB).

Conditional throughout (0001 create_all vs migrated DB), guarding on the inspector.

Revision ID: 0007_relay_devices
Revises: 0006_messaging
Create Date: 2026-07-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_relay_devices"
down_revision = "0006_messaging"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"


def _insp():
    return sa.inspect(op.get_bind())


def _cols(table: str) -> set[str]:
    return {c["name"] for c in _insp().get_columns(table)} if table in _insp().get_table_names() else set()


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    present = set(_insp().get_table_names())

    def ts():
        return [
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        ]
    def pk():
        return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))

    if "relay_devices" not in present:
        op.create_table("relay_devices", pk(),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("channel", sa.String(32), nullable=False, server_default="self_hosted_imessage"),
            sa.Column("credential_hash", sa.String(64), nullable=False, index=True),
            sa.Column("bruce_handle", sa.String(255), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            *ts())

    if "delivery_attempts" not in present:
        op.create_table("delivery_attempts", pk(),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True),
            sa.Column("outbound_message_id", UUID(as_uuid=True), sa.ForeignKey("outbound_messages.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("relay_device_id", UUID(as_uuid=True), sa.ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("attempt_no", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("provider_message_id", sa.String(255), nullable=True),
            sa.Column("error", sa.String(200), nullable=True),
            *ts())

    # outbound_messages lease columns (create_all adds them on a fresh DB; ALTER on a migrated one).
    have = _cols("outbound_messages")
    with op.batch_alter_table("outbound_messages") as b:
        if "lease_owner" not in have:
            b.add_column(sa.Column("lease_owner", sa.String(64), nullable=True))
        if "lease_expires_at" not in have:
            b.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        if "attempts" not in have:
            b.add_column(sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")))
        if "max_attempts" not in have:
            b.add_column(sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("5")))
        if "relay_device_id" not in have:
            b.add_column(sa.Column("relay_device_id", UUID(as_uuid=True), nullable=True))
    idx = {i["name"] for i in _insp().get_indexes("outbound_messages")} if "outbound_messages" in present else set()
    if "ix_outbound_claimable" not in idx:
        op.create_index("ix_outbound_claimable", "outbound_messages", ["status", "lease_expires_at"])

    # RLS: relay_devices is worker-only (infra); delivery_attempts is worker-or-owner.
    def has_policy(table, name):
        return name in op.get_bind().execute(
            sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()

    if not has_policy("relay_devices", "worker_only"):
        op.execute("ALTER TABLE relay_devices ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE relay_devices FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY worker_only ON relay_devices USING (app_is_worker()) WITH CHECK (app_is_worker())")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON relay_devices TO {APP_ROLE}")
    if not has_policy("delivery_attempts", "tenant_or_worker"):
        op.execute("ALTER TABLE delivery_attempts ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE delivery_attempts FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY tenant_or_worker ON delivery_attempts "
                   "USING (user_id = app_current_user() OR app_is_worker()) "
                   "WITH CHECK (user_id = app_current_user() OR app_is_worker())")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON delivery_attempts TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_or_worker ON delivery_attempts")
    op.execute("DROP POLICY IF EXISTS worker_only ON relay_devices")
    op.execute("DROP TABLE IF EXISTS delivery_attempts")
    op.execute("DROP TABLE IF EXISTS relay_devices")
    for c in ("lease_owner", "lease_expires_at", "attempts", "max_attempts", "relay_device_id"):
        op.execute(f"ALTER TABLE outbound_messages DROP COLUMN IF EXISTS {c}")
    op.execute("DROP INDEX IF EXISTS ix_outbound_claimable")
