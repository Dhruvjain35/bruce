"""EVERY RETRY PATH IS BOUNDED — including the one no code was written for.

`test_shadow_retry_durability` covers the retry the worker DECIDES on: a transport failure is labelled
retryable, backed off, and reclaimed. That path was always bounded, because `attempts` advanced when the
worker recorded a result and `can_retry` compared it against `max_attempts`.

The unbounded path is the one where the worker never gets to decide. A container eviction, an OOM kill,
a crash inside the read: the job was CLAIMED and no result was ever recorded. The lease expires, the next
worker claims it, and — while `attempts` advanced only on a successful record — starts from zero. A turn
that reliably kills its reader is claimed forever, burns a worker slot on every wake, and sits in the
backlog for the life of the table, which makes the drain look permanently behind for a reason no metric
names.

So `attempts` advances ON CLAIM, and a job that runs out of attempts becomes a TYPED terminal outcome
(`exhausted_infrastructure_retries`) rather than a silence. This file asserts the whole bound:

  * the counter moves for a worker that recorded NOTHING
  * an exhausted job is never claimed again
  * it is never `completed`, and it never became one through a retry
  * it is visible in the health numbers as its own bucket
  * terminalizing it is IDEMPOTENT — a second sweep changes nothing
  * the sweep performs no read: no model is called and no goal, decision or provider call appears

The crash is modelled by claiming and walking away, which is exactly what a killed worker leaves behind.
Nothing here writes `attempts` by hand: a test that sets the counter it is asserting on proves only that
the test can write a number.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import semantic_shadow
from bruce_engine.db import worker_session
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()
CHANNEL, PMID = "imessage", "pm-exhaust"


class _Decision:
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


class _Reads:
    """A reader that would succeed — used to prove an exhausted job is never handed to it."""
    provider, model = "fake", "reads"

    async def read(self, body):
        from bruce_engine.semantic_contracts import Actionability, SemanticTurn, TurnRole
        return SemanticTurn(turn_role=TurnRole.conversation, actionability=Actionability.no_action,
                            confidence=0.9)


def _run(c):
    return asyncio.run(c)


# --- in memory: the whole life of a job that keeps killing its worker -------------------------------


class _LaterStore(semantic_shadow.InMemoryShadowStore):
    """The same store, looked at from further along. Time moves; the ROW is never edited by the test."""

    def __init__(self) -> None:
        super().__init__()
        self.skew = datetime.timedelta(0)

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc) + self.skew

    async def claim(self, worker_id, lease_seconds=semantic_shadow.DEFAULT_LEASE_SECONDS, **kw):
        return await super().claim(worker_id, lease_seconds, now=self._now())

    async def terminalize_exhausted(self, **kw):
        return await super().terminalize_exhausted(now=self._now())

    async def counts(self, **kw):
        return await super().counts(now=self._now())


def test_a_job_that_crashes_before_recording_still_runs_out_of_attempts(monkeypatch):
    """The unbounded path, walked end to end.

    Each claim is followed by nothing at all — the worker is gone. If `attempts` moved only on a recorded
    result, the fourth claim would succeed and so would the four-hundredth.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    uid = uuid4()

    async def _go():
        store = _LaterStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID, text="put lunch on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), store=store,
                                      tally=semantic_shadow.EnqueueLedger())
        row = next(iter(store.jobs.values()))
        claims = []
        for _ in range(row.max_attempts + 2):
            # The lease from the previous (dead) worker ages out, and another worker wakes.
            store.skew += datetime.timedelta(seconds=semantic_shadow.DEFAULT_LEASE_SECONDS + 1)
            claims.append(await store.claim("w"))
        return claims, row, store

    claims, row, store = asyncio.run(_go())

    assert [c is not None for c in claims] == [True] * row.max_attempts + [False, False], (
        "a job that never records was claimed past max_attempts — a turn whose read kills the worker is "
        "then re-claimed forever, burning a slot on every wake and never leaving the backlog")
    assert row.attempts == row.max_attempts, "attempts did not advance on claim"


def test_an_exhausted_job_becomes_a_typed_terminal_outcome_and_the_sweep_is_idempotent(monkeypatch):
    """What the exhausted row must LOOK like, and that closing it twice is the same as closing it once.

    Idempotency is not a nicety here: the sweep runs on every worker wake, and Cloud Tasks retries a wake
    freely, so "it ran twice" is the ordinary case rather than the exceptional one.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    uid = uuid4()

    async def _go():
        store = _LaterStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID, text="put lunch on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), store=store,
                                      tally=semantic_shadow.EnqueueLedger())
        row = next(iter(store.jobs.values()))
        for _ in range(row.max_attempts):
            store.skew += datetime.timedelta(seconds=semantic_shadow.DEFAULT_LEASE_SECONDS + 1)
            await store.claim("w")
        # The LAST dead worker's lease ages out too. Until it does the row is indistinguishable from one
        # being read right now, and the sweep correctly leaves it alone (see the test below).
        store.skew += datetime.timedelta(seconds=semantic_shadow.DEFAULT_LEASE_SECONDS + 1)
        before = await store.counts()
        first_sweep = await semantic_shadow.sweep_exhausted(store)
        after = await store.counts()
        second_sweep = await semantic_shadow.sweep_exhausted(store)
        # And nothing may pick it up again afterwards, sweep or no sweep.
        store.skew += datetime.timedelta(seconds=semantic_shadow.DEFAULT_LEASE_SECONDS + 1)
        reclaimed = await semantic_shadow.claim_and_observe(worker_id="w", store=store, triage=_Reads())
        return before, first_sweep, after, second_sweep, reclaimed, row, await store.counts()

    before, first_sweep, after, second_sweep, reclaimed, row, final = asyncio.run(_go())

    assert before["queued"] == 1, "an exhausted job left the backlog before anything closed it"
    assert first_sweep == 1, "the exhausted job was never closed out"
    assert second_sweep == 0, (
        "the second sweep touched a row again — terminalization must be idempotent, because a retried "
        "Cloud Task wake runs it twice as a matter of course")
    assert row.status == semantic_shadow.TERMINAL_FAILED
    assert row.outcome == semantic_shadow.EXHAUSTED, "the exhaustion was not recorded as a typed outcome"
    assert after["completed"] == 0 and final["completed"] == 0, (
        "a job that produced no reading was filed as completed — the exact bias the outbox removes")
    assert after["exhausted"] == 1, "exhaustion is not visible in the health numbers"
    assert after["queued"] == 0, "the closed job is still counted as backlog"
    assert reclaimed is None, "an exhausted job was claimed again after being closed"
    assert row.observation is None, "an unread turn was given an observation"


def test_the_sweep_leaves_a_job_that_is_still_being_read(monkeypatch):
    """The bound must not become a killer of live work.

    A worker on its FINAL attempt holds a valid lease. Sweeping that row would terminalize an observation
    that is seconds from being recorded, and the worker's own write would then be fenced out — losing the
    read that was about to succeed.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    uid = uuid4()

    async def _go():
        store = _LaterStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID, text="put lunch on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), store=store,
                                      tally=semantic_shadow.EnqueueLedger())
        row = next(iter(store.jobs.values()))
        for _ in range(row.max_attempts):
            store.skew += datetime.timedelta(seconds=semantic_shadow.DEFAULT_LEASE_SECONDS + 1)
            job = await store.claim("w")            # the last claim's lease is still LIVE
        swept = await semantic_shadow.sweep_exhausted(store)
        landed = await store.record(job, outcome=semantic_shadow.OK,
                                    status=semantic_shadow.COMPLETED,
                                    record=semantic_shadow.ShadowRecord(
                                        user_id=str(uid), channel=CHANNEL, message_id=PMID,
                                        exec_mode="conversation", exec_operation=None,
                                        exec_polarity="neutral", exec_confidence=0.9, exec_goal_id=None,
                                        exec_missing_count=0, exec_validation_codes=(),
                                        exec_latency_ms=3, router_class="fast_conversation",
                                        router_action=None, router_domain=None, router_capabilities=(),
                                        router_source="router_default", agrees=True, divergence=None))
        return swept, landed, row

    swept, landed, row = asyncio.run(_go())
    assert swept == 0, "the sweep closed a job whose worker still held a live lease"
    assert landed is True, "the last attempt could not record — the sweep raced a live read"
    assert row.status == semantic_shadow.COMPLETED and row.outcome == semantic_shadow.OK


def test_the_sweep_reads_nothing_and_calls_no_model(monkeypatch):
    """Closing a job out is BOOKKEEPING. It must not become a last-ditch attempt to read the turn.

    A tripwire rather than a comment: the sweep is the one place where "we never managed to read this
    one" could tempt someone into a retry with no attempts left, which would be an unbounded retry
    wearing a different name.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    from bruce_engine import semantic_executive, semantic_triage
    uid = uuid4()

    async def _tripwire(*a, **kw):
        raise AssertionError("the exhaustion sweep invoked the semantic executive")

    monkeypatch.setattr(semantic_executive, "interpret", _tripwire)
    monkeypatch.setattr(semantic_triage, "triage", _tripwire)

    async def _go():
        store = _LaterStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID, text="put lunch on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), store=store,
                                      tally=semantic_shadow.EnqueueLedger())
        row = next(iter(store.jobs.values()))
        for _ in range(row.max_attempts):
            store.skew += datetime.timedelta(seconds=semantic_shadow.DEFAULT_LEASE_SECONDS + 1)
            await store.claim("w")
        store.skew += datetime.timedelta(seconds=semantic_shadow.DEFAULT_LEASE_SECONDS + 1)
        return await semantic_shadow.sweep_exhausted(store)

    assert asyncio.run(_go()) == 1


def test_the_kill_switch_stops_the_sweep_too(monkeypatch):
    """Off means the queue is not touched AT ALL.

    Sweeping under a disabled switch would terminalize jobs that were only waiting for it to come back
    on — the same destruction `claim_and_observe` already refuses, arriving through the back door.
    """
    monkeypatch.delenv("BRUCE_SEMANTIC_SHADOW", raising=False)

    class _Explodes:
        async def terminalize_exhausted(self):
            raise AssertionError("the sweep touched the queue while the kill switch was off")

    assert asyncio.run(semantic_shadow.sweep_exhausted(_Explodes())) == 0


# --- Postgres: the same bound, enforced by the claim query -----------------------------------------

@pytest.fixture()
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    yield
    db._engine = None; db._sessionmaker = None


async def _expire(uid: UUID) -> None:
    """Age out the lease the dead worker left. Status, attempts and token are untouched."""
    async with worker_session() as s:
        await s.execute(sa_text(f"UPDATE {semantic_shadow.TABLE} SET lease_expires_at = now() - "
                                "interval '1 second' WHERE user_id = :uid"), {"uid": str(uid)})


async def _row(uid: UUID) -> dict:
    async with worker_session() as s:
        return dict((await s.execute(
            sa_text(f"SELECT status, outcome, attempts, max_attempts, observation, lease_token "
                    f"FROM {semantic_shadow.TABLE} WHERE user_id = :uid"), {"uid": str(uid)}
        )).mappings().one())


def test_postgres_stops_claiming_once_the_attempts_are_spent(clean_db, _pg):
    """The bound lives in the claim query's WHERE clause, which is the only place it can live: a worker
    that dies mid-read never reaches any check written in Python."""
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    store = semantic_shadow.PostgresShadowStore()

    async def _go():
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), tally=semantic_shadow.EnqueueLedger())
        claims = []
        for _ in range(5):
            claims.append(await store.claim("cloudrun-node7-4242", 60))
            await _expire(uid)                      # the worker died; the lease ages out
        before = await _row(uid)
        first = await store.terminalize_exhausted()
        after = await _row(uid)
        second = await store.terminalize_exhausted()
        blocked = await store.claim("cloudrun-node7-4242", 60)
        return claims, before, first, after, second, blocked, await semantic_shadow.counts()

    claims, before, first, after, second, blocked, counts = _run(_go())

    assert [c is not None for c in claims] == [True, True, True, False, False], (
        "the claim query kept handing out a job that had already spent every attempt")
    assert before["attempts"] == before["max_attempts"], "attempts did not advance on claim in SQL"
    assert first == 1 and second == 0, "terminalization is not idempotent in the database"
    assert after["status"] == semantic_shadow.TERMINAL_FAILED
    assert after["outcome"] == semantic_shadow.EXHAUSTED
    assert after["observation"] is None and after["lease_token"] is None
    assert blocked is None, "a terminalized job was claimed again"
    assert counts["exhausted"] == 1 and counts["completed"] == 0 and counts["queued"] == 0
