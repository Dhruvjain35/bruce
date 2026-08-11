"""known_people has had NO ROW SECURITY AT ALL. This applies it.

THE INCIDENT. Migration 0032 opens with `if bind.dialect.has_table(bind, TABLE): return`, and
`0001_initial_schema` builds every table with `Base.metadata.create_all()`. So on every database that has
ever been built that way — which is all of them — 0032 returned before reaching its own
`ENABLE ROW LEVEL SECURITY`, and `pg_class` reports relrowsecurity=false, relforcerowsecurity=false. Any
tenant session could read every student's recipient book: the set of people they write to and the
addresses to reach them at. That is the most sensitive relational data Bruce holds after the turn text
itself.

The hole was uncovered, not created, by adding this table to the enforced RLS inventory — which is the
whole argument for having an inventory.

IT IS APPLIED UNCONDITIONALLY AND IDEMPOTENTLY, the pattern 0033 spells out and the pattern 0032 itself
did not follow. A policy guarded by "does the table exist" is missing in exactly the environment nobody
inspects, because that is the environment where `create_all()` already made the table.

ENABLE IS NOT ENOUGH: FORCE is what subjects the TABLE OWNER to the policy. Without it the migration
role, and anything running as it, reads every student's recipient book regardless of the policy text.

THE EXISTING POLICY IS REPLACED, NOT JOINED. Two permissive policies on a table are OR'd, so leaving
0032's version beside this one would let the older, looser predicate silently widen the newer one. The
replacement says the same thing with the repo's fail-closed helpers — `app_current_user()` /
`app_is_worker()` / `app_is_admin()` rather than a raw `current_setting(...)::uuid`, so a malformed or
absent context fails CLOSED instead of raising inside the policy.

Revision ID: 0034_known_people_row_security
Revises: 0033_semantic_shadow_jobs
Create Date: 2026-08-08
"""
import sqlalchemy as sa
from alembic import op

revision = "0034_known_people_row_security"
down_revision = "0033_semantic_shadow_jobs"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
TABLE = "known_people"
POLICY = "known_people_owner"


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE not in set(sa.inspect(bind).get_table_names()):
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} "
        "USING (user_id = app_current_user() OR app_is_worker() OR app_is_admin()) "
        "WITH CHECK (user_id = app_current_user() OR app_is_worker() OR app_is_admin())")


def downgrade() -> None:
    """Deliberately empty of the destructive half.

    Taking row security off a student's recipient book on the way back would turn a rollback into a
    data-exposure incident, and nothing below this revision depends on the policy being absent.
    """
    return
