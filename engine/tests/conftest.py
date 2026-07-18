"""Pytest fixtures for REAL Postgres integration tests.

Provisions a disposable `bruce_test` database, runs the real Alembic migrations against it as the
OWNER, then points the app (bruce_engine.db) at it through the restricted `bruce_app` role — so
these tests exercise the actual RLS policies, never SQLite or mocks. Offline tests don't touch
these fixtures (they're not autouse), so the fast suite still runs without Postgres.

Requires BRUCE_DATABASE_URL (owner) + BRUCE_APP_DATABASE_URL (bruce_app) in engine/.env. If
Postgres isn't reachable the PG tests skip (so the offline suite is unaffected).
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

TEST_DB = "bruce_test"
_USER_TABLES = (
    "users sources source_spans opportunities tasks calendar_proposals briefs missions "
    "mission_phase_events approvals receipts audit_events model_costs "
    "integrations oauth_states "  # added 0004 — must be truncated too, or OAuth state leaks between tests
    "intake_jobs "  # added 0005 — async intake queue; truncate so jobs don't leak between tests
    "messaging_identities messaging_conversations inbound_messages message_attachments "  # added 0006
    "outbound_messages message_delivery_events account_link_codes "
    "relay_devices delivery_attempts"  # added 0007 — self-hosted iMessage relay
).split()


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

    # ensure the restricted app role exists (cluster-level), then a fresh test database
    asyncio.run(
        _admin(
            "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='bruce_app') "
            "THEN CREATE ROLE bruce_app LOGIN PASSWORD 'bruce_dev_pw'; END IF; END $$;"
        )
    )
    asyncio.run(_admin(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    asyncio.run(_admin(f"CREATE DATABASE {TEST_DB}"))

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
    asyncio.run(_admin(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))


@pytest.fixture()
def clean_db(pg_test_db):
    """Truncate all user tables around each PG test so data cannot leak between tests."""
    async def _truncate():
        owner = make_url(os.environ["BRUCE_DATABASE_URL"])
        conn = await asyncpg.connect(
            host=owner.host, port=owner.port or 5432, user=owner.username,
            password=owner.password, database=TEST_DB,
        )
        try:
            await conn.execute(f"TRUNCATE {', '.join(_USER_TABLES)} RESTART IDENTITY CASCADE")
        finally:
            await conn.close()

    asyncio.run(_truncate())
    yield pg_test_db
    asyncio.run(_truncate())
