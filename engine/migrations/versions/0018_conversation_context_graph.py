"""conversation_context_graph — Bite 2 A2 ConversationContextGraph persistence

Four THIN user-owned tables that graph the EXISTING content rows (inbound_messages / conversation_turns
/ outbound_messages / message_attachments / messaging_identities) without duplicating text or bytes:
conversation_messages (canonical node, globally unique (provider, provider_message_id)),
conversation_message_relationships (reply/thread/edit edges + unresolved-target reconciliation),
conversation_message_attachments (join to existing attachment rows), conversation_reaction_events.

tenant_or_worker RLS (the worker ingestion path writes across users for a LINKED user; a user reads only
their own graph) + FORCE ROW LEVEL SECURITY on every table. Conditional-create because 0001 runs
Base.metadata.create_all() and would otherwise build these policy-less on a fresh DB (a cross-tenant hole
for message-relationship data). Account-deletion erasure is inherited from the users.id CASCADE.

Revision ID: 0018_conversation_context_graph
Revises: 0017_magic_link_single_use
Create Date: 2026-07-22
"""
import sqlalchemy as sa
from alembic import op

revision = "0018_conversation_context_graph"
down_revision = "0017_magic_link_single_use"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
_TABLES = ("conversation_messages", "conversation_message_relationships",
           "conversation_message_attachments", "conversation_reaction_events")


def _rls(table: str) -> None:
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    if "tenant_or_worker" not in policies:
        op.execute(f"CREATE POLICY tenant_or_worker ON {table} "
                   f"USING (user_id = app_current_user() OR app_is_worker()) "
                   f"WITH CHECK (user_id = app_current_user() OR app_is_worker())")


def _index(insp, present, table, name, cols) -> None:
    have = {i["name"] for i in insp.get_indexes(table)} if table in present else set()
    if name not in have:
        op.create_index(name, table, cols)


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    insp = sa.inspect(op.get_bind())
    present = set(insp.get_table_names())

    def _fk(target, ondelete):
        return sa.ForeignKey(target, ondelete=ondelete)

    def _base():
        return [
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), _fk("users.id", "CASCADE"), nullable=False, index=True),
        ]

    def _ts():
        return [
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        ]

    if "conversation_messages" not in present:
        op.create_table("conversation_messages", *_base(),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("provider_message_id", sa.String(255), nullable=False),
            sa.Column("provider_chat_id", sa.String(255), nullable=True),
            sa.Column("sender_identity_id", UUID(as_uuid=True), _fk("messaging_identities.id", "SET NULL"), nullable=True),
            sa.Column("direction", sa.String(16), nullable=False),
            sa.Column("service", sa.String(16), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("inbound_message_id", UUID(as_uuid=True), _fk("inbound_messages.id", "SET NULL"), nullable=True),
            sa.Column("conversation_turn_id", UUID(as_uuid=True), _fk("conversation_turns.id", "SET NULL"), nullable=True),
            sa.Column("outbound_message_id", UUID(as_uuid=True), _fk("outbound_messages.id", "SET NULL"), nullable=True),
            sa.Column("source_evidence_id", UUID(as_uuid=True), _fk("sources.id", "SET NULL"), nullable=True),
            sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("unsent_at", sa.DateTime(timezone=True), nullable=True),
            *_ts(),
            sa.UniqueConstraint("provider", "provider_message_id", name="uq_conv_msg_provider"))

    if "conversation_message_relationships" not in present:
        op.create_table("conversation_message_relationships", *_base(),
            sa.Column("source_message_id", UUID(as_uuid=True), _fk("conversation_messages.id", "CASCADE"), nullable=False, index=True),
            sa.Column("target_message_id", UUID(as_uuid=True), _fk("conversation_messages.id", "SET NULL"), nullable=True),
            sa.Column("unresolved_target_provider_message_id", sa.String(255), nullable=True),
            sa.Column("relationship_type", sa.String(32), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("source_evidence_id", UUID(as_uuid=True), _fk("sources.id", "SET NULL"), nullable=True),
            *_ts(),
            sa.UniqueConstraint("source_message_id", "relationship_type", name="uq_conv_rel_source_type"))

    if "conversation_message_attachments" not in present:
        op.create_table("conversation_message_attachments", *_base(),
            sa.Column("message_id", UUID(as_uuid=True), _fk("conversation_messages.id", "CASCADE"), nullable=False, index=True),
            sa.Column("attachment_id", UUID(as_uuid=True), _fk("message_attachments.id", "CASCADE"), nullable=False),
            sa.Column("relationship", sa.String(16), nullable=False, server_default="attached"),
            sa.Column("ordinal", sa.Integer, nullable=False, server_default=sa.text("0")),
            *_ts(),
            sa.UniqueConstraint("message_id", "attachment_id", "relationship", name="uq_conv_msg_att"))

    if "conversation_reaction_events" not in present:
        op.create_table("conversation_reaction_events", *_base(),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("provider_event_id", sa.String(255), nullable=False),
            sa.Column("actor_identity_id", UUID(as_uuid=True), _fk("messaging_identities.id", "SET NULL"), nullable=True),
            sa.Column("target_message_id", UUID(as_uuid=True), _fk("conversation_messages.id", "SET NULL"), nullable=True),
            sa.Column("unresolved_target_provider_message_id", sa.String(255), nullable=True),
            sa.Column("reaction_type", sa.String(16), nullable=False),
            sa.Column("removed", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
            *_ts(),
            sa.UniqueConstraint("provider", "provider_event_id", name="uq_conv_reaction_event"))

    present = set(sa.inspect(op.get_bind()).get_table_names())
    _index(insp, present, "conversation_messages", "ix_conv_messages_owner_chat_time",
           ["user_id", "provider_chat_id", "received_at"])
    _index(insp, present, "conversation_message_relationships", "ix_conv_rel_unresolved",
           ["user_id", "unresolved_target_provider_message_id"])
    _index(insp, present, "conversation_reaction_events", "ix_conv_reaction_target",
           ["user_id", "target_message_id"])

    for t in _TABLES:
        _rls(t)


def downgrade() -> None:
    # ROLLBACK COMPATIBILITY (A3): a destructive downgrade must NOT silently drop a POPULATED
    # ConversationContextGraph. Once A3 writes live reply context, this is not a normal rollback — it
    # needs an explicit, approved, privacy-safe action. Fail closed unless BRUCE_ALLOW_GRAPH_DROP=1.
    import os
    bind = op.get_bind()
    if "conversation_messages" in set(sa.inspect(bind).get_table_names()):
        try:
            n = bind.execute(sa.text("SELECT count(*) FROM conversation_messages")).scalar() or 0
        except Exception:
            n = 0
        if n and os.environ.get("BRUCE_ALLOW_GRAPH_DROP") != "1":
            raise RuntimeError(
                f"refusing to drop a POPULATED ConversationContextGraph ({n} rows): this is a destructive "
                "rollback, not a normal one. Take a privacy-safe backup and set BRUCE_ALLOW_GRAPH_DROP=1 "
                "to authorize.")
    # Drop children before parents (FK order); policies drop with the table but be explicit.
    for t in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_or_worker ON {t}")
        op.execute(f"DROP TABLE IF EXISTS {t}")
