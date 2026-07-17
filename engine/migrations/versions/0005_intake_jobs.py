"""intake_jobs — durable async intake work with a custom worker-aware RLS policy

Adds the ``intake_jobs`` table that makes intake asynchronous: a request commits the job (plus its
source + mission) and returns 202; a worker claims the job with a lease and does the model work
outside the request lifecycle, so a process restart never loses accepted work.

RLS here is DELIBERATELY different from every other table. The standard tenant_isolation policy
(user_id = app_current_user()) is right for the API status reads, but a background worker must claim
jobs ACROSS users, and it cannot know which users have pending work without first querying past
per-user isolation. So this table's policy also admits a WORKER context:

    USING / WITH CHECK (user_id = app_current_user() OR app_is_worker())

``app_is_worker()`` reads the transaction-local ``app.worker`` setting, which is set ONLY by server
worker code (db.worker_session), NEVER from a request handler and NEVER from user input — so a user
request can never gain worker visibility. The worker uses this only to manage the QUEUE; the actual
content writes (sources/spans/tasks) still happen under user_session(job.user_id), fully scoped.

CONDITIONAL for the same reason as 0003/0004: 0001 runs Base.metadata.create_all() against the live
models, so a fresh DB already has intake_jobs while a migrated one does not. Guard on the inspector.

Revision ID: 0005_intake_jobs
Revises: 0004_integrations
Create Date: 2026-07-17
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_intake_jobs"
down_revision = "0004_integrations"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
TABLE = "intake_jobs"

# app.worker == 'on' -> true, anything else (incl. unset/malformed) -> false. Never raises.
_WORKER_FN = """
CREATE OR REPLACE FUNCTION app_is_worker() RETURNS boolean
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN coalesce(current_setting('app.worker', true), '') = 'on';
EXCEPTION WHEN others THEN
    RETURN false;
END $$;
"""


def _existing() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE not in _existing():
        op.create_table(
            TABLE,
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("source_id", sa.dialects.postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("mission_id", sa.dialects.postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("missions.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending", index=True),
            sa.Column("source_kind", sa.String(32), nullable=False),
            sa.Column("mime", sa.String(64), nullable=True),
            sa.Column("input_text", sa.Text, nullable=True),
            sa.Column("input_bytes", sa.LargeBinary, nullable=True),
            sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("3")),
            sa.Column("lease_owner", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.String(200), nullable=True),
            sa.Column("idempotency_key", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("user_id", "idempotency_key", name="uq_intake_job_idem"),
        )
        op.create_index("ix_intake_jobs_claimable", TABLE, ["status", "lease_expires_at"])

    op.execute(_WORKER_FN)

    # Apply RLS whether or not create_all built the table (create_all does not create policies).
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": TABLE}
    ).scalars().all()
    if "tenant_or_worker" not in policies:
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_or_worker ON {TABLE} "
            f"USING (user_id = app_current_user() OR app_is_worker()) "
            f"WITH CHECK (user_id = app_current_user() OR app_is_worker())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_or_worker ON {TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS app_is_worker()")
