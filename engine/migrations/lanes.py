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
}

# the head every lane branches from; the head-assertion test (test_migration_rls_context) must equal the
# single real head, and this is the baseline lanes were reserved against.
RESERVED_FROM_HEAD = "0018_conversation_context_graph"


def lane_for(migration_number: str) -> str | None:
    """The owning lane for a reserved migration number (e.g. '0019'), or None if unreserved."""
    return MIGRATION_LANES.get(migration_number)
