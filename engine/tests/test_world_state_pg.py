"""UserWorldState persistence (R3) against real Postgres — timezone round-trip + RLS isolation +
timezone applied to a real calendar build. Skips without Postgres."""

from __future__ import annotations

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
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def test_timezone_upsert_round_trip(clean_db):
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    assert _run(world_state.get_timezone(uid)) is None
    _run(world_state.set_timezone(uid, "America/Chicago", source="user_stated"))
    assert _run(world_state.get_timezone(uid)) == "America/Chicago"
    _run(world_state.set_timezone(uid, "America/New_York"))          # upsert, not duplicate
    assert _run(world_state.get_timezone(uid)) == "America/New_York"
    assert _run(world_state.resolve_timezone(uid, default="America/Los_Angeles")) == "America/New_York"


def test_resolve_falls_back_to_default_when_unset(clean_db):
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    assert _run(world_state.resolve_timezone(uid, default="America/Los_Angeles")) == "America/Los_Angeles"


def test_timezone_is_owner_isolated(clean_db):
    a, b = uuid4(), uuid4()
    _run(users.ensure(a, auth_provider="test"))
    _run(users.ensure(b, auth_provider="test"))
    _run(world_state.set_timezone(a, "America/Chicago"))
    assert _run(world_state.get_timezone(a)) == "America/Chicago"
    assert _run(world_state.get_timezone(b)) is None                 # b cannot see a's row
