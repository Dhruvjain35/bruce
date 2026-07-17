"""integrations + oauth_states — connected accounts and one-time OAuth state, both under RLS

Adds the two tables the Google Calendar connect flow needs:

  * ``integrations``  — the connected account: encrypted refresh token, granted scopes, selected
    calendar, revocation state. One row per (user, provider).
  * ``oauth_states``  — one-time, short-lived, user-bound CSRF state carrying the PKCE verifier.
    This table is the security boundary of the callback: identity is read from the row, never from
    the attacker-controllable callback query string.

BOTH get the SAME isolation contract as every other user-owned table — ENABLE + FORCE RLS with a
USING and WITH CHECK policy on user_id, and DML-only grants to the restricted bruce_app role. A new
table without RLS would be a silent hole in a model whose entire guarantee is that Postgres, not
application code, enforces tenancy. That is why the policies are created here rather than left to
0002 (which already ran).

CONDITIONAL, for the same reason as 0003: ``0001_initial`` is not static DDL — it calls
Base.metadata.create_all() against the LIVE models, so a fresh database already has these tables
the moment they exist in schema.py, while an already-migrated database does not. Guarding on the
live inspector makes both paths converge. This is documented migration debt, NOT a pattern to
copy: 0001 should be frozen into static DDL post-hackathon, and until it is, every new migration
has to defend itself like this. See docs/deployment-verification.md.

Revision ID: 0004_integrations
Revises: 0003_source_idem
Create Date: 2026-07-17
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_integrations"
down_revision = "0003_source_idem"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
TABLES = ("integrations", "oauth_states")


def _existing() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _rls(table: str) -> None:
    """Same contract as migration 0002: ENABLE + FORCE + USING/WITH CHECK on user_id."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (user_id = app_current_user()) WITH CHECK (user_id = app_current_user())"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def upgrade() -> None:
    present = _existing()

    if "integrations" not in present:
        op.create_table(
            "integrations",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("provider_account_id", sa.String(320), nullable=True),
            sa.Column("scopes", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            # Fernet ciphertext only. Never plaintext.
            sa.Column("refresh_token_encrypted", sa.Text, nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("selected_calendar_id", sa.String(255), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="connected"),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.UniqueConstraint("user_id", "provider", name="uq_integration_user_provider"),
        )

    if "oauth_states" not in present:
        op.create_table(
            "oauth_states",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                      server_default=sa.text("gen_random_uuid()")),
            sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("state", sa.String(128), nullable=False, unique=True, index=True),
            sa.Column("code_verifier", sa.String(128), nullable=False),
            sa.Column("redirect_uri", sa.String(500), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    # RLS is applied whether or not create_all built the table, because create_all does NOT create
    # policies — a fresh DB would otherwise get these tables with NO isolation at all.
    insp = sa.inspect(op.get_bind())
    for t in TABLES:
        existing_policies = op.get_bind().execute(
            sa.text("SELECT policyname FROM pg_policies WHERE tablename = :t"), {"t": t}
        ).scalars().all()
        if "tenant_isolation" not in existing_policies:
            _rls(t)


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"DROP TABLE IF EXISTS {t}")
