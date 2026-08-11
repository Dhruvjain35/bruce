"""THE LEASE FENCE — a worker that no longer holds the job must write NOTHING, even from the same name.

The completion writes used to guard themselves with `AND lease_owner = :worker`. That reads like a fence
and is not one, because the worker id is `cloudrun-<nodename>-<pid>` and Cloud Run serves overlapping
/process invocations FROM ONE CONTAINER: two workers with the same node and the same pid produce the same
string, so both satisfy the guard at the same instant. The write it was written to stop — a worker whose
lease expired mid-read finishing on top of the worker that legitimately reclaimed the job — was admitted
by it.

Two ways that corrupts the sample, and neither leaves a mark:

  OVERWRITE      the newer worker's reading is replaced by the stale one, so the row holds an observation
                 of a read that lost its claim. It looks clean. Nothing flags it.
  RESURRECTION   a job another worker already COMPLETED is written back to a different status, so a turn
                 counted once is counted again, or a completed row silently reopens.

Both matter because these rows are the evidence for whether the semantic executive may be given
authority. A missing row is visible in the backlog; a wrong row is not visible anywhere.

So every test here gives the stale worker EVERY advantage: the same lease_owner string, the same job id,
the same user. The only thing it does not have is the token minted by the claim it lost, and that has to
be enough on its own.

The in-memory cases prove the rule the store enforces; the Postgres cases prove the SQL enforces it, and
those are different machinery — deleting the `lease_token` condition from the UPDATE leaves the in-memory
store perfectly happy.
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
CHANNEL, PMID = "imessage", "pm-fence"

# ONE container, TWO overlapping invocations. Deliberately identical: this string is what the old guard
# compared, and the whole point is that it cannot tell the two workers apart.
SAME_WORKER = "cloudrun-node7-4242"


def _run(c):
    return asyncio.run(c)


def _record(latency_ms: int = 7) -> semantic_shadow.ShadowRecord:
    """A minimal well-formed observation — the payload a completing worker would write."""
    return semantic_shadow.ShadowRecord(
        user_id="u", channel=CHANNEL, message_id=PMID, exec_mode="conversation", exec_operation=None,
        exec_polarity="neutral", exec_confidence=0.9, exec_goal_id=None, exec_missing_count=0,
        exec_validation_codes=(), exec_latency_ms=latency_ms, router_class="fast_conversation",
        router_action=None, router_domain=None, router_capabilities=(), router_source="router_default",
        agrees=True, divergence=None)


class _Decision:
    """Shape-compatible with RouterDecision — the snapshot, not the subject."""
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


# --- in memory: the rule ---------------------------------------------------------------------------

async def _one_job(uid: UUID) -> semantic_shadow.InMemoryShadowStore:
    store = semantic_shadow.InMemoryShadowStore()
    store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID, text="put lunch on friday")
    await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                  decision=_Decision(), store=store,
                                  tally=semantic_shadow.EnqueueLedger())
    return store


def _steal(store: semantic_shadow.InMemoryShadowStore):
    """Expire the lease and let the SAME worker name claim it again — the production race, exactly."""
    row = next(iter(store.jobs.values()))
    row.lease_expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    return row


def test_a_stale_worker_sharing_the_lease_owner_writes_nothing(monkeypatch):
    """The founding case. Same container, same worker string, expired lease — and zero effect."""
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    uid = uuid4()

    async def _go():
        store = await _one_job(uid)
        first = await store.claim(SAME_WORKER)
        _steal(store)
        second = await store.claim(SAME_WORKER)
        stale_write = await store.record(first, outcome=semantic_shadow.OK,
                                         status=semantic_shadow.COMPLETED, record=_record(999))
        live_write = await store.record(second, outcome=semantic_shadow.OK,
                                        status=semantic_shadow.COMPLETED, record=_record(7))
        return first, second, stale_write, live_write, next(iter(store.jobs.values()))

    first, second, stale_write, live_write, row = asyncio.run(_go())

    assert first.lease_owner == second.lease_owner == SAME_WORKER, (
        "the two claims must share a lease_owner — otherwise this test is not reproducing the race")
    assert first.lease_token != second.lease_token, "the claim did not mint a new token"
    assert stale_write is False, (
        "a worker that lost its lease wrote its result anyway — with lease_owner as the only guard, two "
        "workers on one container both pass it")
    assert live_write is True, "the worker that actually holds the job could not record"
    assert row.observation["exec"]["latency_ms"] == 7, (
        "the stale worker's reading overwrote the reading of the worker that holds the claim")


def test_a_stale_worker_cannot_reschedule_a_job_it_lost(monkeypatch):
    """`mark_retryable` is the same hazard wearing the retry path: rescheduling a job another worker is
    mid-read on would send one turn through the executive twice and count it twice."""
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    uid = uuid4()

    async def _go():
        store = await _one_job(uid)
        first = await store.claim(SAME_WORKER)
        _steal(store)
        second = await store.claim(SAME_WORKER)
        stale = await store.mark_retryable(first, outcome=semantic_shadow.TRANSPORT)
        return stale, next(iter(store.jobs.values())), second

    stale, row, second = asyncio.run(_go())
    assert stale is False, "a stale worker rescheduled a job it no longer holds"
    assert row.status == semantic_shadow.PROCESSING, "the live claim was knocked out of processing"
    assert row.lease_token == second.lease_token, "the live worker's lease was overwritten"


def test_a_completed_job_cannot_be_resurrected(monkeypatch):
    """The second corruption. A completed row that a late worker writes back is a turn counted twice, and
    the `status = processing` half of the fence is what refuses it."""
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    uid = uuid4()

    async def _go():
        store = await _one_job(uid)
        first = await store.claim(SAME_WORKER)
        _steal(store)
        second = await store.claim(SAME_WORKER)
        await store.record(second, outcome=semantic_shadow.OK, status=semantic_shadow.COMPLETED,
                           record=_record(7))
        late = await store.record(first, outcome=semantic_shadow.INVALID_SCHEMA,
                                  status=semantic_shadow.TERMINAL_FAILED, record=None)
        return late, next(iter(store.jobs.values())), await store.counts()

    late, row, counts = asyncio.run(_go())
    assert late is False, "a finished job was written back by a worker that had already lost it"
    assert row.status == semantic_shadow.COMPLETED and row.outcome == semantic_shadow.OK
    assert counts["outcomes"] == {semantic_shadow.OK: 1}, "one turn produced two outcomes"


def test_the_worker_reports_that_a_fenced_write_did_not_land(monkeypatch):
    """A rejected write must be VISIBLE, not merely harmless.

    `record` returning False silently would leave the drain reporting an observation that no row holds —
    the same "it looked fine and the record was gone" shape the outbox exists to end.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    uid = uuid4()

    class _StealsTheLease:
        """A reader that lets another worker claim the job WHILE this read is in flight."""
        provider, model = "fake", "slow"

        def __init__(self, store): self._store = store

        async def read(self, body):
            from bruce_engine.semantic_contracts import Actionability, SemanticTurn, TurnRole
            _steal(self._store)
            await self._store.claim(SAME_WORKER)
            return SemanticTurn(turn_role=TurnRole.conversation, actionability=Actionability.no_action,
                                confidence=0.9)

    async def _go():
        store = await _one_job(uid)
        observed = await semantic_shadow.claim_and_observe(worker_id=SAME_WORKER, store=store,
                                                           triage=_StealsTheLease(store))
        return observed, next(iter(store.jobs.values()))

    observed, row = asyncio.run(_go())
    assert observed is not None and observed.outcome == semantic_shadow.OK
    assert observed.recorded is False, (
        "the drain reported an observation that was fenced out — a turn counted in telemetry that no row "
        "holds is exactly the discrepancy this table was built to make impossible")
    assert row.observation is None, "the fenced write landed anyway"


# --- Postgres: the SQL ------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    yield
    db._engine = None; db._sessionmaker = None


async def _expire_lease(uid: UUID) -> None:
    """Age the lease out WITHOUT touching status, attempts or the token — the state a crashed worker
    leaves behind. Written from the test only because sleeping through a 60s lease is not a test."""
    async with worker_session() as s:
        await s.execute(sa_text(f"UPDATE {semantic_shadow.TABLE} SET lease_expires_at = now() - "
                                "interval '1 second' WHERE user_id = :uid"), {"uid": str(uid)})


async def _row(uid: UUID) -> dict:
    async with worker_session() as s:
        return dict((await s.execute(
            sa_text(f"SELECT status, outcome, observation, latency_ms, lease_token, attempts, version "
                    f"FROM {semantic_shadow.TABLE} WHERE user_id = :uid"), {"uid": str(uid)}
        )).mappings().one())


def test_postgres_refuses_a_stale_write_from_an_identical_worker_id(clean_db):
    """The same race, decided by the UPDATE's WHERE clause rather than by a Python guard.

    This is the test that can actually fail if the `lease_token` condition is deleted: the in-memory store
    models the rule, but only the database enforces it on the path production takes.
    """
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    store = semantic_shadow.PostgresShadowStore()

    async def _go():
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), tally=semantic_shadow.EnqueueLedger())
        first = await store.claim(SAME_WORKER, 60)
        await _expire_lease(uid)
        second = await store.claim(SAME_WORKER, 60)
        held = await _row(uid)                      # the row as the LIVE claim left it
        stale = await store.record(first, outcome=semantic_shadow.OK,
                                   status=semantic_shadow.COMPLETED, record=_record(999))
        after_stale = await _row(uid)
        live = await store.record(second, outcome=semantic_shadow.OK,
                                  status=semantic_shadow.COMPLETED, record=_record(7))
        return first, second, held, stale, after_stale, live, await _row(uid)

    first, second, held, stale, after_stale, live, final = _run(_go())

    assert first.lease_owner == second.lease_owner == SAME_WORKER
    assert first.lease_token and second.lease_token and first.lease_token != second.lease_token, (
        "two claims of one row produced the same token — the fence is a constant, not a fence")
    assert stale is False, "Postgres accepted a write from a worker whose claim had been taken"
    assert after_stale["version"] == held["version"], (
        "the stale write matched a row: `version` advances on every UPDATE this module makes, so an "
        "unchanged version is the proof that the statement affected ZERO rows rather than writing "
        "something that happened to look the same")
    assert after_stale["status"] == semantic_shadow.PROCESSING, (
        "the stale write finished a job it did not hold")
    assert after_stale["observation"] is None, "the stale worker's reading reached the row"
    assert live is True and final["status"] == semantic_shadow.COMPLETED
    assert final["latency_ms"] == 7, "the row does not hold the reading of the worker that owned it"
    assert final["lease_token"] is None, "a finished job still carries a lease token"


def test_postgres_refuses_to_resurrect_a_finished_job(clean_db):
    """`status = processing` in the fence. A completed row is not merely locked to its owner — it is
    closed, and a worker arriving late with the RIGHT token still may not reopen it."""
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    store = semantic_shadow.PostgresShadowStore()

    async def _go():
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), tally=semantic_shadow.EnqueueLedger())
        job = await store.claim(SAME_WORKER, 60)
        await store.record(job, outcome=semantic_shadow.OK, status=semantic_shadow.COMPLETED,
                           record=_record(7))
        # The very same claim, replayed — a retried coroutine, a duplicated Cloud Task.
        replay = await store.record(job, outcome=semantic_shadow.INVALID_SCHEMA,
                                    status=semantic_shadow.TERMINAL_FAILED, record=None)
        retry = await store.mark_retryable(job, outcome=semantic_shadow.TRANSPORT)
        return replay, retry, await _row(uid)

    replay, retry, row = _run(_go())
    assert replay is False and retry is False, "a completed observation was rewritten"
    assert row["status"] == semantic_shadow.COMPLETED and row["outcome"] == semantic_shadow.OK
