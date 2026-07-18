"""relay_uploads — staged inbound attachment bytes (self-hosted iMessage alpha)

The relay uploads an attachment here (authenticated), the inbound handler consumes it into the
durable intake source, then the bytes are cleared. Worker-only RLS (infra). Conditional create.

Revision ID: 0009_relay_uploads
Revises: 0008_outbound_to_handle
Create Date: 2026-07-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_relay_uploads"
down_revision = "0008_outbound_to_handle"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    insp = sa.inspect(op.get_bind())
    if "relay_uploads" not in insp.get_table_names():
        op.create_table("relay_uploads",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("relay_device_id", UUID(as_uuid=True), sa.ForeignKey("relay_devices.id", ondelete="SET NULL"), nullable=True),
            sa.Column("content_hash", sa.String(64), nullable=False, index=True),
            sa.Column("media_type", sa.String(64), nullable=False),
            sa.Column("filename", sa.String(255), nullable=True),
            sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
            sa.Column("data", sa.LargeBinary, nullable=True),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")))

    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename='relay_uploads'")).scalars().all()
    if "worker_only" not in policies:
        op.execute("ALTER TABLE relay_uploads ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE relay_uploads FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY worker_only ON relay_uploads USING (app_is_worker()) WITH CHECK (app_is_worker())")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON relay_uploads TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS worker_only ON relay_uploads")
    op.execute("DROP TABLE IF EXISTS relay_uploads")
