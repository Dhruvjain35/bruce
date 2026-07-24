"""agent_runs background-mission lease columns (G0.5 — durable background runner)

Adds the lease/scheduling columns so the worker can claim a background_mission AgentRun one at a time,
FOR UPDATE SKIP LOCKED, exactly like intake_jobs/outbound_messages — crash-safe (a dropped lease is
reclaimed) with no double-execution. agent_runs already has tenant_or_worker RLS (0023), so a
worker_session claim across users is permitted; this migration only adds columns + a claimable index.

Conditional throughout (0001 create_all builds the columns on a fresh DB; ALTER on a migrated one).

Revision ID: 0025_agent_run_leases
Revises: 0024_calendar_event_entities
Create Date: 2026-07-24
"""
import sqlalchemy as sa
from alembic import op

revision = "0025_agent_run_leases"
down_revision = "0024_calendar_event_entities"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"


def _insp():
    return sa.inspect(op.get_bind())


def _cols(table: str) -> set[str]:
    return {c["name"] for c in _insp().get_columns(table)} if table in _insp().get_table_names() else set()


def upgrade() -> None:
    present = set(_insp().get_table_names())
    if "agent_runs" not in present:
        return  # fresh DB before 0023 create_all ran this table — nothing to alter

    have = _cols("agent_runs")
    with op.batch_alter_table("agent_runs") as b:
        if "lease_owner" not in have:
            b.add_column(sa.Column("lease_owner", sa.String(64), nullable=True))
        if "lease_expires_at" not in have:
            b.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        if "next_run_at" not in have:
            b.add_column(sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
        if "attempt_count" not in have:
            b.add_column(sa.Column("attempt_count", sa.Integer, nullable=False, server_default=sa.text("0")))
        if "max_attempts" not in have:
            b.add_column(sa.Column("max_attempts", sa.Integer, nullable=False, server_default=sa.text("5")))

    idx = {i["name"] for i in _insp().get_indexes("agent_runs")}
    if "ix_agent_runs_claimable" not in idx:
        op.create_index("ix_agent_runs_claimable", "agent_runs", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_agent_runs_claimable")
    for c in ("lease_owner", "lease_expires_at", "next_run_at", "attempt_count", "max_attempts"):
        op.execute(f"ALTER TABLE agent_runs DROP COLUMN IF EXISTS {c}")
