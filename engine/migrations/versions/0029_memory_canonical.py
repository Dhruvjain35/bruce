"""memory_records reshaped to the canonical model, + memory_corrections and memory_forget_events.

0028 landed the table with column truth in TWO places: the migration, and a private SQLAlchemy Core table
declared inside the memory module. A schema with two definitions has no definition, and the module's copy
had already drifted from the rest of the engine (every other table is an ORM model in `schema.py`).

This recreates the table from the canonical model. It DROPS rather than migrates because 0028 shipped
hours ago and nothing writes to it yet — there is no production data, and a careful ALTER chain to
preserve zero rows would be ceremony that hides what actually happened. If that ever stops being true,
this migration is the wrong shape and the next one has to be additive.

What changes, and why each one:

  * `value` -> `value_json` + `normalized_value`. The structured value and the folded form the
    deterministic shortlist matches on are different things and were sharing a column.
  * `status` becomes a column. It was three nullable timestamps that every read path had to combine
    correctly (`forgotten_at`, `superseded_by`, `contradicted_by`), which is a rule you can forget.
  * `expires_at` + `freshness_class` become columns, so stale-record filtering is an index scan rather
    than arithmetic over `observed_at` at query time.
  * `evidence` (JSONB) -> `evidence_text`. Evidence is the span the student actually wrote; storing it as
    JSON invited current state to be duplicated inside it, which the model now forbids.

The forgotten-row CHECK survives the reshape: a row that is forgotten and still carries content cannot be
stored, so redaction is enforced by the database rather than by whichever code path last touched it.

Revision ID: 0029_memory_canonical
Revises: 0028_typed_memory
Create Date: 2026-07-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0029_memory_canonical"
down_revision = "0028_typed_memory"
branch_labels = None
depends_on = None
APP_ROLE = "bruce_app"

_RECORDS = "memory_records"
_CORRECTIONS = "memory_corrections"
_FORGETS = "memory_forget_events"


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

    # 0028 created earlier shapes of all three tables. Dropping rather than altering, for the reason in
    # the docstring: they shipped hours ago, nothing writes to them, and an ALTER chain preserving zero
    # rows would obscure what actually happened here.
    for table in (_FORGETS, _CORRECTIONS, _RECORDS):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.create_table(
        _RECORDS,
        sa.Column("memory_id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(200), nullable=True),
        sa.Column("predicate", sa.String(100), nullable=True),
        sa.Column("value_json", JSONB, nullable=True),
        sa.Column("normalized_value", sa.String(300), nullable=True),
        sa.Column("evidence_text", sa.Text, nullable=True),
        sa.Column("source_message_id", sa.String(255), nullable=True, index=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False, server_default=sa.text("1.0")),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("freshness_class", sa.String(16), nullable=False, server_default="fresh"),
        sa.Column("retention_policy", sa.String(16), nullable=False),
        sa.Column("sensitivity", sa.String(16), nullable=False),
        sa.Column("user_editable", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", index=True),
        sa.Column("contradicted_by_id", UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by_id", UUID(as_uuid=True), nullable=True),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entity_key", sa.String(200), nullable=True, index=True),
        sa.Column("domain", sa.String(32), nullable=True, index=True),
        sa.Column("reason_it_matters", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        # Forgetting is enforced by the DATABASE, not by whichever code path last touched the row: a
        # forgotten record that still carries content cannot be stored at all.
        sa.CheckConstraint(
            "forgotten_at IS NULL OR (value_json IS NULL AND normalized_value IS NULL "
            "AND evidence_text IS NULL AND subject IS NULL AND predicate IS NULL "
            "AND reason_it_matters IS NULL AND entity_key IS NULL)",
            name="ck_memory_forgotten_redacted"),
        # A forgotten row must also SAY it is forgotten. Without this the two ways of asking — the status
        # column and the timestamp — could disagree, and retrieval reads the status.
        sa.CheckConstraint("(forgotten_at IS NULL) = (status <> 'forgotten')",
                           name="ck_memory_forgotten_status"),
    )
    # THE retrieval index. Stage 1 filters owner + still-believed + domain and orders by recency, so the
    # composite exists in exactly that order; without it every turn's shortlist is a sequential scan of
    # everything the student has ever told Bruce.
    op.create_index("ix_memory_shortlist", _RECORDS, ["user_id", "status", "domain", "observed_at"])
    op.create_index("ix_memory_entity", _RECORDS, ["user_id", "status", "entity_key"])

    # THE APPEND-ONLY TRIGGER, carried over from 0028 and re-expressed against the new columns.
    #
    # A correction must never edit a row. If Bruce acted on a fact and the student later says "no, it's
    # Ms. Nguyen", the question that matters afterwards is what Bruce believed AT THE TIME it acted — and
    # an overwrite answers "Ms. Nguyen", leaving the email that went to the wrong teacher inexplicable.
    # Enforced in the database rather than in one module, so raw SQL, a future module and a migration
    # author all hit the same wall.
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
       OR NEW.user_id IS DISTINCT FROM OLD.user_id THEN
        RAISE EXCEPTION 'memory_records is append-only; corrections supersede, they do not edit (memory %)', OLD.memory_id;
    END IF;
    RETURN NEW;
END;
$$""")
    op.execute(f"DROP TRIGGER IF EXISTS memory_records_no_edit ON {_RECORDS}")
    op.execute(f"CREATE TRIGGER memory_records_no_edit BEFORE UPDATE ON {_RECORDS} "
               f"FOR EACH ROW EXECUTE FUNCTION memory_records_append_only()")
    _rls(_RECORDS)

    op.create_table(
        _CORRECTIONS,
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("memory_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("replacement_memory_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_message_id", sa.String(255), nullable=True),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    _rls(_CORRECTIONS)

    op.create_table(
        _FORGETS,
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("target", sa.String(200), nullable=True, index=True),
        sa.Column("source_message_id", sa.String(255), nullable=True),
        sa.Column("record_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    _rls(_FORGETS)


def downgrade() -> None:
    op.drop_table(_FORGETS)
    op.drop_table(_CORRECTIONS)
    op.drop_table(_RECORDS)
