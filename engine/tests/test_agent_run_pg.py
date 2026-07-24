"""AgentRun persistence (R2) against real Postgres — create/resume/update round-trip, idempotency, RLS."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_run_store
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def test_create_resume_update(clean_db):
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    run = _run(agent_run_store.create_run(uid, goal={"action": "create", "domain": "calendar"}))
    assert run["status"] == "understanding"
    from uuid import UUID
    _run(agent_run_store.update_run(uid, UUID(run["id"]), status="executing", selected_provider_account="a@b.com"))
    latest = _run(agent_run_store.latest_active(uid))
    assert latest["id"] == run["id"] and latest["status"] == "executing" and latest["selected_provider_account"] == "a@b.com"
    _run(agent_run_store.update_run(uid, UUID(run["id"]), status="completed"))
    assert _run(agent_run_store.latest_active(uid)) is None                  # terminal -> not active


def test_idempotent_create(clean_db):
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    a = _run(agent_run_store.create_run(uid, idempotency_key="k1"))
    b = _run(agent_run_store.create_run(uid, idempotency_key="k1"))
    assert a["id"] == b["id"]                                                # same run, no duplicate


def test_owner_isolated(clean_db):
    a, b = uuid4(), uuid4()
    _run(users.ensure(a, auth_provider="test")); _run(users.ensure(b, auth_provider="test"))
    run = _run(agent_run_store.create_run(a))
    from uuid import UUID
    assert _run(agent_run_store.get_run(b, UUID(run["id"]))) is None         # b cannot read a's run
