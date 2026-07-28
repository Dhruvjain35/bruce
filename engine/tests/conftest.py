"""Pytest fixtures for REAL Postgres integration tests.

Provisions a disposable database PER TEST SESSION, runs the real Alembic migrations against it as the
OWNER, then points the app (bruce_engine.db) at it through the restricted `bruce_app` role — so these
tests exercise the actual RLS policies, never SQLite or mocks. Offline tests don't touch these fixtures
(they're not autouse), so the fast suite still runs without Postgres.

WHY THE DATABASE NAME IS NOT A CONSTANT. It used to be `bruce_test`, one name shared by every process on
the machine, created with `DROP DATABASE ... WITH (FORCE)` at session start. Two pytest runs at once —
two worktrees during a parallel lane, an xdist worker pool, a developer running one file while CI runs
the suite — meant the second run dropped the first run's database out from under it mid-transaction.
Measured during #121: 268 failures and 275 errors, none of them about the code, twice in a row before
the pattern was recognisable. A test suite that fails for reasons unrelated to the code under test is
worse than a slow one, because it teaches you to disbelieve red.

So the name is derived per session: the xdist worker id when running under xdist, otherwise the process
id. Two concurrent runs cannot collide, and `WITH (FORCE)` can no longer terminate anyone else's
connections. `test_db_isolation.py` runs two real concurrent sessions and proves it.

Requires BRUCE_DATABASE_URL (owner) + BRUCE_APP_DATABASE_URL (bruce_app) in engine/.env. If Postgres
isn't reachable the PG tests skip (so the offline suite is unaffected).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest
from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ENGINE = Path(__file__).resolve().parents[1]
load_dotenv(ENGINE / ".env")

# Link codes are HMAC-peppered; tests need a (non-secret) pepper if .env didn't provide one.
os.environ.setdefault("BRUCE_LINK_CODE_PEPPER", "test-link-code-pepper-not-a-real-secret")

# Postgres identifiers cap at 63 bytes, so the token stays short.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER") or f"p{os.getpid()}"
TEST_DB = f"bruce_test_{_WORKER}"

# How long cleanup will wait for a lock before giving up. The old `TRUNCATE users ... CASCADE` took an
# ACCESS EXCLUSIVE lock on every child table and could block behind a connection the previous test had
# not finished with — observed as an intermittent hang, not an error. A bounded wait turns that into a
# visible failure with a name, which is the whole point.
_CLEANUP_LOCK_TIMEOUT_MS = 5_000


def _swap_db(url: str, dbname: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + dbname


async def _admin(sql: str, *, database: str = "postgres") -> None:
    owner = make_url(os.environ["BRUCE_DATABASE_URL"])
    conn = await asyncpg.connect(
        host=owner.host, port=owner.port or 5432, user=owner.username,
        password=owner.password, database=database,
    )
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _connect_test_db():
    owner = make_url(os.environ["BRUCE_DATABASE_URL"])
    return await asyncpg.connect(
        host=owner.host, port=owner.port or 5432, user=owner.username,
        password=owner.password, database=TEST_DB,
    )


@pytest.fixture(scope="session")
def pg_test_db():
    owner_url = os.environ.get("BRUCE_DATABASE_URL")
    app_url = os.environ.get("BRUCE_APP_DATABASE_URL")
    if not owner_url or not app_url:
        pytest.skip("Postgres env (BRUCE_DATABASE_URL/BRUCE_APP_DATABASE_URL) not configured")
    try:
        asyncio.run(_admin("SELECT 1"))
    except Exception:
        pytest.skip("Postgres not reachable")

    # ensure the restricted app role exists (cluster-level), then a fresh database FOR THIS SESSION ONLY
    asyncio.run(
        _admin(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bruce_app') "
            "THEN CREATE ROLE bruce_app LOGIN PASSWORD 'bruce_dev_pw'; END IF; END $$;"
        )
    )
    # FORCE is safe now precisely BECAUSE the name is session-scoped: the only connections it can
    # terminate are this session's own leftovers from a previous crashed run of the same worker.
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
    asyncio.run(_admin(f'CREATE DATABASE "{TEST_DB}"'))

    test_owner = _swap_db(owner_url, TEST_DB)
    test_app = _swap_db(app_url, TEST_DB)

    # migrate the test db AS OWNER (privileged); assertions later run as bruce_app
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ENGINE / "alembic.ini"), "upgrade", "head"],
        cwd=str(ENGINE),
        env={**os.environ, "BRUCE_DATABASE_URL": test_owner},
        check=True,
        capture_output=True,
    )

    # point the app at the test db (as bruce_app) and reset the cached engine
    os.environ["BRUCE_DATABASE_URL"] = test_owner
    os.environ["BRUCE_APP_DATABASE_URL"] = test_app
    import bruce_engine.db as db

    db._engine = None
    db._sessionmaker = None

    yield test_app

    async def _dispose():
        if db._engine is not None:
            await db._engine.dispose()

    asyncio.run(_dispose())
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))


async def _user_tables(conn) -> list[str]:
    """Every table in the public schema except Alembic's own bookkeeping.

    Read from the catalog rather than a hand-maintained list. The old list was written against migration
    0017 and never grew: `agent_runs`, `user_world_state`, `calendar_event_entities`, `gmail_sent_ledger`
    and everything since were simply never truncated, and only survived because `TRUNCATE users CASCADE`
    happened to reach them through a foreign key. A table that ever loses that FK would start leaking
    between tests silently, which is the kind of bug that gets blamed on the code for a week.
    """
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'")
    return [r["tablename"] for r in rows]


@pytest.fixture()
def clean_db(pg_test_db):
    """Truncate every table around each PG test so data cannot leak between tests.

    One statement, so Postgres takes all the locks at once and either succeeds or deadlocks immediately
    rather than half-way through. `lock_timeout` bounds the wait: if a connection from the previous test
    is still holding a row lock, this raises with a name instead of hanging, and the failure points at
    the test that leaked the connection rather than the one that happened to run next.

    Cleanup failures are NOT swallowed and NOT retried. A retry loop here would hide exactly the
    condition worth knowing about.
    """
    async def _truncate():
        conn = await _connect_test_db()
        try:
            await conn.execute(f"SET lock_timeout = {_CLEANUP_LOCK_TIMEOUT_MS}")
            tables = await _user_tables(conn)
            if not tables:
                return
            quoted = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
        finally:
            await conn.close()

    asyncio.run(_truncate())
    yield pg_test_db
    asyncio.run(_truncate())
