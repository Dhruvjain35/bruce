"""school connector — canonical academic graph (courses/assignments/submissions/...) under RLS

Thirteen provider-neutral tables holding a student's synced school data: the sync-cursor position, the
source/source_span evidence anchors, the course graph (institutions/terms/instructors/courses), the
assignment + submission + material + announcement + schedule-event objects, and the change-history log.

tenant_isolation RLS (USING/WITH CHECK user_id = app_current_user()) — every row is owned by exactly one
student and is only ever written under user_session(user_id); there is NO worker/service path into this
data (unlike the messaging domain), so no tenant_or_worker policy. Conditional create because 0001 runs
Base.metadata.create_all() and would otherwise build these policy-less on a fresh DB — a cross-tenant hole
for a student's entire academic record. school_sources first (others FK to it via source_id).

Revision ID: 0012_school_connector
Revises: 0011_conversation_runtime
Create Date: 2026-07-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_school_connector"
down_revision = "0011_conversation_runtime"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"

# Ordered so a table is created only after any table it references (school_sources before the rest).
TABLES = (
    "school_sync_cursors", "school_sources", "school_source_spans", "school_institutions",
    "school_terms", "school_instructors", "school_courses", "school_assignments", "school_materials",
    "school_announcements", "school_submissions", "school_schedule_events", "school_object_changes",
)

UUID = sa.dialects.postgresql.UUID
JSONB = sa.dialects.postgresql.JSONB


def _existing() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _ts():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    ]


def _pk():
    return sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _owner():
    return sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)


def _prov():
    """Provenance columns shared by every canonical school-object table (matches schema._SchoolProv)."""
    return [
        sa.Column("provider", sa.String(32), nullable=False, server_default="canvas"),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("source_url", sa.String(1000), nullable=True),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("capability_state", sa.String(16), nullable=False, server_default="supported"),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("school_sources.id", ondelete="SET NULL"), nullable=True),
    ]


def _rls(table: str) -> None:
    policies = op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()
    if "tenant_isolation" not in policies:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} "
                   f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    present = _existing()

    if "school_sync_cursors" not in present:
        op.create_table("school_sync_cursors", _pk(), _owner(),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("resource", sa.String(48), nullable=False),
            sa.Column("cursor_value", sa.String(255), nullable=True),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
            *_ts(),
            sa.UniqueConstraint("user_id", "provider", "resource", name="uq_school_cursor"))

    if "school_sources" not in present:
        op.create_table("school_sources", _pk(), _owner(),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("object_type", sa.String(32), nullable=False),
            sa.Column("provider_id", sa.String(128), nullable=False),
            sa.Column("source_url", sa.String(1000), nullable=True),
            sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("capability_state", sa.String(16), nullable=False, server_default="supported"),
            sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            *_ts(),
            sa.UniqueConstraint("user_id", "provider", "object_type", "provider_id", name="uq_school_source"))

    if "school_source_spans" not in present:
        op.create_table("school_source_spans", _pk(), _owner(),
            sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("school_sources.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("span_text", sa.Text, nullable=False),
            sa.Column("label", sa.String(64), nullable=True),
            sa.Column("ordinal", sa.Integer, nullable=True),
            *_ts())

    if "school_institutions" not in present:
        op.create_table("school_institutions", _pk(), _owner(),
            sa.Column("name", sa.String(300), nullable=False),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_institution"))

    if "school_terms" not in present:
        op.create_table("school_terms", _pk(), _owner(),
            sa.Column("name", sa.String(300), nullable=False),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.text("false")),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_term"))

    if "school_instructors" not in present:
        op.create_table("school_instructors", _pk(), _owner(),
            sa.Column("name", sa.String(300), nullable=False),
            sa.Column("email", sa.String(320), nullable=True),
            sa.Column("role", sa.String(64), nullable=True),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_instructor"))

    if "school_courses" not in present:
        op.create_table("school_courses", _pk(), _owner(),
            sa.Column("name", sa.String(500), nullable=False),
            sa.Column("course_code", sa.String(120), nullable=True),
            sa.Column("workflow_state", sa.String(32), nullable=True, index=True),
            sa.Column("term_provider_id", sa.String(128), nullable=True),
            sa.Column("term_name", sa.String(300), nullable=True),
            sa.Column("institution_provider_id", sa.String(128), nullable=True),
            sa.Column("url", sa.String(1000), nullable=True),
            sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_course"))

    if "school_assignments" not in present:
        op.create_table("school_assignments", _pk(), _owner(),
            sa.Column("course_provider_id", sa.String(128), nullable=False, index=True),
            sa.Column("name", sa.String(500), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True, index=True),
            sa.Column("unlock_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lock_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("points_possible", sa.Float, nullable=True),
            sa.Column("submission_types", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("submission_state", sa.String(16), nullable=False, server_default="unknown", index=True),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("score", sa.Float, nullable=True),
            sa.Column("grade", sa.String(32), nullable=True),
            sa.Column("late", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("missing", sa.Boolean, nullable=False, server_default=sa.text("false")),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_assignment"))

    if "school_materials" not in present:
        op.create_table("school_materials", _pk(), _owner(),
            sa.Column("course_provider_id", sa.String(128), nullable=False, index=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False, server_default="file"),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_material"))

    if "school_announcements" not in present:
        op.create_table("school_announcements", _pk(), _owner(),
            sa.Column("course_provider_id", sa.String(128), nullable=False, index=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("message", sa.Text, nullable=True),
            sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True, index=True),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_announcement"))

    if "school_submissions" not in present:
        op.create_table("school_submissions", _pk(), _owner(),
            sa.Column("assignment_provider_id", sa.String(128), nullable=False, index=True),
            sa.Column("state", sa.String(16), nullable=False, server_default="unknown"),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempt", sa.Integer, nullable=True),
            sa.Column("late", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("missing", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("score", sa.Float, nullable=True),
            sa.Column("grade", sa.String(32), nullable=True),
            sa.Column("points_possible", sa.Float, nullable=True),
            sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rubric", JSONB, nullable=True),
            sa.Column("feedback", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_submission"))

    if "school_schedule_events" not in present:
        op.create_table("school_schedule_events", _pk(), _owner(),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=True, index=True),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("location", sa.String(500), nullable=True),
            sa.Column("course_provider_id", sa.String(128), nullable=True),
            *_prov(), *_ts(),
            sa.UniqueConstraint("user_id", "provider", "provider_id", name="uq_school_schedule_event"))

    if "school_object_changes" not in present:
        op.create_table("school_object_changes", _pk(), _owner(),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("object_type", sa.String(32), nullable=False),
            sa.Column("provider_id", sa.String(128), nullable=False),
            sa.Column("change_type", sa.String(16), nullable=False),
            sa.Column("cursor_value", sa.String(255), nullable=True),
            sa.Column("changed_fields", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Index("ix_school_change_lookup", "user_id", "object_type", "provider_id"))

    # RLS whether or not create_all built the tables (create_all never creates policies).
    for t in TABLES:
        _rls(t)


def downgrade() -> None:
    for t in reversed(TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
