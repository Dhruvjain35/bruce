"""conversation_runtime — conversation_turns + event_candidates (Bite 1 conversation brain)

Two user-owned tables holding the most sensitive student free-text (conversation turns) and reviewed
event candidates. tenant_isolation RLS (USING/WITH CHECK user_id = app_current_user()) because every
write happens under user_session(user_id) for a LINKED user — no worker path touches these. Conditional
create because 0001 runs Base.metadata.create_all() and would otherwise build them policy-less on a
fresh DB (a silent cross-tenant hole for the most sensitive data). event_candidates first — a
conversation_turns FK references it.

Revision ID: 0011_conversation_runtime
Revises: 0010_link_attempts
Create Date: 2026-07-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_conversation_runtime"
down_revision = "0010_link_attempts"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
_TABLES = ("event_candidates", "conversation_turns")


def _rls(table: str) -> None:
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()
    if "tenant_isolation" not in policies:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                   f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    JSONB = sa.dialects.postgresql.JSONB
    insp = sa.inspect(op.get_bind())
    existing = set(insp.get_table_names())

    if "event_candidates" not in existing:
        op.create_table("event_candidates",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("sources.id", ondelete="SET NULL"), nullable=True),
            sa.Column("inbound_message_id", UUID(as_uuid=True), sa.ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("all_day", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("location", sa.String(500), nullable=True),
            sa.Column("confidence", sa.Float, nullable=True),
            sa.Column("missing_fields", JSONB, nullable=True),
            sa.Column("provenance", JSONB, nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="proposed"),
            sa.Column("idempotency_key", sa.String(255), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("user_id", "idempotency_key", name="uq_event_candidate_idem"))

    if "conversation_turns" not in existing:
        op.create_table("conversation_turns",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("channel_identity", sa.String(255), nullable=False),
            sa.Column("provider_message_id", sa.String(255), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("intent", sa.String(40), nullable=True),
            sa.Column("response_type", sa.String(40), nullable=True),
            sa.Column("text", sa.Text, nullable=True),
            sa.Column("decision", JSONB, nullable=True),
            sa.Column("risk_level", sa.String(16), nullable=True),
            sa.Column("confidence", sa.Float, nullable=True),
            sa.Column("mission_id", UUID(as_uuid=True), sa.ForeignKey("missions.id", ondelete="SET NULL"), nullable=True),
            sa.Column("event_candidate_id", UUID(as_uuid=True), sa.ForeignKey("event_candidates.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("user_id", "channel", "provider_message_id", "role", name="uq_turn_msg_role"))

    for t in _TABLES:
        _rls(t)


def downgrade() -> None:
    for t in ("conversation_turns", "event_candidates"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"DROP TABLE IF EXISTS {t}")
