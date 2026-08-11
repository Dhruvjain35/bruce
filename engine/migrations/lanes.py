"""Migration-lane reservation (D-INT-4, integration invariant 3).

Four workstreams run in parallel worktrees off Alembic head 0018. If each cuts its own "0019" the chain
forks into an Alembic MULTI-HEAD + a merge conflict. To prevent that, migration NUMBERS are reserved per
lane up front, and CI (test_migration_discipline) enforces the reservation.

Rules:
  * A lane's FIRST new migration uses its reserved number below.
  * A lane's SECOND migration is NOT free to pick the next integer — the integration owner assigns it, so
    the chain stays strictly linear with exactly one head.
  * trust-evals has no reserved number (its surface is the YAML capability registry, not schema).
  * Any migration file numbered >= 0019 MUST have an owner here (CI fails otherwise).
"""

# reserved next-migration number -> owning workstream lane (off head 0018)
MIGRATION_LANES: dict[str, str] = {
    "0019": "integration-oauth",   # oauth_states tenant_or_worker RLS (real Google callback fix)
    "0020": "school-command",
    "0021": "humanity",
    "0022": "runtime",             # general agent runtime lane opener: user_world_state (R3)
    "0023": "runtime",             # agent_runs + agent_run_events (R2)
    "0024": "runtime",             # calendar_event_entities (R7)
    "0025": "runtime",             # agent_runs background-mission lease columns (G0.5)
    "0026": "runtime",             # gmail_sent_ledger — Gmail exactly-once send ledger (Phase G)
    # Two lanes run in parallel from head 0026 (#121a authorization, #121b typed memory). The numbers are
    # assigned HERE, up front, by the one owner — which is the whole point of this file. Without it both
    # lanes cut a "0027" in separate worktrees and the chain forks into a multi-head on merge day.
    "0027": "authorization",       # authorization_evidence + authorization_refusals — durable consent (#121a)
    "0028": "memory",              # typed memory records (#121b)
    "0029": "memory",              # memory reshaped to the canonical ORM model (#122)
    "0030": "memory",              # claim lineage + active-claim uniqueness (#123)
    # The brain spine. agent_runs.status becomes a CHECK-constrained MachineState vocabulary, so an
    # illegal state cannot reach the row even if a caller bypasses transitions.propose_transition.
    "0031": "runtime",             # agent_runs.status CHECK over MachineState (brain spine)
    # `Fill.resolvable` says a recipient may be looked up and never invented. Nothing could look one up
    # until this table: people the student INTRODUCED, in their own trusted words, with provenance.
    "0032": "runtime",             # known_people — conversation-learned recipients
    # Shadow observation was a detached task, so a restart dropped the record — and dropped the SLOW
    # reads specifically, biasing the authority decision toward fast successes. This is its outbox, in
    # the shape it has to end up in: keyed on the canonical conversation turn, fenced by a per-claim
    # lease token, carrying the turn's own capability truth and an intake disposition for every trusted
    # turn, with a retry bound the database will not let reach zero.
    "0033": "runtime",             # semantic_shadow_jobs — durable shadow observation queue
    # NOT a shadow change. 0032 opens with `if has_table: return` and 0001 builds every table with
    # create_all(), so 0032 returned before its own ENABLE ROW LEVEL SECURITY on every database that
    # exists: any tenant session could read every student's recipient book. Found by adding the table to
    # the enforced RLS inventory, which is the argument for having one.
    "0034": "runtime",             # known_people — the row security 0032 never actually applied
    # The reconciliation's POPULATION used to come from the jobs the drain had claimed, so a user whose
    # intake call was missing had no job and was never checked — the invariant agreed with itself on
    # every wake. The canonical count cannot be bought with a row-level grant (RLS admits ROWS, so
    # `FOR SELECT USING (app_is_worker())` hands over every column of every student's turn, `text`
    # included) nor with a per-owner definer (that hands over the owner LIST, and a worker that knows who
    # exists can open a session as each of them). The join happens inside the definer and one row of
    # counts comes back.
    "0035": "runtime",             # shadow_reconciliation() — aggregate-only, no owner ids leave it
}

# the head every lane branches from; the head-assertion test (test_migration_rls_context) must equal the
# single real head, and this is the baseline lanes were reserved against.
RESERVED_FROM_HEAD = "0018_conversation_context_graph"


def lane_for(migration_number: str) -> str | None:
    """The owning lane for a reserved migration number (e.g. '0019'), or None if unreserved."""
    return MIGRATION_LANES.get(migration_number)
