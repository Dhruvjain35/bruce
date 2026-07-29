"""agent_runs.status is a closed vocabulary, enforced by the database.

WHY A CONSTRAINT AND NOT JUST THE CODE. `agent_run_store` now routes every status write through
`transitions.propose_transition`, which is the real fix — but the column it protects is a plain
`String(32)`, so anything holding a session can still write a word the machine has never heard of. That
is not hypothetical: the failure this whole lane exists to close began with the model emitting
`required_capabilities=["sending messages"]` — free text into a field that was supposed to join to a
registry — and nothing in the schema objected. A status column with no vocabulary is the same defect
waiting in a different column, and the fix is the same: make the invalid state unrepresentable rather
than merely discouraged.

The check is deliberately WIDER than `contract.MachineState`. Two vocabularies genuinely share this
column and collapsing them would be a data-loss migration, not a cleanup:

  * the mission machine  — understanding / preparing / awaiting_approval / executing / waiting_external /
    verifying / succeeded / blocked / failed / cancelled
  * the background-mission LEASE lifecycle — queued / running / completed / dead_letter — which
    `transitions.py` explicitly declares out of its own scope and hands to `agent_run_store`.

`completed` is NOT `succeeded` and must not be folded into it. `succeeded` is a claim about the outside
world that requires an independent read-back; `completed` means a worker finished its turn on the row.
`agent_run_store.STATUS_VOCABULARY` is the same list, and `test_status_enforcement` compares the two so
the code and the constraint cannot drift apart.

EXISTING DATA. Every reachable database was inspected before this was written and no `agent_runs` row
held a status outside the list, so the constraint applies as-is. The backfill below is not decoration:
a migration that assumes clean data and cannot run is worse than no migration, and this one must also
survive a deployment whose rows predate the lane. An unrecognised status becomes `blocked` — the honest
state for "cannot proceed without something", non-terminal so nothing is stranded — and the original
string is preserved verbatim in `recovery_state` so the change is inspectable and undoable by hand.
Nothing is deleted and no run is silently marked done.

CREATED UNCONDITIONALLY, for the reason `0030_claim_lineage` spells out: `0001_initial_schema` builds
tables with `Base.metadata.create_all()`, which emits no CHECK constraints, so a migration that skips
itself on a fresh database leaves the invariant missing in exactly the environment nobody inspects.

Revision ID: 0031_agent_run_status_check
Revises: 0030_claim_lineage
Create Date: 2026-07-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0031_agent_run_status_check"
down_revision = "0030_claim_lineage"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_agent_runs_status"

# Kept as a literal, because a migration is a historical record and must not change meaning when the
# application's constants do. `test_status_enforcement.test_migration_vocabulary_matches_the_code` reads
# this tuple back out and compares it to `agent_run_store.STATUS_VOCABULARY`, so drift is a test failure
# rather than a production insert failure.
STATUS_VOCABULARY = (
    # the mission machine (contract.MachineState)
    "understanding", "preparing", "awaiting_approval", "executing", "waiting_external",
    "verifying", "succeeded", "blocked", "failed", "cancelled",
    # the background-mission lease lifecycle (agent_run_store)
    "queued", "running", "completed", "dead_letter",
)

_QUARANTINE = "blocked"


def _sql_list() -> str:
    return ", ".join(f"'{s}'" for s in STATUS_VOCABULARY)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Make the data satisfiable, explicitly and reversibly. `recovery_state` is the run's own
    #    restart-from-here JSONB, which is exactly where a note about how this run was touched belongs.
    bind.execute(sa.text(f"""
        UPDATE agent_runs
           SET recovery_state = COALESCE(recovery_state, '{{}}'::jsonb)
                                || jsonb_build_object('migrated_0031_from_status', status),
               status = '{_QUARANTINE}'
         WHERE status NOT IN ({_sql_list()})
    """))

    # 2. Drop-then-add so a re-run (or a database that somehow already has it) converges rather than
    #    failing on a duplicate name.
    bind.execute(sa.text(f"ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS {CONSTRAINT}"))
    bind.execute(sa.text(
        f"ALTER TABLE agent_runs ADD CONSTRAINT {CONSTRAINT} CHECK (status IN ({_sql_list()}))"))


def downgrade() -> None:
    # Only the constraint comes off. The quarantined rows are NOT restored: their original status was
    # copied into `recovery_state` and putting a word back that the machine cannot move is a worse
    # database than one where the history is readable and the live state is legal.
    op.get_bind().execute(sa.text(f"ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS {CONSTRAINT}"))
