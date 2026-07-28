"""Claim lineage + database-enforced uniqueness for active memory.

TWO DEFECTS #122 SHIPPED WITH, BOTH CLOSED HERE.

1. FORGETTING REACHED ONE ROW. A corrected fact is two rows: v1 superseded, v2 active. A fact-scoped
   forget of v2 left v1 sitting in the table with its value intact — still readable through provenance,
   correction history and any audit view. The student asked Bruce to forget a teacher's name and Bruce
   kept the previous spelling of it. `claim_root_id` makes every version of one claim addressable as a
   lineage, so "forget that" can mean what a person means by it.

2. DUPLICATE PREVENTION WAS READ-THEN-COMPARE. Two concurrent writes of the same fact both read "no
   duplicate" and both inserted. `uq_memory_active_claim` makes it the database's problem: at most one
   ACTIVE row per (user, claim). Concurrent identical writes collapse to one; concurrent CONFLICTING
   writes also collide, which is the more important half — it is structurally impossible to end up with
   two active contradictory facts, rather than merely unlikely.

WHY A PARTIAL INDEX RATHER THAN A CONSTRAINT. Superseded, contradicted, forgotten and expired versions
of a claim must coexist freely — that is the whole point of keeping history. Only `status = 'active'` is
exclusive, and `WHERE` is the only way to say that.

EVERYTHING HERE IS CREATED UNCONDITIONALLY, and that is not defensive style. `0001_initial_schema` calls
`Base.metadata.create_all()`, so any table reachable from `Base` — which now includes `memory_records` —
exists on a fresh database WITHOUT its CHECK constraints, indexes, triggers or RLS policy, because
`create_all` emits none of those. A migration that guards its work behind "if the table is absent" skips
on a fresh database and the invariants are missing in exactly the environment nobody inspects.
`test_migration_parity.py` builds a fresh database and an upgraded one and compares them, so this stops
being a rule someone has to remember.

Revision ID: 0030_claim_lineage
Revises: 0029_memory_canonical
Create Date: 2026-07-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0030_claim_lineage"
down_revision = "0029_memory_canonical"
branch_labels = None
depends_on = None
APP_ROLE = "bruce_app"
_RECORDS = "memory_records"


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_RECORDS)}


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    bind = op.get_bind()
    present = _columns(bind)

    if "claim_root_id" not in present:
        op.add_column(_RECORDS, sa.Column("claim_root_id", UUID(as_uuid=True), nullable=True))
    if "claim_key" not in present:
        op.add_column(_RECORDS, sa.Column("claim_key", sa.String(64), nullable=True))

    # Backfill: every existing row is its own lineage root. Correct by construction — before this
    # migration there was no way to express "these two rows are the same claim", so each one is.
    op.execute(f"UPDATE {_RECORDS} SET claim_root_id = memory_id WHERE claim_root_id IS NULL")

    op.execute(f"CREATE INDEX IF NOT EXISTS ix_memory_claim_root ON {_RECORDS} "
               f"(user_id, claim_root_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_memory_claim_key ON {_RECORDS} (user_id, claim_key)")

    # THE UNIQUENESS GUARANTEE. `claim_key IS NOT NULL` so rows written before this migration — which
    # have no claim key — are not forced into a single-row-per-user collision.
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_active_claim ON {_RECORDS} "
               f"(user_id, claim_key) WHERE status = 'active' AND claim_key IS NOT NULL")

    # Re-assert every invariant `0029` owns, unconditionally. On a fresh database `create_all` built this
    # table bare and `0029`'s DROP/CREATE fixed it; on an upgraded database `0029` ran normally. Both
    # paths must arrive here identical, and re-running these is the cheapest way to be sure rather than
    # to assume. `IF NOT EXISTS` / `DROP ... IF EXISTS` make each one idempotent.
    op.execute(f"ALTER TABLE {_RECORDS} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_RECORDS} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_RECORDS} TO {APP_ROLE}")
    policies = bind.execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": _RECORDS}).scalars().all()
    if "tenant_isolation" not in policies:
        op.execute(f"CREATE POLICY tenant_isolation ON {_RECORDS} "
                   f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())")

    constraints = {c["name"] for c in sa.inspect(bind).get_check_constraints(_RECORDS)}
    if "ck_memory_forgotten_redacted" not in constraints:
        op.execute(f"""ALTER TABLE {_RECORDS} ADD CONSTRAINT ck_memory_forgotten_redacted CHECK (
            forgotten_at IS NULL OR (value_json IS NULL AND normalized_value IS NULL
            AND evidence_text IS NULL AND subject IS NULL AND predicate IS NULL
            AND reason_it_matters IS NULL AND entity_key IS NULL))""")
    if "ck_memory_forgotten_status" not in constraints:
        op.execute(f"ALTER TABLE {_RECORDS} ADD CONSTRAINT ck_memory_forgotten_status "
                   f"CHECK ((forgotten_at IS NULL) = (status <> 'forgotten'))")

    op.execute(f"""
CREATE OR REPLACE FUNCTION memory_records_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.forgotten_at IS NULL AND NEW.forgotten_at IS NOT NULL THEN
        IF NEW.value_json IS NOT NULL OR NEW.normalized_value IS NOT NULL
           OR NEW.evidence_text IS NOT NULL OR NEW.subject IS NOT NULL
           OR NEW.predicate IS NOT NULL OR NEW.reason_it_matters IS NOT NULL THEN
            RAISE EXCEPTION 'forgetting must erase content, not rewrite it (memory %)', OLD.memory_id;
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.predicate IS DISTINCT FROM OLD.predicate
       OR NEW.value_json IS DISTINCT FROM OLD.value_json
       OR NEW.normalized_value IS DISTINCT FROM OLD.normalized_value
       OR NEW.evidence_text IS DISTINCT FROM OLD.evidence_text
       OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
       OR NEW.source_type IS DISTINCT FROM OLD.source_type
       OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
       OR NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.claim_root_id IS DISTINCT FROM OLD.claim_root_id THEN
        RAISE EXCEPTION 'memory_records is append-only; corrections supersede, they do not edit (memory %)', OLD.memory_id;
    END IF;
    RETURN NEW;
END;
$$""")
    op.execute(f"DROP TRIGGER IF EXISTS memory_records_no_edit ON {_RECORDS}")
    op.execute(f"CREATE TRIGGER memory_records_no_edit BEFORE UPDATE ON {_RECORDS} "
               f"FOR EACH ROW EXECUTE FUNCTION memory_records_append_only()")

    op.execute("CREATE INDEX IF NOT EXISTS ix_memory_shortlist ON memory_records "
               "(user_id, status, domain, observed_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_memory_entity ON memory_records "
               "(user_id, status, entity_key)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_memory_active_claim")
    op.execute("DROP INDEX IF EXISTS ix_memory_claim_key")
    op.execute("DROP INDEX IF EXISTS ix_memory_claim_root")
    op.drop_column(_RECORDS, "claim_key")
    op.drop_column(_RECORDS, "claim_root_id")
