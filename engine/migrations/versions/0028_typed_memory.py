"""memory_records — typed personal memory (#121b): one row per thing Bruce believes about one student.

tenant_isolation RLS + FORCE ROW LEVEL SECURITY, like every other user table. What is different here is
that three of the module's guarantees are repeated as database constraints rather than left to the
writer, because a memory row outlives the code path that produced it and will eventually be written by
something that is not `memory_writer`:

  * ck_memory_trusted_source     only the student's own trusted text becomes a memory. Quoted, forwarded,
                                 attached, provider and model content is evidence, never a source. This
                                 is the 30-of-38 adversarial-corpus defect, expressed as a CHECK.
  * ck_memory_forgotten_redacted a forgotten row that still carries content cannot be committed. Forget
                                 erases; it does not hide.
  * memory_records_append_only   a BEFORE UPDATE trigger that permits changes to exactly four lifecycle
                                 columns (superseded_by, contradicted_by, last_confirmed_at, and the
                                 forget redaction). Corrections therefore cannot rewrite history from
                                 psql, from a script, or from a future module.

Revision ID: 0028_typed_memory
Revises: 0027_authorization_evidence
Create Date: 2026-07-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0028_typed_memory"
down_revision = "0027_authorization_evidence"
branch_labels = None
depends_on = None
APP_ROLE = "bruce_app"
_TABLE = "memory_records"

# Content columns are NULLABLE only so that forgetting can erase them; a LIVE row must carry all of them
# (ck_memory_live_has_content). Splitting the two conditions keeps the error message specific — "this row
# was forgotten and still has content" and "this row is live and has none" are different bugs.
_CONTENT = ("subject", "predicate", "value", "evidence", "reason_it_matters", "entity_key", "domain")

_IMMUTABLE = ("id", "user_id", "kind", "subject", "predicate", "value", "evidence",
              "reason_it_matters", "source_message_id", "source_type", "confidence", "observed_at",
              "retention_policy", "sensitivity", "user_editable", "entity_key", "domain",
              "forgotten_at", "forget_scope", "created_at")

_APPEND_ONLY_FN = f"""
CREATE OR REPLACE FUNCTION memory_records_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- The forget path: content may only be REMOVED, never rewritten under cover of a redaction.
    IF OLD.forgotten_at IS NULL AND NEW.forgotten_at IS NOT NULL THEN
        IF {" OR ".join(f"NEW.{c} IS NOT NULL" for c in _CONTENT)} THEN
            RAISE EXCEPTION 'forgetting must erase content, not rewrite it (memory %)', OLD.id;
        END IF;
        RETURN NEW;
    END IF;
    -- Everything else: only supersession, contradiction and re-confirmation may move.
    IF {" OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in _IMMUTABLE)} THEN
        RAISE EXCEPTION 'memory_records is append-only; corrections supersede, they do not edit (memory %)', OLD.id;
    END IF;
    RETURN NEW;
END $$;
"""


def _rls() -> None:
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": _TABLE}).scalars().all()
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {APP_ROLE}")
    if "tenant_isolation" not in policies:
        op.execute(f"CREATE POLICY tenant_isolation ON {_TABLE} "
                   f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())")


def upgrade() -> None:
    UUID = sa.dialects.postgresql.UUID
    JSONB = sa.dialects.postgresql.JSONB
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if _TABLE not in present:
        op.create_table(_TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            # One of the six layers. `style` is disjoint from the five factual ones and is kept out of
            # factual retrieval by ck_memory_style_predicate + the FACTUAL filter in memory_retrieval.
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("subject", sa.String(200), nullable=True),
            sa.Column("predicate", sa.String(100), nullable=True),
            sa.Column("value", sa.String(300), nullable=True),
            sa.Column("evidence", JSONB, nullable=True),
            sa.Column("reason_it_matters", sa.String(300), nullable=True),
            sa.Column("source_message_id", sa.String(255), nullable=True),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("confidence", sa.Float, nullable=False, server_default=sa.text("1.0")),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("retention_policy", sa.String(16), nullable=False),
            sa.Column("sensitivity", sa.String(16), nullable=False, server_default="ordinary"),
            sa.Column("user_editable", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("contradicted_by", UUID(as_uuid=True), sa.ForeignKey(f"{_TABLE}.id", ondelete="SET NULL"), nullable=True),
            sa.Column("superseded_by", UUID(as_uuid=True), sa.ForeignKey(f"{_TABLE}.id", ondelete="SET NULL"), nullable=True),
            # Derived from subject/predicate by the writer. Denormalized so the first retrieval stage is
            # an index scan; never authored independently of the columns they come from.
            sa.Column("entity_key", sa.String(200), nullable=True),
            sa.Column("domain", sa.String(32), nullable=True),
            # Forgetting. Set in the SAME statement that NULLs every content column above.
            sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("forget_scope", sa.String(16), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.CheckConstraint(
                "kind IN ('profile','relationships','world','entity','episodic','style')",
                name="ck_memory_kind"),
            # THE hard rule, at the database. A forwarded "my teacher is Ms. Delgado" has no way to become
            # a user fact even if every Python guard above it is removed.
            sa.CheckConstraint("source_type = 'trusted_user_text'", name="ck_memory_trusted_source"),
            sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memory_confidence"),
            sa.CheckConstraint("superseded_by IS NULL OR superseded_by <> id",
                               name="ck_memory_no_self_supersede"),
            # Style is a separate kind AND a separate predicate namespace; neither can carry the other's.
            sa.CheckConstraint("predicate IS NULL OR (kind = 'style') = (predicate LIKE 'style.%')",
                               name="ck_memory_style_predicate"),
            sa.CheckConstraint(
                "forgotten_at IS NOT NULL OR ("
                + " AND ".join(f"{c} IS NOT NULL" for c in ("subject", "predicate", "value", "evidence"))
                + ")", name="ck_memory_live_has_content"),
            sa.CheckConstraint(
                "forgotten_at IS NULL OR ("
                + " AND ".join(f"{c} IS NULL" for c in _CONTENT) + ")",
                name="ck_memory_forgotten_redacted"))
        op.create_index("ix_memory_user_kind_entity", _TABLE, ["user_id", "kind", "entity_key"])
        op.create_index("ix_memory_user_domain", _TABLE, ["user_id", "domain"])
        op.create_index("ix_memory_user_source", _TABLE, ["user_id", "source_message_id"])
        op.create_index("ix_memory_user_recency", _TABLE, ["user_id", "last_confirmed_at", "observed_at"])
    op.execute(_APPEND_ONLY_FN)
    op.execute(f"DROP TRIGGER IF EXISTS memory_records_no_edit ON {_TABLE}")
    op.execute(f"CREATE TRIGGER memory_records_no_edit BEFORE UPDATE ON {_TABLE} "
               f"FOR EACH ROW EXECUTE FUNCTION memory_records_append_only()")
    _rls()


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS memory_records_no_edit ON {_TABLE}")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    op.execute("DROP FUNCTION IF EXISTS memory_records_append_only()")
