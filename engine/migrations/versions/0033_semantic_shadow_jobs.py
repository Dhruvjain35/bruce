"""semantic_shadow_jobs — the durable outbox that makes shadow observation a ROW, not a coroutine.

WHY THIS TABLE EXISTS. Shadow mode was a detached asyncio task: it held a strong reference and a hard
budget, so it never delayed or broke a turn, but it was still BEST EFFORT. A container shutdown, a
process restart or a worker cancellation dropped the observation silently — and it dropped exactly the
SLOW ones, because a fast successful read had already finished. The sample that decides whether the
semantic executive may be given authority would then have been biased toward fast successful reads,
which is the one bias that must not exist in it. Slow and failing reads are the evidence that matters.

So the request path commits a row and stops. A worker claims it under a lease, reads, and records — the
same shape as `intake_jobs` (migration 0005), deliberately, because a second job pattern would be a
second set of failure modes to learn.

ONE ROW PER TURN, KEYED ON THE TURN ITSELF. `conversation_turn_id` is UNIQUE and is the identity: the
invariant is "exactly one shadow job per eligible CONVERSATION TURN", and the only honest way to state
that is over the turn. The older way to say it — (user_id, channel, provider_message_id) — is the
PROVIDER's metadata, and it identifies a turn only if you assume no provider ever reuses an id across
channels; narrowing it to (user_id, provider_message_id) silently swallowed the second channel's turn,
which is a LOSS, and losses bias the authority sample toward whichever channel won the race. That triple
is still here, still UNIQUE, DEMOTED TO INGRESS DEDUPE: it is what wins the race between two simultaneous
webhook deliveries in the instant before the canonical id is known. Reconciliation joins on the canonical
id and never re-derives the triple, so a row that lost its canonical reference matches no turn and
reconciles as UNOBSERVED rather than as a silent pass.

ON DELETE CASCADE to that turn. The row exists only to describe the turn; telemetry that outlives the
student content it points at is a deletion that did not finish.

THE RETRY BOUND IS A DATABASE CONSTRAINT, because its zero value is silent total loss. `claim` takes a
row only while `attempts < max_attempts`; at 0 that predicate is false on a row that has never been
claimed, so every fresh job is permanently unclaimable — no error, no backlog, no observations, and a
queue that looks perfectly drained. Hence a positive DEFAULT *and* `CHECK (max_attempts > 0)`: the state
is unrepresentable rather than merely unintended.

THE LEASE FENCE IS A TOKEN, NOT A NAME. Completion writes used to guard themselves with
`AND lease_owner = :worker`, and the worker id is `cloudrun-<nodename>-<pid>`. On Cloud Run that string
is NOT unique to a claim: one container serves overlapping /process invocations, and a worker whose lease
expired mid-read shares its lease_owner with the worker that legitimately reclaimed the row. The guard
therefore admitted exactly the write it was written to stop — a zombie overwriting a newer observation.
`lease_token` is minted fresh per claim (`gen_random_uuid()`) and every completion write requires THAT
token, so a stale worker updates ZERO rows even from the same process on the same container. Nullable
because a row that has never been claimed holds no lease, and because a NULL matches nothing, which is
the correct answer to "you do not hold this".

CAPABILITY TRUTH BELONGS TO THE TURN. The shadow worker used to build the executive's context from
`tool_registry.specs(None) if live` — a static table, identical for every user, from a function whose own
docstring says it does not check the user's connection. The failure ran in the worst direction: a user
with NO Google connection asks for a calendar event, the live router truthfully answers that Bruce has no
hands, the executive proposes `calendar.create_event` because it is globally live, and a FALSE CAPABILITY
DENIAL is recorded against a router that was right — the number the authority decision most depends on
over-counting in the direction that argues FOR granting authority. `reachable_operations` is therefore
computed at the REQUEST BOUNDARY through the real per-user ToolBroker and stored on the turn's own row.
It is NULLABLE and NULL means something: `[]` is "we looked, and this user can reach nothing", NULL is
"we could not establish capability truth", and an unknown truth may never be counted as a denial.
Registry ids only, so nothing here is PII.

AND EVERY TRUSTED TURN LEAVES A DISPOSITION. `enqueue` was once called from inside the `else` of the
routing branch, so resolved continuations and ambiguous goal selections skipped shadow entirely — no
row, no trace — and the in-process invariant kept agreeing with itself because it defined the denominator
as the sum of its own numerators. Reconciliation counts trusted turns from `conversation_turns`, a ledger
shadow does not write, against these rows; that only works if a turn deliberately NOT observed leaves a
row saying so. An exclusion that leaves nothing behind is indistinguishable from a dropped turn.

NO TURN TEXT LIVES HERE. The row REFERENCES the turn; the worker reads the student's words from
`conversation_turns` under the owner's session, for the one turn its claimed job is about. Two reasons: a
second copy of the most sensitive free-text Bruce holds is a second thing to protect, and a deletion that
cascades through conversation_turns must not leave the text alive in a telemetry table.

RLS IS `tenant_or_worker`, COPIED FROM intake_jobs FOR THE SAME REASON. The owner sees their own rows; a
WORKER SESSION (app.worker='on', set only by server worker code — never from a request, never from user
input) may claim across users, because a worker cannot know which users have queued work without first
looking past per-user isolation. Every write of the observation still happens under
user_session(job.user_id).

CREATED UNCONDITIONALLY, for the reason 0030/0031/0032 all spell out: `0001_initial_schema` builds tables
with `Base.metadata.create_all()`, so a migration that skips itself on a fresh database leaves the policy
missing in exactly the environment nobody inspects. The table create is guarded; the RLS is not.

Revision ID: 0033_semantic_shadow_jobs
Revises: 0032_known_people
Create Date: 2026-08-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0033_semantic_shadow_jobs"
down_revision = "0032_known_people"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
TABLE = "semantic_shadow_jobs"
TURNS = "conversation_turns"

INGRESS_UNIQUE = "uq_semantic_shadow_job_turn"
CANONICAL_UNIQUE = "uq_semantic_shadow_job_conversation_turn"
TURN_FK = "fk_semantic_shadow_job_conversation_turn"
MAX_ATTEMPTS_CHECK = "ck_semantic_shadow_job_max_attempts_positive"
CLAIM_INDEX = "ix_semantic_shadow_jobs_claimable"

DEFAULT_MAX_ATTEMPTS = 3


def _insp():
    return sa.inspect(op.get_bind())


def _retry_bound_cannot_be_zero() -> None:
    """The positive DEFAULT and the CHECK, asserted rather than assumed.

    TWO PATHS BUILD THIS TABLE — `0001_initial_schema`'s `create_all()` from the ORM on a fresh database,
    and the `create_table` below on a migrated one — so the property whose absence is 100% silent loss of
    observations is established here, where it holds for both, instead of in whichever path happens to
    run.
    """
    if TABLE not in set(_insp().get_table_names()):
        return
    op.execute(f"ALTER TABLE {TABLE} ALTER COLUMN max_attempts SET DEFAULT {DEFAULT_MAX_ATTEMPTS}")
    # Repair before constraining. A row already carrying a non-positive bound is a job that can never be
    # claimed again; giving it the real default puts it back in the queue instead of failing the upgrade.
    op.execute(f"UPDATE {TABLE} SET max_attempts = {DEFAULT_MAX_ATTEMPTS} WHERE max_attempts < 1")
    if MAX_ATTEMPTS_CHECK not in {c["name"] for c in _insp().get_check_constraints(TABLE)}:
        op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {MAX_ATTEMPTS_CHECK} CHECK (max_attempts > 0)")


def _canonical_turn_fk_cascades(bind) -> None:
    """The telemetry row must not outlive the turn it describes — on either build path."""
    tables = set(_insp().get_table_names())
    if TABLE not in tables or TURNS not in tables:
        return
    rows = bind.execute(sa.text(
        """
        SELECT c.conname, c.confdeltype
          FROM pg_constraint c
          JOIN pg_class rel ON rel.oid = c.conrelid
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
         WHERE c.contype = 'f' AND rel.relname = :t AND a.attname = 'conversation_turn_id'
        """), {"t": TABLE}).mappings().all()
    if any(r["confdeltype"] == "c" for r in rows):
        return
    for r in rows:                       # a non-cascading FK is replaced, never left beside a new one
        op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {r['conname']}")
    op.create_foreign_key(TURN_FK, TABLE, TURNS, ["conversation_turn_id"], ["id"], ondelete="CASCADE")


def upgrade() -> None:
    bind = op.get_bind()
    UUID = sa.dialects.postgresql.UUID
    JSONB = sa.dialects.postgresql.JSONB

    if TABLE not in set(_insp().get_table_names()):
        op.create_table(
            TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            # THE IDENTITY. Nullable because a NULL never collides in a unique index, which is what lets
            # a row that cannot prove which turn it belongs to reconcile as UNOBSERVED — the truth about
            # it — instead of claiming a turn by reconstruction.
            sa.Column("conversation_turn_id", UUID(as_uuid=True),
                      sa.ForeignKey(f"{TURNS}.id", ondelete="CASCADE", name=TURN_FK), nullable=True),
            # THE PROVIDER'S METADATA — ingress dedupe only. The student's words stay in
            # conversation_turns, which is where a deletion reaches them.
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("provider_message_id", sa.String(255), nullable=False),
            # pending -> processing -> completed | retryable_failed (-> reclaimed) | terminal_failed,
            # the intake_jobs vocabulary unchanged.
            sa.Column("status", sa.String(24), nullable=False, server_default="pending", index=True),
            # THE TYPED OUTCOME: ok | timeout | transport | provider_rejected | invalid_schema |
            # budget_exceeded | turn_missing. A failed observation is DATA here; it is never allowed to
            # be `completed`, because "completed" with no reading is precisely the record that would
            # make the collected sample look cleaner than the executive actually is.
            sa.Column("outcome", sa.String(32), nullable=True, index=True),
            sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("max_attempts", sa.Integer, nullable=False,
                      server_default=sa.text(str(DEFAULT_MAX_ATTEMPTS))),
            sa.Column("lease_owner", sa.String(64), nullable=True),
            # THE FENCE. Written by `claim` alone; every completion write compares against it.
            sa.Column("lease_token", UUID(as_uuid=True), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.String(200), nullable=True),   # TYPE/short reason only, no content
            # What production actually decided, captured at the turn: labels and capability ids only.
            sa.Column("router_snapshot", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            # The two context booleans AS OF THE TURN. Re-deriving them at worker time would read a world
            # that has moved on, which is a different (and invisible) bias.
            sa.Column("context_flags", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            # `eligible`, or a typed exclusion reason. NOT NULL: a trusted turn with no disposition is
            # exactly the row reconciliation cannot tell apart from a dropped one.
            sa.Column("intake_disposition", sa.String(40), nullable=False, server_default="eligible"),
            # NO server_default. A default of '[]' would tell every row that capability truth was
            # established and empty for it, which is the one thing this column exists to distinguish.
            sa.Column("reachable_operations", JSONB, nullable=True),
            # The executive's reading. Written by an UPDATE, so a retry converges instead of duplicating.
            sa.Column("observation", JSONB, nullable=True),
            sa.Column("agrees", sa.Boolean, nullable=True),
            sa.Column("divergence", sa.String(64), nullable=True, index=True),
            # One of the four numbers the authority decision rests on, so it is a COLUMN like `agrees`
            # and `divergence`: it has to be countable in SQL over the whole table. NULL means the turn
            # was never read, which is deliberately not False — counting an unread turn as "no denial"
            # would make the metric look healthier the more often the executive failed to answer.
            sa.Column("false_capability_denial", sa.Boolean, nullable=True),
            sa.Column("latency_ms", sa.Integer, nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            # THE INVARIANT: one shadow job per CONVERSATION TURN, stated over the turn itself.
            sa.UniqueConstraint("conversation_turn_id", name=CANONICAL_UNIQUE),
            # INGRESS DEDUPE. Enqueue is `ON CONFLICT DO NOTHING` against this constraint — a
            # SELECT-then-INSERT would race two webhook deliveries into two observations.
            sa.UniqueConstraint("user_id", "channel", "provider_message_id", name=INGRESS_UNIQUE),
            sa.CheckConstraint("max_attempts > 0", name=MAX_ATTEMPTS_CHECK),
        )
        # The claim query filters on status + lease_expires_at; index it for the worker hot path. It also
        # filters `attempts < max_attempts`, which is a cheap predicate on top of the candidate set — no
        # second index earns its write cost on a telemetry table this small.
        op.create_index(CLAIM_INDEX, TABLE, ["status", "lease_expires_at"])

    _retry_bound_cannot_be_zero()
    _canonical_turn_fk_cascades(bind)

    # Unconditional (create_all builds tables, never policies), and idempotent.
    policies = bind.execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": TABLE}
    ).scalars().all()
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")
    if "tenant_or_worker" not in policies:
        # Identical to intake_jobs (0005). app_is_worker() reads the transaction-local app.worker
        # setting, which only db.worker_session sets — a request handler can never gain it.
        op.execute(
            f"CREATE POLICY tenant_or_worker ON {TABLE} "
            f"USING (user_id = app_current_user() OR app_is_worker()) "
            f"WITH CHECK (user_id = app_current_user() OR app_is_worker())"
        )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_or_worker ON {TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
