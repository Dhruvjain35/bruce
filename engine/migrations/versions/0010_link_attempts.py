"""messaging_link_attempts — per-handle brute-force guard for account linking (private-alpha bridge)

A channel identity (phone/email handle) that texts many wrong invite codes is locked out for a
window, independent of the per-code attempt cap. Keyed by handle, not user → worker-only RLS (infra).
Holds no message content. Conditional create (0001 create_all may have made it already).

Revision ID: 0010_link_attempts
Revises: 0009_relay_uploads
Create Date: 2026-07-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_link_attempts"
down_revision = "0009_relay_uploads"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    insp = sa.inspect(op.get_bind())
    if "messaging_link_attempts" not in insp.get_table_names():
        op.create_table("messaging_link_attempts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("channel_identity", sa.String(255), nullable=False),
            sa.Column("failed_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("channel", "channel_identity", name="uq_link_attempt_handle"))

    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename='messaging_link_attempts'")).scalars().all()
    if "worker_only" not in policies:
        op.execute("ALTER TABLE messaging_link_attempts ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE messaging_link_attempts FORCE ROW LEVEL SECURITY")
        op.execute("CREATE POLICY worker_only ON messaging_link_attempts USING (app_is_worker()) WITH CHECK (app_is_worker())")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON messaging_link_attempts TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS worker_only ON messaging_link_attempts")
    op.execute("DROP TABLE IF EXISTS messaging_link_attempts")
