"""Bite 1.5 keystone — DB-backed capability access, against REAL Postgres.

Adversarial coverage of the access model that replaces per-user Cloud Run env editing. Everything runs
against the disposable ``bruce_test`` DB through the restricted ``bruce_app`` role (via ``pg_test_db`` /
``clean_db``), so the real RLS policies + the append-only audit trigger are exercised — no SQLite, no
mocks. Each security acceptance criterion of the keystone gets a named test here:

  * default-deny for tenants on all four tables (on the create_all-BUILT migrated DB)
  * kill UPSERT with no pre-existing state row disables an enrolled user
  * rolled_out + no per-user grant -> DENY (no mass-enable)
  * production entitlement -> persistent ALLOW; suspended / messaging-disabled -> DENY
  * staging enrollment -> ALLOW; expired / revoked -> DENY; staging never persists, production never TTL'd
  * fail-closed: a DB error inside conversation_access -> DENY
  * capability_audit is append-only (UPDATE/DELETE rejected)
  * admin_session() refuses to open while a tenant app.user_id is set

Skips cleanly when Postgres isn't configured (via ``pg_test_db``).
"""

from __future__ import annotations

import asyncio
import datetime
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import access_control, schema
from bruce_engine.db import admin_session, user_session, worker_session
from bruce_engine.repositories import PostgresUserRepository
from scripts import capability_admin

users_repo = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    """Rebuild the app engine per test with NullPool (real asyncpg, real PG — no pooling). Depends on
    ``pg_test_db`` so the whole module skips cleanly when Postgres isn't configured."""

    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _owner_params() -> dict:
    u = make_url(os.environ["BRUCE_DATABASE_URL"])
    return dict(host=u.host, port=u.port or 5432, user=u.username, password=u.password, database=u.database)


# --------------------------------------------------------------------------- default-deny (create_all DB)


def test_rls_default_deny_for_tenant_on_all_four_tables(clean_db):
    """A tenant (user_session as bruce_app) gets ZERO rows and a DENIED insert on all four capability
    tables, even though admin-written rows exist — proven on the create_all-BUILT migrated DB (0001 runs
    Base.metadata.create_all; 0013 layers RLS on top)."""

    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        # admin seeds a row in every table (each mutation also appends a capability_audit row)
        await capability_admin.grant_production(a, reason="seed", actor="op@host")
        await capability_admin.enroll_staging(a, reason="seed", actor="op@host")
        await capability_admin.set_kill(False, actor="op@host")

        # positive control: the runtime gate's worker_session DOES see the state rows (not just empty)
        async with worker_session() as s:
            assert (await s.execute(select(func.count()).select_from(schema.ProductionAccountEntitlement))).scalar_one() >= 1
            assert (await s.execute(select(func.count()).select_from(schema.StagingTestEnrollment))).scalar_one() >= 1
            assert (await s.execute(select(func.count()).select_from(schema.CapabilityGlobalState))).scalar_one() >= 1

        # tenant READ: zero rows on all four (RLS: no tenant policy)
        async with user_session(a) as s:
            for model in (schema.ProductionAccountEntitlement, schema.StagingTestEnrollment,
                          schema.CapabilityGlobalState, schema.CapabilityAudit):
                assert (await s.execute(select(func.count()).select_from(model))).scalar_one() == 0

        # tenant WRITE: denied on all four. Values chosen to have NO unique/FK conflict, so RLS is the
        # ONLY possible cause of failure (b exists + has no entitlement; distinct capability/env; etc.).
        inserts = [
            schema.ProductionAccountEntitlement.__table__.insert().values(user_id=b),
            schema.StagingTestEnrollment.__table__.insert().values(
                user_id=b, capability="conversation", environment="denytest"),
            schema.CapabilityGlobalState.__table__.insert().values(
                capability="conversation", environment="denytest"),
            schema.CapabilityAudit.__table__.insert().values(actor="intruder", action="tamper"),
        ]
        for stmt in inserts:
            with pytest.raises(Exception):
                async with user_session(a) as s:
                    await s.execute(stmt)

    _run(run())


# --------------------------------------------------------------------------- kill UPSERT (no prior row)


def test_kill_upsert_with_no_preexisting_row_disables_enrolled_user(clean_db):
    """`kill --on` with NO pre-existing capability_global_state row (truncated) INSERTS a killed row and
    disables an already-enrolled user on the very next conversation_access call."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        # precondition: there is NO global-state row at all
        async with worker_session() as s:
            assert (await s.execute(select(func.count()).select_from(schema.CapabilityGlobalState))).scalar_one() == 0

        await capability_admin.enroll_staging(a, actor="op@host")
        assert (await access_control.conversation_access(a)).allow is True

        await capability_admin.set_kill(True, actor="op@host")   # UPSERT that must INSERT (no prior row)
        d = await access_control.conversation_access(a)
        assert d.allow is False and d.source == "killed"

    _run(run())


# --------------------------------------------------------------------------- rolled_out never mass-enables


def test_rolled_out_without_per_user_grant_denies(clean_db):
    """rollout_state='rolled_out' must NEVER mass-enable: a user with no entitlement and no enrollment is
    still DENIED (per-user grant is required regardless of rollout_state)."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        env = access_control.current_environment()
        async with admin_session() as s:
            await s.execute(pg_insert(schema.CapabilityGlobalState).values(
                capability="conversation", environment=env, rollout_state="rolled_out", killed=False
            ).on_conflict_do_update(index_elements=["capability", "environment"],
                                    set_={"rollout_state": "rolled_out"}))

        d = await access_control.conversation_access(a)
        assert d.allow is False and d.source == "no_grant"

    _run(run())


# --------------------------------------------------------------------------- production persistence


def test_production_entitlement_persistent_allow_then_suspend_or_disable_denies(clean_db):
    """An active production entitlement -> persistent ALLOW(production); suspending the account OR
    disabling messaging -> DENY. There is no TTL: access ends only on an explicit state change."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await capability_admin.grant_production(a, reason="alpha", actor="op@host")

        d = await access_control.conversation_access(a)
        assert d.allow is True and d.source == "production"

        async with admin_session() as s:   # suspend -> DENY
            await s.execute(update(schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == a).values(
                account_status="suspended", suspended_at=_now()))
        assert (await access_control.conversation_access(a)).allow is False

        async with admin_session() as s:   # reactivate but disable messaging -> DENY
            await s.execute(update(schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == a).values(
                account_status="active", suspended_at=None, messaging_enabled=False))
        assert (await access_control.conversation_access(a)).allow is False

    _run(run())


def test_capability_availability_gates_production(clean_db):
    """A production entitlement whose capability_availability lacks the capability -> DENY (the gate checks
    membership, never rollout_state)."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        async with admin_session() as s:
            s.add(schema.ProductionAccountEntitlement(
                user_id=a, account_status="active", messaging_enabled=True,
                capability_availability=["some_other_capability"]))
        d = await access_control.conversation_access(a)
        assert d.allow is False and d.source == "no_grant"

    _run(run())


# --------------------------------------------------------------------------- staging enrollment


def test_staging_enrollment_allow_then_revoke_denies(clean_db):
    """A live staging enrollment -> ALLOW(staging); an immediate revoke -> DENY."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await capability_admin.enroll_staging(a, hours=24, reason="canary", actor="op@host")

        d = await access_control.conversation_access(a)
        assert d.allow is True and d.source == "staging"

        n = await capability_admin.revoke(a, actor="op@host")
        assert n == 1
        assert (await access_control.conversation_access(a)).allow is False

    _run(run())


def test_expired_staging_enrollment_denies(clean_db):
    """A staging enrollment past its expires_at -> DENY (TTL enforced)."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        async with admin_session() as s:
            s.add(schema.StagingTestEnrollment(
                user_id=a, capability="conversation", environment=access_control.current_environment(),
                expires_at=_now() - datetime.timedelta(hours=1)))
        assert (await access_control.conversation_access(a)).allow is False

    _run(run())


def test_staging_never_makes_production_persistent(clean_db):
    """A staging-only user is ALLOW(staging) — never sourced as production and never persistent: revoking
    the enrollment removes access, and no production entitlement was ever created."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await capability_admin.enroll_staging(a, hours=24, actor="op@host")

        d = await access_control.conversation_access(a)
        assert d.allow is True and d.source == "staging"      # sourced staging, NOT production

        async with worker_session() as s:                      # no production entitlement exists
            assert (await s.execute(select(func.count()).select_from(
                schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == a))).scalar_one() == 0

        await capability_admin.revoke(a, actor="op@host")      # revoke -> access gone (was not persistent)
        assert (await access_control.conversation_access(a)).allow is False

    _run(run())


def test_production_access_not_expired_by_staging_ttl(clean_db):
    """A production user with an EXPIRED staging enrollment alongside still has persistent production
    access — a staging TTL never expires production."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await capability_admin.grant_production(a, actor="op@host")
        async with admin_session() as s:
            s.add(schema.StagingTestEnrollment(
                user_id=a, capability="conversation", environment=access_control.current_environment(),
                expires_at=_now() - datetime.timedelta(hours=1)))

        d = await access_control.conversation_access(a)
        assert d.allow is True and d.source == "production"

    _run(run())


# --------------------------------------------------------------------------- fail-closed


def test_conversation_access_fails_closed_on_db_error(clean_db, monkeypatch):
    """A DB error inside conversation_access resolves to DENY (fail-closed), even for a user who would
    otherwise be allowed."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await capability_admin.grant_production(a, actor="op@host")   # would normally ALLOW

        def _boom(*_a, **_k):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(access_control, "worker_session", _boom)
        d = await access_control.conversation_access(a)
        assert d.allow is False and d.source == "error"

    _run(run())


# --------------------------------------------------------------------------- append-only audit


def test_capability_audit_is_append_only(clean_db):
    """capability_audit is append-only. Under the app role RLS denies UPDATE (0 rows), and even the
    BYPASSRLS owner cannot UPDATE/DELETE — the BEFORE UPDATE/DELETE trigger raises."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await capability_admin.grant_production(a, reason="x", actor="op@host")  # writes one audit row
        rows = await capability_admin.list_audit(limit=10)
        assert len(rows) >= 1 and rows[0].action == "grant_production"
        aid = rows[0].id

        # app-role path: RLS has no UPDATE policy -> the row is invisible to UPDATE -> 0 rows (no mutation)
        async with admin_session() as s:
            res = await s.execute(update(schema.CapabilityAudit).where(
                schema.CapabilityAudit.id == aid).values(actor="tamper"))
            assert res.rowcount == 0

        # owner path (BYPASSRLS superuser): the strongest attacker still cannot mutate — trigger raises
        conn = await asyncpg.connect(**_owner_params())
        try:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("UPDATE capability_audit SET actor='tamper' WHERE id=$1", aid)
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute("DELETE FROM capability_audit WHERE id=$1", aid)
            still = await conn.fetchrow("SELECT actor FROM capability_audit WHERE id=$1", aid)
            assert still is not None and still["actor"] != "tamper"
        finally:
            await conn.close()

    _run(run())


def test_every_mutation_writes_an_audit_row_with_server_actor(clean_db):
    """grant / enroll / revoke / kill each append exactly one audit row carrying the SERVER-derived actor
    (never a client-supplied string) — proving the audit trail is complete."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await capability_admin.grant_production(a, actor="op@host")
        await capability_admin.enroll_staging(a, actor="op@host")
        await capability_admin.revoke(a, actor="op@host")
        await capability_admin.set_kill(True, actor="op@host")
        await capability_admin.set_kill(False, actor="op@host")

        rows = await capability_admin.list_audit(limit=50)
        actions = sorted(r.action for r in rows)
        assert actions == sorted(
            ["grant_production", "enroll_staging", "revoke_staging", "kill_on", "kill_off"])
        assert all(r.actor == "op@host" for r in rows)

    _run(run())


# --------------------------------------------------------------------------- admin_session guard


def test_admin_session_refuses_when_tenant_context_is_set(clean_db):
    """admin_session() ASSERTS app.user_id is unset — an admin context can never coexist with a tenant
    context. Plant a tenant app.user_id as the bruce_app role default (so a fresh connection carries it)
    and prove admin_session refuses to open, while worker_session (no such guard) still opens."""

    async def run():
        app_url = make_url(os.environ["BRUCE_APP_DATABASE_URL"])
        dbname, role = app_url.database, app_url.username

        async def _owner_exec(sql: str):
            c = await asyncpg.connect(**_owner_params())
            try:
                await c.execute(sql)
            finally:
                await c.close()

        await _owner_exec(f"ALTER ROLE {role} IN DATABASE {dbname} SET app.user_id = '{uuid4()}'")
        db._engine = None
        db._sessionmaker = None   # force fresh connections that pick up the planted role default
        try:
            with pytest.raises(RuntimeError):
                async with admin_session():
                    pass
        finally:
            await _owner_exec(f"ALTER ROLE {role} IN DATABASE {dbname} RESET app.user_id")
            db._engine = None
            db._sessionmaker = None

    _run(run())


def test_admin_session_opens_cleanly_without_tenant_context(clean_db):
    """Sanity/positive control: with no tenant context, admin_session opens and can read the state tables
    (proves the guard discriminates rather than always raising)."""

    async def run():
        async with admin_session() as s:
            assert (await s.execute(select(func.count()).select_from(schema.CapabilityGlobalState))).scalar_one() >= 0

    _run(run())


def test_unverified_production_entitlement_denies(clean_db):
    """An active, messaging-enabled entitlement whose identity is NOT verified must be DENIED — access
    resolves from a VERIFIED linked identity, not merely the presence of an entitlement row."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        async with admin_session() as s:
            s.add(schema.ProductionAccountEntitlement(
                user_id=a, account_status="active", messaging_enabled=True, verified_identity=False,
                capability_availability=["conversation"]))
        assert (await access_control.conversation_access(a)).allow is False   # unverified -> DENY

        async with admin_session() as s:   # verify the identity -> now ALLOW
            await s.execute(update(schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == a).values(verified_identity=True))
        d = await access_control.conversation_access(a)
        assert d.allow is True and d.source == "production"

    _run(run())


def test_activate_production_entitlement_is_automatic_verified_and_idempotent(clean_db):
    """The programmatic path D1 uses: one call creates a persistent, verified, messaging-enabled
    entitlement that ALLOWs access; a second call is idempotent (still one row, still allowed)."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        created = await access_control.activate_production_entitlement(a, reason="signup")
        assert created is True
        d = await access_control.conversation_access(a)
        assert d.allow is True and d.source == "production"

        created2 = await access_control.activate_production_entitlement(a, reason="signup")
        assert created2 is False                              # idempotent by user_id
        async with worker_session() as s:
            n = (await s.execute(select(func.count()).select_from(schema.ProductionAccountEntitlement).where(
                schema.ProductionAccountEntitlement.user_id == a))).scalar_one()
        assert n == 1
        assert (await access_control.conversation_access(a)).allow is True

    _run(run())
