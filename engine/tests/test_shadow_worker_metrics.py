"""EVERY BACKLOG AND OUTCOME NUMBER IS EMITTED BY THE REAL WORKER RESPONSE, FROM A REAL QUEUE.

`counts()` and `backlog()` were written, documented and tested — and called by nothing in production. A
queue that is measurable in principle and measured nowhere fails the same way the detached task it
replaced did: work disappears, and the first person to notice is whoever eventually asks why the sample
is small. The wiring exists now, so this file holds it to the FULL set rather than to a sample of it —
a metric that is computed and then dropped by a hand-written key list has happened twice in this module
already (`unclassified_outcome`, and the whole `turns` block before that).

Two claims, and they are different:

  IT IS EMITTED     `worker_api.process`'s own response body carries every backlog state, every outcome
                    class, both queue ages, and the four reconciliation numbers. Asserted on the response
                    a Cloud Task actually records, not on `semantic_shadow.health()` in isolation — the
                    seam that was missing for a whole release was the CALLER, not the function.
  IT IS REAL        the queue is driven into those states through the real claim/lease/record machine
                    against real Postgres, so a number that is emitted but wrong is caught too.

AND A FAILED READ IS NOT AN EMPTY QUEUE. The last test drives that on purpose. A metrics path that
returns zeros when it cannot read reports its healthiest numbers exactly when the database is
unreachable, which is the original failure of this module wearing a dashboard.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import conversation_store, semantic_shadow, worker_api
from bruce_engine.db import user_session, worker_session
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.semantic_contracts import Actionability, SemanticTurn, TurnRole

users = PostgresUserRepository()

CHANNEL = "imessage"
IDENTITY = "+15550123"
TABLE = semantic_shadow.TABLE


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(
        db, "create_async_engine",
        lambda url, **kw: (kw.pop("poolclass", None),
                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    monkeypatch.delenv("BRUCE_ROUTER_SEMANTIC", raising=False)
    yield
    db._engine = None
    db._sessionmaker = None


@pytest.fixture()
def quiet_worker(monkeypatch):
    """The intake and mission drains are stubbed; the shadow measurement is not.

    `claim_and_observe` is stubbed to a no-op for one reason only: the real one would call the live
    Stage-1 provider over the network. The QUEUE it measures is built below through that same function
    with fake providers, so nothing about the numbers is faked — only the wake's extra drain is.
    """
    async def _no_intake(*a, **kw):
        return False

    async def _no_drain(**kw):
        return None

    monkeypatch.setattr(worker_api.worker, "process_one", _no_intake)
    monkeypatch.setattr(worker_api, "background_runner_enabled", lambda: False)
    monkeypatch.setattr(worker_api.semantic_shadow, "claim_and_observe", _no_drain)


class _Decision:
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


class _Reads:
    """A usable answer -> outcome `ok` -> status `completed`."""

    provider, model = "fake", "reads"

    async def read(self, body):
        return SemanticTurn(turn_role=TurnRole.conversation, actionability=Actionability.no_action,
                            confidence=0.9)


class _Malformed:
    """The model ANSWERED and the answer is unusable -> `invalid_schema` -> the `malformed` bucket."""

    provider, model = "fake", "malformed"

    async def read(self, body):
        return {"not": "a semantic turn"}


def _run(c):
    return asyncio.run(c)


# THE REAL DRAIN, captured at import. `quiet_worker` replaces `semantic_shadow.claim_and_observe` on the
# module object, and `worker_api.semantic_shadow` IS that module — so the stub is global for the test and
# the queue below would be built by calling the stub. Holding the original here keeps the two apart:
# the QUEUE is driven by the production drain, and only the WAKE's extra pass is silenced.
_REAL_DRAIN = semantic_shadow.claim_and_observe


async def _queued_turn(uid: UUID, *, pmid: str, age_s: int = 300) -> UUID:
    turn_id = await conversation_store.persist_user_turn(
        uid, channel=CHANNEL, channel_identity=IDENTITY, provider_message_id=pmid, text="hey bruce")
    async with user_session(uid) as s:
        await s.execute(
            sa_text("UPDATE conversation_turns SET created_at = now() - make_interval(secs => :age) "
                    "WHERE id = :id"), {"age": age_s, "id": str(turn_id)})
    await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=pmid, decision=_Decision(),
                                 conversation_turn_id=turn_id,
                                 tally=semantic_shadow.EnqueueLedger())
    return turn_id


async def _age_row(turn_id: UUID, *, created_s: int | None = None, updated_s: int | None = None,
                   attempts: int | None = None, lease_expired: bool = False) -> None:
    """Move ONE job row's clock (and, for the crash shape, its attempt counter).

    A worker killed between claiming and recording leaves exactly this behind: a `processing` row whose
    attempts have already advanced and whose lease is expiring. Reproducing it by hand is the only way to
    reach it without killing a process mid-test, and it is the state `sweep_exhausted` exists for.
    """
    sets, params = [], {"t": str(turn_id)}
    if created_s is not None:
        sets.append("created_at = now() - make_interval(secs => :c)")
        params["c"] = created_s
    if updated_s is not None:
        sets.append("updated_at = now() - make_interval(secs => :u)")
        params["u"] = updated_s
    if attempts is not None:
        sets.append("attempts = :a")
        params["a"] = attempts
    if lease_expired:
        sets.append("lease_expires_at = now() - make_interval(secs => 30)")
    async with worker_session() as s:
        await s.execute(sa_text(f"UPDATE {TABLE} SET {', '.join(sets)} WHERE conversation_turn_id = :t"),
                        params)


async def _build_a_queue_holding_one_of_everything(uid: UUID) -> dict:
    """Five turns driven into five different queue states through the real machine."""
    store = semantic_shadow.PostgresShadowStore()
    ids = {}
    for name, pmid in (("completed", "pm-1"), ("malformed", "pm-2"), ("processing", "pm-3"),
                       ("exhausted", "pm-4"), ("pending", "pm-5")):
        ids[name] = await _queued_turn(uid, pmid=pmid)

    # `claim` takes the oldest pending row, so these two land on pm-1 and pm-2 in order.
    await _REAL_DRAIN(worker_id="w-metrics", triage=_Reads())
    await _REAL_DRAIN(worker_id="w-metrics", triage=_Malformed())

    # pm-3: claimed and ABANDONED — a worker still holding a live lease. It is backlog, not a failure,
    # and `oldest_processing_age_s` is the number that says how long one claim has been held.
    claimed = await store.claim("w-metrics")
    assert claimed is not None
    await _age_row(ids["processing"], updated_s=240)

    # pm-4: the crash shape. Claimed, never recorded, out of attempts, lease expired — the row that would
    # sit in the backlog forever if the wake did not sweep it.
    exhausted = await store.claim("w-metrics")
    assert exhausted is not None
    await _age_row(ids["exhausted"], attempts=exhausted.max_attempts, lease_expired=True)

    # pm-5: never claimed. `oldest_pending_age_s` is how long a turn has waited to be read AT ALL, which
    # is a different question from how long a claim has been held, and one number cannot answer both.
    await _age_row(ids["pending"], created_s=480)
    return ids


# --- the emission -------------------------------------------------------------------------------------

def test_the_worker_response_emits_every_backlog_and_outcome_number(clean_db, quiet_worker):
    """The full contract, on the response body a Cloud Task records.

    Each name is here because it has a different owner. An unusable answer is an output-contract fix; a
    lost packet is infrastructure; a job that was never read at all is neither; and a turn waiting to be
    claimed is a different problem from a claim that is stuck. Collapsing any of them would send the next
    investigation to the wrong place.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_build_a_queue_holding_one_of_everything(uid))

    out = _run(worker_api.process())
    q = out["shadow_health"]["queue"]

    assert q["status"] == semantic_shadow.QUEUE_OK
    assert out["shadow_exhausted"] == 1, "the wake did not close out the crashed job"
    assert out["shadow_backlog_before"] >= 0, (
        "backlog() has no production caller again — the backlog a wake INHERITED is what says whether "
        "the drain keeps up, and the queue it leaves behind cannot tell a working drain from a dead one")

    # backlog states
    assert q["pending"] == 1
    assert q["processing"] == 1 and q["leased"] == 1, (
        "`processing` is the status the row actually holds and `leased` is what this block has always "
        "published; dropping either renames a live metric to zero without failing anything")
    assert q["retryable"] == 0

    # outcome classes
    assert q["completed"] == 1, "a successful observation is not counted as completed"
    assert q["malformed"] == 1, "an unusable model answer was not counted as malformed"
    assert q["exhausted"] == 1, (
        "the swept crash job is not counted under `exhausted` — a turn nobody could ever read is "
        "EVIDENCE, and it must never be silently absent or filed as completed")
    assert q["model_failed"] == 0 and q["infrastructure_failed"] == 0
    assert q["unclassified_outcome"] == 0, (
        "the bucket that means 'this queue holds something I do not understand' is not published, so "
        "nothing can alert on it — it was computed and then dropped once already")

    # ages: two numbers, two questions
    assert q["oldest_pending_age_s"] >= 400, (
        "a turn that has waited eight minutes to be read at all reads as fresh")
    assert q["oldest_processing_age_s"] >= 200, (
        "a claim held for four minutes reads as fresh — a worker stuck mid-read and a drain that never "
        "wakes need different fixes, and one 'oldest job' number cannot tell them apart")
    assert q["oldest_processing_age_s"] == q["oldest_leased_age_s"]

    # and the queue still adds up, with anything it cannot classify named rather than lost
    assert q["enqueued"] == q["pending"] + q["processing"] + q["retryable"] + q["terminal"]
    assert q["unaccounted"] == 0 and q["reconciled"] is True


def test_the_worker_response_emits_the_reconciliation_numbers(clean_db, quiet_worker):
    """The independent ledger's numbers, on the same response.

    The canonical total, the gap, and the STATUS. The status is not a boolean on purpose:
    `reconciled: True/False` cannot express "I could not read the ledger", and that third state
    collapsing into the first is precisely what this round of work was about.

    There is deliberately no count of USERS here any more. It used to be published as `checked_users`,
    and it was published because the worker had the owner list — which is the enumeration migration 0038
    removed. The comparison happens inside the definer now, so how many owners are in the window is a
    fact this process does not have and does not need.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_build_a_queue_holding_one_of_everything(uid))

    turns = _run(worker_api.process())["shadow_health"]["turns"]

    assert turns["canonical_trusted_turns"] == 5
    assert turns["trusted_turns_with_intake"] == 5 and turns["trusted_turns_without_intake"] == 0
    assert turns["unobserved_turns"] == 0
    assert turns["reconciliation_status"] == semantic_shadow.RECON_CLEAN
    assert turns["reconciliation_status"] in (
        semantic_shadow.RECON_CLEAN, semantic_shadow.RECON_FAILED, semantic_shadow.RECON_UNKNOWN), (
        "the reconciliation status is outside its own vocabulary")
    assert "checked_users" not in turns, (
        "the emission counts owners again — knowing how many is one query away from knowing which")


# --- a failed read is not an empty queue --------------------------------------------------------------

def test_a_failed_queue_read_is_distinguishable_from_a_genuinely_empty_queue(clean_db, quiet_worker,
                                                                             monkeypatch):
    """Both states, side by side, so neither can be mistaken for the other.

    An EMPTY queue publishes `status: ok` and honest zeros. A FAILED read publishes `status: unknown` and
    NO NUMBERS AT ALL — not zeros. Zeros are what a dashboard draws, and a dashboard that draws its
    healthiest picture when the database is unreachable is worse than no dashboard.
    """
    empty = _run(worker_api.process())["shadow_health"]["queue"]
    assert empty["status"] == semantic_shadow.QUEUE_OK
    assert empty["pending"] == 0 and empty["queued"] == 0 and empty["completed"] == 0

    async def _boom(self):
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr(semantic_shadow.PostgresShadowStore, "counts", _boom)
    out = _run(worker_api.process())
    broken = out["shadow_health"]["queue"]

    assert broken["status"] == semantic_shadow.QUEUE_UNKNOWN, (
        "a failed queue read published the same shape as an empty queue")
    for absent in ("pending", "processing", "completed", "queued", "oldest_pending_age_s"):
        assert absent not in broken, (
            f"{absent!r} was published by a read that failed — a fabricated zero here says the queue is "
            "healthy at the exact moment nobody can see it")
    assert out["shadow_errors"] >= 1, (
        "a failed metrics read was swallowed into a clean-looking response body")

