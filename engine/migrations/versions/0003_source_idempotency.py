"""sources: idempotency_key (+ unique per user) and extracted — durable, replayable intake

Two columns, both in service of making POST /v1/intake idempotent:

  * ``idempotency_key`` — derived from (source_kind, raw text) unless the client supplies one.
    UNIQUE (user_id, idempotency_key) is what actually PREVENTS duplicate sources under a race:
    two concurrent identical intakes both attempt the INSERT, exactly one wins, the loser catches
    IntegrityError and reads the winner's row. Nullable, and Postgres treats NULLs as distinct in
    a unique constraint, so pre-existing sources (key NULL) never collide.
  * ``extracted`` — the grounded extraction result (ExtractedIntake JSON) for the source. Stored so
    an idempotent retry REPLAYS the original response instead of re-running a nondeterministic LLM
    extraction that could contradict the spans/tasks already persisted from the first call.

Privacy note on ``extracted``: it holds DERIVED, minimized content (the same grounded material
already durable in source_spans/tasks), NOT the raw blob. It therefore follows the same lifecycle
as spans/tasks — it survives the retention sweep, which erases only ``raw_text``. Account deletion
still removes it via the users FK cascade.

WHY THIS MIGRATION IS CONDITIONAL (a real flaw in 0001, not defensiveness for its own sake)
-------------------------------------------------------------------------------------------
``0001_initial`` does not contain static DDL — it calls ``Base.metadata.create_all()`` against the
LIVE ORM models. So 0001 does not describe a fixed schema; it describes whatever bruce_engine.schema
says *at the moment it runs*. Consequence:

    fresh database (CI)      -> 0001 create_all() ALREADY builds idempotency_key/extracted/uq
    already-migrated database -> has neither; only this migration adds them

Those two paths diverge, so an unconditional ADD COLUMN here fails on a fresh database with
"column already exists" (it does — that is how this was found). Guarding on the live inspector is
what makes both paths converge on the same schema.

The deeper hazard remains and MUST be fixed next: with create_all() in 0001, anyone adding a column
to schema.py gets it for free on fresh databases (including CI) while a real migrated database does
NOT have it — CI passes green while production 500s on a missing column. The fix is to freeze 0001
into static DDL so migrations are immutable. That is a separate, single-purpose commit; it is not
smuggled in here.

Revision ID: 0003_source_idem
Revises: 0002_rls
Create Date: 2026-07-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_source_idem"
down_revision = "0002_rls"
branch_labels = None
depends_on = None

TABLE = "sources"
SPANS = "source_spans"
UQ = "uq_source_idem"


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _uqs(table: str) -> set[str]:
    return {u["name"] for u in sa.inspect(op.get_bind()).get_unique_constraints(table)}


def upgrade() -> None:
    cols = _cols(TABLE)
    if "idempotency_key" not in cols:
        op.add_column(TABLE, sa.Column("idempotency_key", sa.String(128), nullable=True))
    if "extracted" not in cols:
        op.add_column(TABLE, sa.Column("extracted", JSONB, nullable=True))
    if UQ not in _uqs(TABLE):
        op.create_unique_constraint(UQ, TABLE, ["user_id", "idempotency_key"])
    # source_spans.ordinal — a source's spans are all written in ONE transaction, so created_at
    # ties exactly and id is a random uuid. Without an explicit ordinal there is no stable order
    # to replay them in, and an idempotent retry returns the same ids SHUFFLED.
    if "ordinal" not in _cols(SPANS):
        op.add_column(SPANS, sa.Column("ordinal", sa.Integer(), nullable=True))


def downgrade() -> None:
    if "ordinal" in _cols(SPANS):
        op.drop_column(SPANS, "ordinal")
    if UQ in _uqs(TABLE):
        op.drop_constraint(UQ, TABLE, type_="unique")
    if "extracted" in _cols(TABLE):
        op.drop_column(TABLE, "extracted")
    if "idempotency_key" in _cols(TABLE):
        op.drop_column(TABLE, "idempotency_key")
