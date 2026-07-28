"""Two Postgres-backed test sessions at once must not destroy each other.

THE INCIDENT. The test database used to be one shared name, created with `DROP DATABASE ... WITH (FORCE)`
at session start. During #121 two lanes ran their suites concurrently and the second run dropped the
first run's database mid-transaction: 268 failures and 275 errors, none about the code, twice before the
pattern was recognisable. Nothing in the suite could have caught it, because the suite was the thing
being destroyed.

So this file runs the real thing: two genuine pytest sessions, concurrently, against real Postgres, each
doing real writes and real cleanup. It is slower than a unit test and it is the only shape of test that
would have failed before the fix and passes after it.

Deliberately not solved by serialising the suite. Serialising makes the symptom go away and leaves the
isolation broken for anyone who runs a file locally while CI is running, which is most of the time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests import conftest as ct

ENGINE = Path(__file__).resolve().parents[1]

# A tiny module the concurrent sessions actually execute. Written to disk by the test rather than kept as
# a collected file, so the outer suite does not run it as well and the two sessions are provably doing
# the same work at the same time.
_PROBE = '''
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import world_state
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


@pytest.mark.parametrize("i", range(6))
def test_writes_and_reads_its_own_database(clean_db, i):
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(world_state.set_timezone(uid, "America/Chicago", source="user_stated"))
    assert _run(world_state.resolve_timezone(uid, default="UTC")) == "America/Chicago"
'''


def _session(tag: str, probe: Path) -> subprocess.CompletedProcess:
    """One full pytest session with its own worker identity — which is what picks the database name."""
    env = {**os.environ, "PYTEST_XDIST_WORKER": tag}
    # The parent session already rewrote these to point at ITS database. A child must start from the
    # real cluster URLs, or both children would inherit one name and the test would prove nothing.
    for key in ("BRUCE_DATABASE_URL", "BRUCE_APP_DATABASE_URL"):
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:randomly", "--no-header"],
        cwd=str(ENGINE), env=env, capture_output=True, text=True, timeout=600)


@pytest.mark.slow
def test_two_concurrent_sessions_do_not_interfere(pg_test_db, tmp_path):
    probe = ENGINE / "tests" / "_isolation_probe.py"
    probe.write_text(_PROBE)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(_session, "isoA", probe)
            b = pool.submit(_session, "isoB", probe)
            ra, rb = a.result(), b.result()
    finally:
        probe.unlink(missing_ok=True)

    for tag, r in (("A", ra), ("B", rb)):
        assert r.returncode == 0, f"session {tag} failed:\n{r.stdout[-4000:]}\n{r.stderr[-2000:]}"
        # Skipping would make this vacuously green — the exact failure mode a "did both pass?" assertion
        # invites, since a session with no database configured skips every test and exits 0.
        assert " skipped" not in r.stdout.splitlines()[-1], f"session {tag} skipped instead of running"
        assert "6 passed" in r.stdout, f"session {tag} did not run all 6:\n{r.stdout[-2000:]}"


def test_the_database_name_is_session_scoped_not_a_shared_constant():
    """The property the incident turned on. A constant name means `DROP DATABASE ... WITH (FORCE)` in one
    process terminates another process's connections, and FORCE is what makes that silent."""
    assert ct.TEST_DB != "bruce_test"
    assert ct.TEST_DB.startswith("bruce_test_")
    assert ct._WORKER, "no worker identity — the name would collapse back to a constant"
    assert len(ct.TEST_DB) <= 63, "Postgres truncates identifiers past 63 bytes, which reintroduces collisions"


def test_cleanup_covers_every_table_not_a_hand_maintained_list(pg_test_db):
    """The old list was written against migration 0017 and never grew. Everything added since — agent
    runs, world state, calendar entities, the gmail ledger, authorization, memory — was truncated only as
    a side effect of `users ... CASCADE`, and would have started leaking the day any of them lost that
    foreign key."""
    import asyncio

    async def _names():
        conn = await ct._connect_test_db()
        try:
            return set(await ct._user_tables(conn))
        finally:
            await conn.close()

    tables = asyncio.run(_names())
    for expected in ("users", "agent_runs", "user_world_state", "gmail_sent_ledger",
                     "authorization_evidence", "authorization_refusals", "memory_records"):
        assert expected in tables, f"{expected} is not covered by cleanup"
    assert "alembic_version" not in tables, "truncating the migration bookkeeping would reset the chain"


def test_cleanup_failures_are_raised_rather_than_retried():
    """A retry loop around cleanup hides the one condition worth knowing about — a test that leaked a
    connection — and turns it into an intermittent failure somewhere else entirely."""
    import ast
    import inspect
    tree = ast.parse(Path(ct.__file__).read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "clean_db")
    fn.body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    code = ast.unparse(fn)          # the CODE, with the docstring removed — prose about retries is not a retry
    assert "lock_timeout" in inspect.getsource(ct.clean_db), "cleanup can block indefinitely"
    assert "except" not in code, "cleanup swallows failures"
    assert "while" not in code and "for _ in range" not in code, "cleanup retries instead of failing"
