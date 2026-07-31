"""known_people — the only source of a recipient Bruce did not read in the current sentence.

WHY THIS TABLE EXISTS. `goal_slots.Fill.resolvable` declares that a recipient may be LOOKED UP and never
invented. Until now nothing could look one up: `turn_context.people` was a declared shape nobody
populated, `gmail.resolve_recipient` is live=False, and the only resolution in the tree was an explicit
address in the current message or a self-reference. So "email my teacher and thank her" could only ever
be a question, however well Bruce understood the rest of it.

A row exists ONLY because the student said, in their own trusted words, that a person has an address.
Nothing here is inferred from a signature, a header, or a forwarded message — those are somebody else's
correspondents, and the whole authorization corpus is full of what happens when that distinction is lost.

NOT A HARD DELETE, TWICE OVER. A correction writes a NEW row and points the old one at it
(`superseded_by_id`), and forgetting stamps `forgotten_at` rather than removing anything. Resolution
reads neither. Both choices exist so "why did Bruce think that" and "why did it stop" stay answerable
after the fact — an address is acted on far from the turn that supplied it.

CREATED UNCONDITIONALLY, for the reason 0030 and 0031 both spell out: `0001_initial_schema` builds tables
with `Base.metadata.create_all()`, so a migration that skips itself on a fresh database leaves the table
missing in exactly the environment nobody inspects.

Revision ID: 0032_known_people
Revises: 0031_agent_run_status_check
Create Date: 2026-07-31
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0032_known_people"
down_revision = "0031_agent_run_status_check"
branch_labels = None
depends_on = None

TABLE = "known_people"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.has_table(bind, TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("normalized_name", sa.String(200), nullable=False, index=True),
        sa.Column("relationship", sa.String(120), nullable=True, index=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("source_message_id", sa.String(255), nullable=True),
        sa.Column("stated_span", sa.Text(), nullable=True),
        sa.Column("provenance", sa.String(32), nullable=False, server_default="user_stated"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("superseded_by_id", UUID(as_uuid=True),
                  sa.ForeignKey("known_people.id", ondelete="SET NULL"), nullable=True),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_index("ix_known_people_user_live", TABLE, ["user_id", "normalized_name"])

    # TENANT ISOLATION, like every other user-owned table here. A recipient book is among the most
    # sensitive things Bruce holds: it is the set of people this student writes to.
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"""
        CREATE POLICY {TABLE}_owner ON {TABLE}
          USING (user_id = current_setting('app.user_id', true)::uuid
                 OR current_setting('app.worker', true) = 'on'
                 OR current_setting('app.admin', true) = 'on')
          WITH CHECK (user_id = current_setting('app.user_id', true)::uuid
                      OR current_setting('app.worker', true) = 'on'
                      OR current_setting('app.admin', true) = 'on')
    """)


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
