"""magic-link single-use tokens (E1) — atomic, server-side one-time consumption

The founder's internal-test sign-in link must be single-use, not merely short-TTL. This adds one
admin-only table, ``magic_link_tokens``: only the sha256 HASH of the token's random jti is stored
(never the raw JWT or the sign-in URL), bound to (user, environment) with issued/expiry times and a
``consumed_at``. ``/internal/test/auth`` atomically consumes the matching UNUSED, unexpired, same-user,
same-environment row (``UPDATE ... WHERE consumed_at IS NULL ...``), so a reuse — or a concurrent
double-open — yields exactly one session (the row lock admits exactly one writer).

Same keystone pattern as 0013/0016: unconditional ENABLE + FORCE ROW LEVEL SECURITY + explicit GRANT
(create_all builds RLS-OFF; the app role has a blanket DML grant), separate from the pg_policies
CREATE-POLICY guard. Admin-only RLS (mint + consume run in an admin_session, like the capability
control tables). NO seed row, so the migration writes nothing under RLS.

Revision ID: 0017_magic_link_single_use
Revises: 0016_relay_bootstrap
Create Date: 2026-07-21
"""
import sqlalchemy as sa
from alembic import op

revision = "0017_magic_link_single_use"
down_revision = "0016_relay_bootstrap"
branch_labels = None
depends_on = None

APP_ROLE = "bruce_app"
UUID = sa.dialects.postgresql.UUID
TABLE = "magic_link_tokens"


def _insp():
    return sa.inspect(op.get_bind())


def _has_policy(table: str, name: str) -> bool:
    return name in op.get_bind().execute(
        sa.text("SELECT policyname FROM pg_policies WHERE tablename=:t"), {"t": table}).scalars().all()


def upgrade() -> None:
    present = set(_insp().get_table_names())

    if TABLE not in present:
        op.create_table(
            TABLE,
            sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("jti_hash", sa.String(64), nullable=False, index=True),      # sha256 of the token jti
            sa.Column("user_id", UUID(as_uuid=True), nullable=False),              # founder the link signs in
            sa.Column("environment", sa.String(24), nullable=False),               # binds the link to an env
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),   # short-lived
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),   # set exactly once, atomically
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("jti_hash", name="uq_magic_link_jti_hash"))

    # Unconditional + idempotent (create_all builds the table RLS-OFF; re-runs are harmless).
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO {APP_ROLE}")

    # Admin-only: mint (INSERT) and consume (UPDATE) both run in an admin_session (app_is_admin()).
    if not _has_policy(TABLE, "magic_admin_select"):
        op.execute(f"CREATE POLICY magic_admin_select ON {TABLE} FOR SELECT USING (app_is_admin())")
    if not _has_policy(TABLE, "magic_admin_insert"):
        op.execute(f"CREATE POLICY magic_admin_insert ON {TABLE} FOR INSERT WITH CHECK (app_is_admin())")
    if not _has_policy(TABLE, "magic_admin_update"):
        op.execute(f"CREATE POLICY magic_admin_update ON {TABLE} "
                   f"FOR UPDATE USING (app_is_admin()) WITH CHECK (app_is_admin())")


def downgrade() -> None:
    for p in ("magic_admin_select", "magic_admin_insert", "magic_admin_update"):
        op.execute(f"DROP POLICY IF EXISTS {p} ON {TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
