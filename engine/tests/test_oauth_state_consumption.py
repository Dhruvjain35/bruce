"""Real-Postgres proof of the OAuth-callback fix — the state is consumable PRE-IDENTITY via a worker
session, exactly once. Reproduces + guards the live bug where a committed, valid, unconsumed state could
not be seen by _consume_state (owner-conn was RLS-blocked). NOT mocked. Skips without Postgres.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import oauth_google, schema
from bruce_engine.db import user_session


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://x/v1/integrations/google/callback")
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _mint(uid):
    async with user_session(uid) as s:
        s.add(schema.User(id=uid, auth_provider="test"))
    url = await oauth_google.start_authorization(uid)   # writes oauth_states under the USER session (committed)
    return parse_qs(urlparse(url).query)["state"][0]


def test_state_committed_readable_then_consumed_exactly_once_via_worker(clean_db):
    uid = uuid4()
    state = _run(_mint(uid))

    async def readback():
        async with user_session(uid) as s:               # a SEPARATE connection sees it committed
            return (await s.execute(sa_text(
                "SELECT consumed_at, (expires_at > now()) AS valid FROM oauth_states WHERE user_id=:u"),
                {"u": str(uid)})).mappings().first()
    r = _run(readback())
    assert r is not None and r["valid"] and r["consumed_at"] is None      # committed, valid, unconsumed

    claimed = _run(oauth_google._consume_state(state))                    # THE callback path (worker) — failed live
    assert str(claimed["user_id"]) == str(uid) and claimed["provider"] == oauth_google.PROVIDER
    with pytest.raises(oauth_google.InvalidState):                        # single-use: a replay fails
        _run(oauth_google._consume_state(state))


def test_a_read_does_not_consume_the_active_state(clean_db):
    uid = uuid4()
    state = _run(_mint(uid))
    _run(oauth_google.get_integration(uid))                              # a status/read must not consume it

    async def unconsumed():
        async with user_session(uid) as s:
            return (await s.execute(sa_text(
                "SELECT consumed_at FROM oauth_states WHERE user_id=:u"), {"u": str(uid)})).scalar_one()
    assert _run(unconsumed()) is None
    assert str(_run(oauth_google._consume_state(state))["user_id"]) == str(uid)   # still consumable after a read


def test_rls_still_isolates_users_only_worker_crosses(clean_db):
    uid, other = uuid4(), uuid4()
    state = _run(_mint(uid))

    async def other_count():
        async with user_session(other) as s:
            return (await s.execute(select(func.count()).select_from(schema.OAuthState).where(
                schema.OAuthState.state == state))).scalar_one()
    assert _run(other_count()) == 0                                      # a different user still cannot see it


def test_expired_state_is_invalid(clean_db):
    uid = uuid4()
    state = _run(_mint(uid))
    async def expire():
        async with user_session(uid) as s:
            await s.execute(sa_text("UPDATE oauth_states SET expires_at = now() - interval '1 minute' WHERE user_id=:u"), {"u": str(uid)})
    _run(expire())
    with pytest.raises(oauth_google.InvalidState):
        _run(oauth_google._consume_state(state))
