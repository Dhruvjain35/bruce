"""messaging domain — provider-neutral tables for the messaging-first vertical slice, under RLS

Seven tables (messaging_identities, messaging_conversations, inbound_messages, message_attachments,
outbound_messages, message_delivery_events, account_link_codes). Same worker-or-owner RLS as
intake_jobs: inbound is processed server-side (webhook -> worker) and a channel identity exists
BEFORE it is linked to a user, so a worker session may touch a pre-link row; the app reads only the
user's own once linked. The tenant boundary is still Postgres, not app code.

Conditional (0001 runs create_all against live models) — guard on the inspector, like 0004/0005.

Revision ID: 0006_messaging
Revises: 0005_intake_jobs
Create Date: 2026-07-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_messaging"
down_revision = "0005_intake_jobs"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
TABLES = (
    "messaging_identities", "messaging_conversations", "inbound_messages",
    "message_attachments", "outbound_messages", "message_delivery_events", "account_link_codes",
)


def _existing() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _rls(table: str) -> None:
    """Worker-or-owner: the app sees its own rows; a worker/service session may process pre-link rows.
    app_current_user() + app_is_worker() were created in migrations 0002 / 0005."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_or_worker ON {table} "
        f"USING (user_id = app_current_user() OR app_is_worker()) "
        f"WITH CHECK (user_id = app_current_user() OR app_is_worker())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    present = _existing()
    UUID = sa.dialects.postgresql.UUID

    # 0001's create_all already builds these when the models exist; only create what's missing.
    def col_ts():
        return [
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        ]

    def pk():
        return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))

    def owner(nullable=True):
        return sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=nullable, index=True)

    if "messaging_identities" not in present:
        op.create_table("messaging_identities", pk(), owner(),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False, server_default="linq"),
            sa.Column("channel_identity", sa.String(255), nullable=False),
            sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
            *col_ts(),
            sa.UniqueConstraint("channel", "channel_identity", name="uq_msg_identity"))
    if "messaging_conversations" not in present:
        op.create_table("messaging_conversations", pk(), owner(),
            sa.Column("identity_id", UUID(as_uuid=True), sa.ForeignKey("messaging_identities.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("provider_conversation_id", sa.String(255), nullable=True, index=True),
            sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
            *col_ts())
    if "inbound_messages" not in present:
        op.create_table("inbound_messages", pk(), owner(),
            sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("messaging_conversations.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("provider_message_id", sa.String(255), nullable=True),
            sa.Column("channel_identity", sa.String(255), nullable=False),
            sa.Column("text", sa.Text, nullable=True),
            sa.Column("reply_to_message_id", sa.String(255), nullable=True),
            sa.Column("provider_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="SET NULL"), nullable=True),
            sa.Column("mission_id", UUID(as_uuid=True), sa.ForeignKey("missions.id", ondelete="SET NULL"), nullable=True),
            *col_ts(),
            sa.UniqueConstraint("channel", "provider_message_id", name="uq_inbound_provider_msg"))
    if "message_attachments" not in present:
        op.create_table("message_attachments", pk(), owner(),
            sa.Column("inbound_message_id", UUID(as_uuid=True), sa.ForeignKey("inbound_messages.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("media_type", sa.String(64), nullable=True),
            sa.Column("url", sa.String(1000), nullable=True),
            sa.Column("filename", sa.String(255), nullable=True),
            sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="SET NULL"), nullable=True),
            *col_ts())
    if "outbound_messages" not in present:
        op.create_table("outbound_messages", pk(), owner(),
            sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("messaging_conversations.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("text", sa.Text, nullable=False),
            sa.Column("deep_link", sa.String(500), nullable=True),
            sa.Column("mission_id", UUID(as_uuid=True), sa.ForeignKey("missions.id", ondelete="SET NULL"), nullable=True),
            sa.Column("provider_message_id", sa.String(255), nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            *col_ts(),
            sa.UniqueConstraint("idempotency_key", name="uq_outbound_idem"))
    if "message_delivery_events" not in present:
        op.create_table("message_delivery_events", pk(), owner(),
            sa.Column("outbound_message_id", UUID(as_uuid=True), sa.ForeignKey("outbound_messages.id", ondelete="CASCADE"), nullable=True, index=True),
            sa.Column("event_type", sa.String(24), nullable=False),
            sa.Column("provider_event_id", sa.String(255), nullable=True),
            sa.Column("reason", sa.String(200), nullable=True),
            *col_ts(),
            sa.UniqueConstraint("provider_event_id", name="uq_delivery_event"))
    if "account_link_codes" not in present:
        op.create_table("account_link_codes", pk(), owner(nullable=False),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False, index=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("bound_identity_id", UUID(as_uuid=True), sa.ForeignKey("messaging_identities.id", ondelete="SET NULL"), nullable=True),
            *col_ts())

    # RLS whether or not create_all built the tables (create_all does not create policies).
    for t in TABLES:
        policies = op.get_bind().execute(
            sa.text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": t}
        ).scalars().all()
        if "tenant_or_worker" not in policies:
            _rls(t)


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_or_worker ON {t}")
        op.execute(f"DROP TABLE IF EXISTS {t}")
