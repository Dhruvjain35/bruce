"""INCREMENT A — adversarial REAL-Postgres integration tests for tenant isolation / RLS.

Everything here runs against a disposable ``bruce_test`` database (provisioned + migrated by the
``pg_test_db`` fixture in conftest) and asserts THROUGH the restricted ``bruce_app`` role, so the
actual Postgres row-level-security policies are exercised — no SQLite, no mocks, no in-memory
repositories. The real repositories (``PostgresUserRepository`` / ``PostgresSourceRepository`` /
``PostgresTaskRepository`` / ``PostgresMissionRepository``) and the real FastAPI app
(``TestClient(bruce_engine.api.app)``) with minted HS256 JWTs are the surfaces under test.

Async bodies are wrapped in ``asyncio.run(...)`` inside sync test functions to match the suite's
existing style. Every test depends on ``clean_db`` (truncates all user tables around each test).

One test-environment tweak: the app engine is rebuilt per test with SQLAlchemy ``NullPool`` (still
the real asyncpg driver against real Postgres — just no cross-event-loop connection pooling), so
the repeated ``asyncio.run`` loops and the per-request TestClient loops never trip over pooled
asyncpg connections bound to a dead loop. RLS behaviour is completely unaffected.
"""

from __future__ import annotations

import asyncio
import os
import time
from uuid import UUID, uuid4

import asyncpg
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import schema
from bruce_engine.db import user_session
from bruce_engine.records import (
    ConcurrencyError,
    CrossTenantError,
    MissionRecord,
    NotFoundError,
    SourceRecord,
    TaskRecord,
)
from bruce_engine.repositories import (
    PostgresMissionRepository,
    PostgresSourceRepository,
    PostgresTaskRepository,
    PostgresUserRepository,
)

# Real Postgres repositories (the objects under test).
users_repo = PostgresUserRepository()
sources = PostgresSourceRepository()
tasks = PostgresTaskRepository()
missions = PostgresMissionRepository()

# Real FastAPI app, real (Postgres) repos wired at module import — NOT replaced with in-memory.
client = TestClient(api.app)

# The 13 tables that carry FORCE RLS per migration 0002 (users + every user-owned table).
_RLS_TABLES = [
    "users", "sources", "source_spans", "opportunities", "tasks", "calendar_proposals",
    "briefs", "missions", "mission_phase_events", "approvals", "receipts", "model_costs",
    "audit_events",
    # Added 0004. integrations holds ENCRYPTED Google refresh tokens and oauth_states holds
    # single-use PKCE verifiers — a new table that quietly missed RLS would be the worst possible
    # place for the gap, so they are covered by the same all-tables guarantee as everything else.
    "integrations", "oauth_states",
]


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    """Rebuild the app engine per test with NullPool (real asyncpg, real PG — no pooling).

    Depends on ``pg_test_db`` so the whole module skips cleanly when Postgres isn't configured.
    """

    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


# --------------------------------------------------------------------------- helpers


def _token(uid: UUID) -> str:
    """Mint an HS256 JWT with the sub claim (and aud, if BRUCE_JWT_AUDIENCE is set)."""
    payload = {"sub": str(uid), "exp": int(time.time()) + 3600}
    aud = os.environ.get("BRUCE_JWT_AUDIENCE")
    if aud:
        payload["aud"] = aud
    return jwt.encode(payload, os.environ["BRUCE_JWT_SECRET"], algorithm="HS256")


def _auth(uid: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(uid)}"}


def _app_conn_params() -> dict:
    """asyncpg connection kwargs for the restricted bruce_app role against bruce_test."""
    u = make_url(os.environ["BRUCE_APP_DATABASE_URL"])
    return dict(host=u.host, port=u.port or 5432, user=u.username, password=u.password, database=u.database)


async def _mk_mission(uid: UUID, **kw) -> MissionRecord:
    await users_repo.ensure(uid)
    return await missions.create(MissionRecord(user_id=uid, goal=kw.pop("goal", {}), **kw))


def _mk_mission_id(uid: UUID) -> str:
    return str(asyncio.run(_mk_mission(uid)).id)


# --------------------------------------------------------------------------- 1


def test_01_owner_can_read_own_records(clean_db):
    """A can read A-owned source, task, and mission records through the real repos."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        src = await sources.create(SourceRecord(user_id=a, kind="text", raw_text="hello"))
        tsk = await tasks.create(
            TaskRecord(user_id=a, kind="deadline", title="Essay due", source_id=src.id)
        )
        m = await missions.create(MissionRecord(user_id=a, goal={"topic": "polariton"}))

        gsrc = await sources.get_for_user(src.id, a)
        gtsk = await tasks.get_for_user(tsk.id, a)
        gm = await missions.get_for_user(m.id, a)
        assert gsrc is not None and gsrc.raw_text == "hello"
        assert gtsk is not None and gtsk.title == "Essay due" and gtsk.source_id == src.id
        assert gm is not None and gm.goal == {"topic": "polariton"}
        assert [t.id for t in await tasks.list_for_user(a)] == [tsk.id]

    asyncio.run(run())


# --------------------------------------------------------------------------- 2


def test_02_A_gets_404_for_B_owned_via_api(clean_db):
    """Via the API: the owner (B) reads their mission (200); a different user (A) gets 404."""
    a, b = uuid4(), uuid4()
    mid = _mk_mission_id(b)
    assert client.get(f"/v1/missions/{mid}", headers=_auth(b)).status_code == 200  # owner sees it
    assert client.get(f"/v1/missions/{mid}", headers=_auth(a)).status_code == 404  # 404, never 403


# --------------------------------------------------------------------------- 3


def test_03_B_gets_404_for_A_owned_via_api(clean_db):
    """Symmetric to #2: A owns, A reads (200), B is denied with 404."""
    a, b = uuid4(), uuid4()
    mid = _mk_mission_id(a)
    assert client.get(f"/v1/missions/{mid}", headers=_auth(a)).status_code == 200
    assert client.get(f"/v1/missions/{mid}", headers=_auth(b)).status_code == 404


# --------------------------------------------------------------------------- 4


def test_04_missing_auth_is_401(clean_db):
    """No bearer token -> 401 on both the detail and the list endpoints."""
    mid = _mk_mission_id(uuid4())
    assert client.get(f"/v1/missions/{mid}").status_code == 401
    assert client.get("/v1/missions").status_code == 401


# --------------------------------------------------------------------------- 5


def test_05_cross_user_read_insert_update_delete_fk_all_fail(clean_db):
    """Cross-tenant read/update/delete are blocked and the FK cross-reference raises CrossTenantError."""

    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        src_a = await sources.create(SourceRecord(user_id=a, kind="text", raw_text="a-secret"))
        m_a = await missions.create(MissionRecord(user_id=a, goal={"k": "v"}))

        # cross-READ: B cannot see A's mission (RLS USING + repo filter)
        assert await missions.get_for_user(m_a.id, b) is None
        assert m_a.id not in [x.id for x in await missions.list_for_user(b)]

        # cross-INSERT / cross-FK-reference: B attaching A's source raises CrossTenantError
        with pytest.raises(CrossTenantError):
            await tasks.create(TaskRecord(user_id=b, kind="deadline", title="x", source_id=src_a.id))

        # cross-UPDATE: B's UPDATE of A's mission touches 0 rows (row invisible under B's context)
        async with user_session(b) as s:
            res = await s.execute(
                update(schema.Mission).where(schema.Mission.id == m_a.id).values(short_status="hacked")
            )
            assert res.rowcount == 0

        # cross-DELETE: B's DELETE of A's mission touches 0 rows
        async with user_session(b) as s:
            res = await s.execute(delete(schema.Mission).where(schema.Mission.id == m_a.id))
            assert res.rowcount == 0

        # A's row survived every attempt, untouched
        still = await missions.get_for_user(m_a.id, a)
        assert still is not None and still.short_status != "hacked"

    asyncio.run(run())


# --------------------------------------------------------------------------- 6


def test_06_cannot_alter_user_id_on_update(clean_db):
    """A cannot re-home its own row to B: WITH CHECK rejects the write; the user_id is unchanged."""

    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        m = await missions.create(MissionRecord(user_id=a, goal={}))

        with pytest.raises(Exception):  # new-row RLS WITH CHECK violation
            async with user_session(a) as s:
                await s.execute(
                    update(schema.Mission).where(schema.Mission.id == m.id).values(user_id=b)
                )

        row = await missions.get_for_user(m.id, a)
        assert row is not None and row.user_id == a  # ownership unchanged (attempt was a hard fail)
        assert await missions.get_for_user(m.id, b) is None  # and it never became B's

    asyncio.run(run())


# --------------------------------------------------------------------------- 7


def test_07_using_and_with_check_both_enforce_ownership(clean_db):
    """USING blocks the cross-tenant READ; WITH CHECK blocks the insert-as-someone-else WRITE."""

    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        m_a = await missions.create(MissionRecord(user_id=a, goal={}))

        # USING: B cannot read A's row
        assert await missions.get_for_user(m_a.id, b) is None
        async with user_session(b) as s:
            rows = (
                await s.execute(schema.Mission.__table__.select().where(schema.Mission.id == m_a.id))
            ).all()
            assert rows == []

        # WITH CHECK: B cannot insert a row owned by A
        with pytest.raises(Exception):
            async with user_session(b) as s:
                s.add(
                    schema.Mission(
                        user_id=a, kind="outreach", status="running", phase="created",
                        short_status="x", goal={},
                    )
                )
                await s.flush()

        # nothing landed for A beyond the original row
        assert len(await missions.list_for_user(a)) == 1

    asyncio.run(run())


# --------------------------------------------------------------------------- 8


def test_08_force_rls_enabled_on_all_user_tables(clean_db):
    """After migrations, every user table has ROW SECURITY both ENABLED and FORCED."""

    async def run():
        conn = await asyncpg.connect(**_app_conn_params())
        try:
            rows = await conn.fetch(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relname = ANY($1::text[])",
                _RLS_TABLES,
            )
            got = {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rows}
            for t in _RLS_TABLES:
                assert t in got, f"table {t} not found"
                assert got[t] == (True, True), f"{t} rowsecurity/force = {got[t]} (want (True, True))"
        finally:
            await conn.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- 9


def test_09_app_role_is_least_privilege(clean_db):
    """bruce_app: no BYPASSRLS, not superuser, owns no table, and cannot CREATE in schema public."""

    async def run():
        conn = await asyncpg.connect(**_app_conn_params())
        try:
            role = await conn.fetchrow(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                "FROM pg_roles WHERE rolname = 'bruce_app'"
            )
            assert role is not None
            assert role["rolbypassrls"] is False, "bruce_app must NOT have BYPASSRLS"
            assert role["rolsuper"] is False, "bruce_app must NOT be superuser"

            owned = await conn.fetchval(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = 'bruce_app'"
            )
            assert owned == 0, "bruce_app must own no tables"

            assert await conn.fetchval("SELECT has_schema_privilege('bruce_app','public','CREATE')") is False
            # sanity: it does have the USAGE it was granted (proves the check discriminates)
            assert await conn.fetchval("SELECT has_schema_privilege('bruce_app','public','USAGE')") is True
        finally:
            await conn.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- 10


def test_10_connection_reuse_clears_user_context(clean_db):
    """One reused bruce_app connection: A-txn sees A; no-context sees 0; B-txn sees only B."""

    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        await missions.create(MissionRecord(user_id=a, goal={}))
        await missions.create(MissionRecord(user_id=b, goal={}))

        conn = await asyncpg.connect(**_app_conn_params())
        try:
            # A transaction: transaction-local app.user_id -> sees exactly A's 1 row
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.user_id', $1, true)", str(a))
                assert await conn.fetchval("SELECT count(*) FROM missions") == 1
                owner = await conn.fetchval("SELECT user_id FROM missions")
                assert str(owner) == str(a)

            # same connection, no context now (setting was transaction-local) -> 0 rows
            assert await conn.fetchval("SELECT count(*) FROM missions") == 0

            # B transaction on the SAME connection: sees only B's row
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.user_id', $1, true)", str(b))
                rows = await conn.fetch("SELECT user_id FROM missions")
                assert len(rows) == 1 and str(rows[0]["user_id"]) == str(b)
        finally:
            await conn.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- 11


def test_11_missing_or_malformed_user_id_fails_closed(clean_db):
    """Unset or non-UUID app.user_id -> 0 rows and NO exception (app_current_user() swallows it)."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        await missions.create(MissionRecord(user_id=a, goal={}))

        conn = await asyncpg.connect(**_app_conn_params())
        try:
            # missing context: fails closed to 0 rows, no error
            assert await conn.fetchval("SELECT count(*) FROM missions") == 0

            # malformed context: still 0 rows, still no error (EXCEPTION -> NULL in the SQL fn)
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.user_id', 'not-a-uuid', true)")
                assert await conn.fetchval("SELECT count(*) FROM missions") == 0

            # positive control: a valid context does see the row (proves the table isn't just empty)
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.user_id', $1, true)", str(a))
                assert await conn.fetchval("SELECT count(*) FROM missions") == 1
        finally:
            await conn.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- 12


def test_12_worker_repos_require_explicit_user_context(clean_db):
    """Background-worker repo calls with a mismatched user get nothing (None / NotFoundError)."""

    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        m = await missions.create(MissionRecord(user_id=a, goal={}))

        # right user -> visible; wrong user -> invisible
        assert await missions.get_for_user(m.id, a) is not None
        assert await missions.get_for_user(m.id, b) is None

        # a transition attempted under the wrong user context resolves to not-found, never a leak
        with pytest.raises(NotFoundError):
            await missions.update_phase(m.id, b, expected_version=1, phase="executing", short_status="x")

    asyncio.run(run())


# --------------------------------------------------------------------------- 13


def test_13_duplicate_ingestion_is_idempotent(clean_db):
    """Same idempotency_key -> the same logical mission (one row, same id)."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        m1 = await missions.create(MissionRecord(user_id=a, goal={"n": 1}, idempotency_key="dup-key"))
        m2 = await missions.create(MissionRecord(user_id=a, goal={"n": 2}, idempotency_key="dup-key"))
        assert m1.id == m2.id
        assert len(await missions.list_for_user(a)) == 1

    asyncio.run(run())


# --------------------------------------------------------------------------- 14


def test_14_concurrent_transitions_exactly_one_wins(clean_db):
    """Two concurrent update_phase(expected_version=1): exactly one succeeds, one ConcurrencyError."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        m = await missions.create(MissionRecord(user_id=a, goal={}))

        results = await asyncio.gather(
            missions.update_phase(m.id, a, expected_version=1, phase="executing", short_status="e1"),
            missions.update_phase(m.id, a, expected_version=1, phase="verifying", short_status="e2"),
            return_exceptions=True,
        )
        oks = [r for r in results if isinstance(r, MissionRecord)]
        errs = [r for r in results if isinstance(r, ConcurrencyError)]
        assert len(oks) == 1, f"expected exactly one winner, got {results}"
        assert len(errs) == 1, f"expected exactly one ConcurrencyError, got {results}"
        assert oks[0].version == 2

        final = await missions.get_for_user(m.id, a)
        assert final is not None and final.version == 2  # single committed transition

    asyncio.run(run())


# --------------------------------------------------------------------------- 15


def test_15_data_survives_engine_restart(clean_db):
    """Create, dispose+reset the engine (simulated process restart), recreate, read the row back."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        m = await missions.create(MissionRecord(user_id=a, goal={"persisted": True}))

        # simulate a restart: tear the engine down and clear the cached handles
        await db._engine.dispose()
        db._engine = None
        db._sessionmaker = None

        # next repo call rebuilds the engine from scratch and still finds the committed row
        again = await missions.get_for_user(m.id, a)
        assert again is not None and again.id == m.id and again.goal == {"persisted": True}

    asyncio.run(run())


# --------------------------------------------------------------------------- 16


def test_16_account_deletion_removes_all_owned_records(clean_db):
    """Deleting the user cascades: every source/task/mission they owned is gone."""

    async def run():
        a = uuid4()
        await users_repo.ensure(a)
        src = await sources.create(SourceRecord(user_id=a, kind="text", raw_text="x"))
        tsk = await tasks.create(TaskRecord(user_id=a, kind="deadline", title="t", source_id=src.id))
        m = await missions.create(MissionRecord(user_id=a, goal={}))

        assert await sources.get_for_user(src.id, a) is not None
        assert await tasks.get_for_user(tsk.id, a) is not None
        assert await missions.get_for_user(m.id, a) is not None

        await users_repo.delete(a)  # account deletion path (FK ON DELETE CASCADE)

        assert await sources.get_for_user(src.id, a) is None
        assert await tasks.get_for_user(tsk.id, a) is None
        assert await missions.get_for_user(m.id, a) is None

    asyncio.run(run())


# --------------------------------------------------------------------------- 17


def test_17_wrong_owner_and_missing_are_indistinguishable(clean_db):
    """A nonexistent id and another user's real id return an IDENTICAL 404 (no existence leak)."""
    a, b = uuid4(), uuid4()
    mid = _mk_mission_id(b)  # a real mission that belongs to B

    r_missing = client.get(f"/v1/missions/{uuid4()}", headers=_auth(a))  # id that exists nowhere
    r_other = client.get(f"/v1/missions/{mid}", headers=_auth(a))  # id that exists but is B's

    assert r_missing.status_code == 404 and r_other.status_code == 404
    assert r_missing.json() == r_other.json()  # byte-identical body -> cannot tell which case it was
