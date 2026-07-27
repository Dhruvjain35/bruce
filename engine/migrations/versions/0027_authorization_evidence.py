"""authorization_evidence + authorization_refusals — durable consent.

#120 made authorization mandatory but turn-scoped: it was minted and spent inside one call, which is
safe (it cannot go stale) and useless to a background mission, because a mission executes minutes or days
after the turn that planned it. `MissionExecutor` therefore refused every write. This is the storage that
lets a mission carry an authorization_id across the wait and have the executor RELOAD and RECHECK it,
rather than carrying a copied boolean that was true once.

Two tables, because "no later refusal" cannot be answered by the evidence alone. A refusal that arrives
while nothing is outstanding leaves no trace on any authorization row, and the next authorization created
after it is legitimate — so the refusal has to be recorded on its own timeline and compared against the
authorization's own timestamp at execution.

Append-mostly by design. `invalidated_at`, `superseded_by_authorization_id` and `consumed_at` are the only
mutable columns, and each one is a one-way transition: an authorization's history must not be editable
after the fact, or the audit trail proves nothing.

Revision ID: 0027_authorization_evidence
Revises: 0026_gmail_sent_ledger
Create Date: 2026-07-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0027_authorization_evidence"
down_revision = "0026_gmail_sent_ledger"
branch_labels = None
depends_on = None
APP_ROLE = "bruce_app"
_EVIDENCE = "authorization_evidence"
_REFUSALS = "authorization_refusals"


def _rls(table: str) -> None:
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")
    if "tenant_isolation" not in policies:
        op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                   f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())")


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    JSONB = sa.dialects.postgresql.JSONB
    present = set(sa.inspect(op.get_bind()).get_table_names())

    if _EVIDENCE not in present:
        op.create_table(
            _EVIDENCE,
            sa.Column("authorization_id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("conversation_id", sa.String(255), nullable=True, index=True),
            sa.Column("trusted_message_id", sa.String(255), nullable=True),
            sa.Column("source_message_timestamp", sa.DateTime(timezone=True), nullable=False),
            # Decision + mission linkage: what the student was answering, and which durable run may spend
            # it. A mission carries only this id; it never carries a copy of the verdict.
            sa.Column("decision_id", sa.String(255), nullable=True, index=True),
            sa.Column("mission_id", UUID(as_uuid=True), nullable=True, index=True),
            sa.Column("agent_run_id", UUID(as_uuid=True), nullable=True, index=True),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("operation", sa.String(64), nullable=False),
            sa.Column("normalized_arguments", JSONB, nullable=False),
            # The binding. Indexed with (user, provider, operation) because the hot query at execution is
            # "is there a live authorization for exactly this".
            sa.Column("arguments_fingerprint", sa.String(64), nullable=False),
            sa.Column("polarity", sa.String(24), nullable=False),
            sa.Column("authorization_type", sa.String(32), nullable=False),
            sa.Column("explicit_operation_request", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("confidence", sa.Float, nullable=False, server_default=sa.text("1.0")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("invalidated_by_message_id", sa.String(255), nullable=True),
            sa.Column("superseded_by_authorization_id", UUID(as_uuid=True), nullable=True),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("consumed_by_attempt", sa.String(255), nullable=True),
            sa.Column("operation_receipt_id", sa.String(255), nullable=True),
        )
        op.create_index("ix_authz_live_lookup", _EVIDENCE,
                        ["user_id", "provider", "operation", "arguments_fingerprint"])
    _rls(_EVIDENCE)

    if _REFUSALS not in present:
        # The refusal timeline. One row every time the deterministic boundary blocks a turn, whether or
        # not anything was outstanding at the time. Execution compares an authorization's own timestamp
        # against this, so a refusal cannot be lost merely because it arrived at a quiet moment.
        op.create_table(
            _REFUSALS,
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False),
            sa.Column("refused_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("message_id", sa.String(255), nullable=True),
            sa.Column("polarity", sa.String(24), nullable=False),
            sa.Column("conversation_id", sa.String(255), nullable=True),
        )
        op.create_index("ix_authz_refusal_user_time", _REFUSALS, ["user_id", "refused_at"])
    _rls(_REFUSALS)


def downgrade() -> None:
    op.drop_table(_REFUSALS)
    op.drop_table(_EVIDENCE)
