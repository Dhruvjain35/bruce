"""Background runner + AgentRun lease (G0.5) against real Postgres. The load-bearing guarantees: a run is
claimed by AT MOST ONE worker at a time (SKIP LOCKED), a crashed run's expired lease is RECLAIMED
(resumable), a worker claims across ALL users (tenant_or_worker RLS), a future/rescheduled run isn't
claimed early, and the runner never leaves a run stuck or retries past its budget."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_run_store, background_runner
from bruce_engine.background_runner import AdvanceOutcome, BackgroundRunner
from bruce_engine.db import worker_session
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


async def _row(run_id) -> dict | None:
    async with worker_session() as s:
        r = (await s.execute(sa_text(
            "SELECT status, lease_owner, attempt_count, max_attempts, next_run_at, lease_expires_at "
            "FROM agent_runs WHERE id = :id"), {"id": str(run_id)})).mappings().first()
    return dict(r) if r else None


async def _expire_lease(run_id):
    async with worker_session() as s:
        await s.execute(sa_text("UPDATE agent_runs SET lease_expires_at = now() - interval '1 minute' "
                                "WHERE id = :id"), {"id": str(run_id)})


def _user():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    return uid


def test_enqueue_then_claim_leases_the_run():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid, domain="mission", goal={"desired_outcome": "watch canvas"}))
    assert _run(_row(run["id"]))["status"] == "queued"
    claimed = _run(agent_run_store.claim_background("w1", lease_seconds=60))
    assert claimed is not None and claimed["id"] == run["id"] and claimed["user_id"] == str(uid)
    row = _run(_row(run["id"]))
    assert row["status"] == "running" and row["lease_owner"] == "w1" and row["attempt_count"] == 1


def test_claim_is_exclusive_while_leased():
    uid = _user()
    _run(agent_run_store.enqueue_background(uid))
    first = _run(agent_run_store.claim_background("w1"))
    second = _run(agent_run_store.claim_background("w2"))
    assert first is not None and second is None            # leased -> not claimable by another worker


def test_expired_lease_is_reclaimed_after_crash():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))
    _run(agent_run_store.claim_background("w1"))            # claimed, then the worker "crashes"
    _run(_expire_lease(run["id"]))
    reclaimed = _run(agent_run_store.claim_background("w2"))
    assert reclaimed is not None and reclaimed["id"] == run["id"]
    assert _run(_row(run["id"]))["attempt_count"] == 2      # second attempt, resumed by another worker


def test_future_run_is_not_claimed_early():
    uid = _user()
    _run(agent_run_store.enqueue_background(uid, next_run_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    assert _run(agent_run_store.claim_background("w1")) is None


def test_worker_claims_across_users():
    a, b = _user(), _user()
    _run(agent_run_store.enqueue_background(a))
    _run(agent_run_store.enqueue_background(b))
    c1 = _run(agent_run_store.claim_background("w1"))
    c2 = _run(agent_run_store.claim_background("w1"))
    assert {c1["user_id"], c2["user_id"]} == {str(a), str(b)}   # one worker drains BOTH users' missions


def test_renew_extends_only_for_the_owner():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))
    _run(agent_run_store.claim_background("w1"))
    assert _run(agent_run_store.renew_background_lease("w1", UUID(run["id"]))) is True
    assert _run(agent_run_store.renew_background_lease("w2", UUID(run["id"]))) is False   # not the owner


def test_runner_completes_a_queued_run_then_idles():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))
    r = BackgroundRunner(worker_id="w1")                  # default NoopAdvancer
    assert _run(r.run_once()) is True
    assert _run(_row(run["id"]))["status"] == "completed"
    assert _run(r.run_once()) is False                    # nothing left to do


def test_runner_reschedules_when_not_done():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))

    class Later:
        async def advance(self, run):
            return AdvanceOutcome(done=False, retry_after_seconds=3600)

    r = BackgroundRunner(worker_id="w1", advancer=Later())
    assert _run(r.run_once()) is True
    row = _run(_row(run["id"]))
    assert row["status"] == "queued" and row["next_run_at"] is not None
    assert _run(agent_run_store.claim_background("w2")) is None   # rescheduled into the future -> not due


def test_advancer_exception_does_not_strand_the_run():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))

    class Boom:
        async def advance(self, run):
            raise RuntimeError("mission step blew up")

    r = BackgroundRunner(worker_id="w1", advancer=Boom())
    assert _run(r.run_once()) is True
    assert _run(_row(run["id"]))["status"] == "queued"    # rescheduled for a later attempt, not stuck 'running'


def test_healthy_monitor_is_not_force_failed_by_the_failure_budget():
    """Finding 2 regression: a recurring monitor (advance returns done=False) must run indefinitely — a
    healthy reschedule resets the failure budget, so it is NEVER killed as 'max_attempts_exceeded'."""
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))

    class Monitor:
        async def advance(self, run):
            return AdvanceOutcome(done=False, retry_after_seconds=300)

    r = BackgroundRunner(worker_id="w1", advancer=Monitor())
    for _ in range(8):                                         # well past max_attempts (5)
        assert _run(r.run_once()) is True
        # simulate the schedule coming due again
        async def _due():
            async with worker_session() as s:
                await s.execute(sa_text("UPDATE agent_runs SET next_run_at = now() - interval '1 second' "
                                        "WHERE id = :id"), {"id": run["id"]})
        _run(_due())
    row = _run(_row(run["id"]))
    assert row["status"] == "queued"                          # still monitoring, not failed
    assert row["attempt_count"] <= 1                          # budget reset each healthy cycle


def test_lost_lease_write_is_fenced_out():
    """Finding 1 regression: a worker that lost its lease (reclaimed by another) must NOT clobber the run."""
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))
    _run(agent_run_store.claim_background("w1"))               # w1 holds the lease
    # simulate w1 losing the lease: it expires and another worker reclaims
    _run(_expire_lease(run["id"]))
    _run(agent_run_store.claim_background("w2"))               # w2 now owns it (status running, lease=w2)
    # stale w1 tries to complete -> fenced (it no longer owns the lease)
    _run(agent_run_store.complete_background(uid, UUID(run["id"]), status="completed", worker_id="w1"))
    row = _run(_row(run["id"]))
    assert row["status"] == "running" and row["lease_owner"] == "w2"   # w1's write did nothing


def test_runner_gives_up_after_max_attempts():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid))

    async def _cap():
        async with worker_session() as s:
            await s.execute(sa_text("UPDATE agent_runs SET max_attempts = 1 WHERE id = :id"),
                            {"id": run["id"]})
    _run(_cap())

    class Boom:
        async def advance(self, run):
            raise RuntimeError("keeps failing")

    r = BackgroundRunner(worker_id="w1", advancer=Boom())
    assert _run(r.run_once()) is True
    row = _run(_row(run["id"]))
    assert row["status"] == "dead_letter"                 # attempt_count(1) >= max_attempts(1) -> dead-letter (Phase E)
