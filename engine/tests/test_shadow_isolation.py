"""SHADOW ISOLATION AND DURABILITY — a shadow that can change a student's turn is not a shadow, and a
shadow that loses its slow reads is not a measurement.

"Semantic authority is off" is a claim about DECISIONS. It says nothing about latency, and nothing about
failure propagation. The first wiring awaited the read inline, before the student's reply was composed —
so a slow model would have added the full triage deadline plus a transport retry to every turn, and a
raised exception would have travelled up the request path. Authority was off the whole time and Bruce
would still have been changed. The second wiring fixed that with a detached task, and traded one defect
for a quieter one: a restart, a container shutdown or a cancelled worker dropped the record, and it
dropped the SLOW and FAILING reads specifically, because the fast successes had already finished. The
sample deciding whether the executive may be given authority was therefore biased toward exactly the
reads that prove the least.

So this file protects both properties at once:

  ISOLATION   the request path never calls a model, never raises, and never depends on the read
  DURABILITY  the observation is a row; failures become TYPED OUTCOMES rather than missing rows; a
              retry converges to one record; and a failed read is NEVER filed as completed

These are deliberately NOT model tests. Each injects a failing reader, because the point is the boundary
rather than the quality of any reading — and a test that needed a live model could not run in the suite
that has to protect these properties on every commit.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from uuid import uuid4

import pytest

from bruce_engine import semantic_shadow

CHANNEL, PMID = "imessage", "pm-1"


class _Decision:
    """The router decision shadow is compared against. Shape-compatible with RouterDecision."""
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"
    decision_id = None


class _Hangs:
    """A reader that never returns — the deadline cases."""
    provider, model = "fake", "hangs"

    async def read(self, body):
        await asyncio.sleep(3600)


class _Throws:
    """An unusable answer dressed as an exception — nothing about the transport is wrong."""
    provider, model = "fake", "throws"

    async def read(self, body):
        raise RuntimeError("provider exploded")


class _Drops:
    """A lost packet. THE ONLY class of failure that may be retried."""
    provider, model = "fake", "drops"

    async def read(self, body):
        raise ConnectionError("connection reset")


class _Malformed:
    """Answers with something that is not a SemanticTurn at all."""
    provider, model = "fake", "malformed"

    async def read(self, body):
        return {"not": "a semantic turn"}


class _Reads:
    """A reader that succeeds, so `ok` and the comparison can be exercised."""
    provider, model = "fake", "reads"

    async def read(self, body):
        from bruce_engine.semantic_contracts import Actionability, SemanticTurn, TurnRole
        return SemanticTurn(turn_role=TurnRole.conversation, actionability=Actionability.no_action,
                            confidence=0.9)


def _shadow_on(monkeypatch, budget: str = "5"):
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", float(budget))


async def _queued(user_id, *, text: str = "put lunch on friday"):
    """A store holding one enqueued turn and the conversation_turns row it references."""
    store = semantic_shadow.InMemoryShadowStore()
    store.add_turn(user_id, channel=CHANNEL, provider_message_id=PMID, text=text)
    await semantic_shadow.intake(user_id, channel=CHANNEL, provider_message_id=PMID,
                                  decision=_Decision(), store=store)
    return store


# --- the request path -----------------------------------------------------------------------------

def test_enqueueing_never_invokes_the_executive(monkeypatch):
    """The request path writes a row and stops.

    A tripwire rather than a timing assertion: a clock is flaky on loaded CI, and "did a model get
    called" is the property — a shadow read on the request path is what put the whole triage deadline on
    a student's reply the first time this was wired.
    """
    _shadow_on(monkeypatch)
    from bruce_engine import semantic_executive, semantic_triage

    async def _tripwire(*a, **kw):
        raise AssertionError("the request path invoked the semantic executive")

    monkeypatch.setattr(semantic_executive, "interpret", _tripwire)
    monkeypatch.setattr(semantic_triage, "triage", _tripwire)

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        started = time.perf_counter()
        created = await semantic_shadow.intake(uuid4(), channel=CHANNEL, provider_message_id=PMID,
                                                decision=_Decision(), store=store)
        return created, time.perf_counter() - started, len(store.jobs)

    created, elapsed, rows = asyncio.run(_go())
    assert created and rows == 1, "the turn was not queued for observation"
    assert elapsed < 0.25, f"enqueue took {elapsed:.3f}s — it is doing more than one insert"


def test_a_failing_queue_write_never_breaks_the_turn(monkeypatch):
    """Telemetry that can fail a student's turn is worse than no telemetry."""
    _shadow_on(monkeypatch)

    class _BrokenStore:
        async def enqueue(self, *a, **kw):
            raise RuntimeError("the database is on fire")

    async def _go():
        return await semantic_shadow.intake(uuid4(), channel=CHANNEL, provider_message_id=PMID,
                                             decision=_Decision(), store=_BrokenStore())

    assert asyncio.run(_go()) is False, "a failed enqueue must report False, not raise into the turn"


def test_exactly_one_job_per_turn(monkeypatch):
    """A webhook redelivery must not become a second observation of one turn.

    Asserted through the store's uniqueness rather than through a guard in the caller: the real
    constraint is UNIQUE(user_id, channel, provider_message_id), because a SELECT-then-INSERT would race
    two concurrent deliveries past any caller-side check.
    """
    _shadow_on(monkeypatch)
    uid = uuid4()

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        first = await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                              decision=_Decision(), store=store)
        second = await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                               decision=_Decision(), store=store)
        return first, second, len(store.jobs)

    first, second, rows = asyncio.run(_go())
    assert first is True and second is False
    assert rows == 1, "one turn produced two shadow jobs"


# --- the kill switch ------------------------------------------------------------------------------

def test_the_kill_switch_prevents_job_creation(monkeypatch):
    """Off means no row at all — not a row whose result is discarded."""
    monkeypatch.delenv("BRUCE_SEMANTIC_SHADOW", raising=False)

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        created = await semantic_shadow.intake(uuid4(), channel=CHANNEL, provider_message_id=PMID,
                                                decision=_Decision(), store=store)
        return created, len(store.jobs)

    assert asyncio.run(_go()) == (False, 0)


def test_an_already_queued_job_no_ops_while_the_switch_is_off(monkeypatch):
    """A job queued before the switch went off must not be read, and must not be destroyed either.

    Leaving it queued is the deliberate choice: draining it as "skipped" would throw away turns the
    switch was only briefly off for, and the backlog stays visible in `counts()` meanwhile.
    """
    _shadow_on(monkeypatch)
    uid = uuid4()

    async def _go():
        store = await _queued(uid)
        monkeypatch.delenv("BRUCE_SEMANTIC_SHADOW", raising=False)
        observed = await semantic_shadow.claim_and_observe(worker_id="w1", store=store,
                                                           triage=_Hangs())
        return observed, await store.counts()

    observed, counts = asyncio.run(_go())
    assert observed is None, "a queued job was claimed while the kill switch was off"
    assert counts["pending"] == 1 and counts["queued"] == 1, "the queued turn was consumed or lost"


# --- typed outcomes -------------------------------------------------------------------------------

@pytest.mark.parametrize("reader,budget,expected", [
    (_Hangs(), "0.2", semantic_shadow.BUDGET_EXCEEDED),
    (_Throws(), "5", semantic_shadow.INVALID_SCHEMA),
    (_Malformed(), "5", semantic_shadow.INVALID_SCHEMA),
])
def test_a_broken_read_becomes_a_typed_outcome_and_is_never_completed(monkeypatch, reader, budget,
                                                                     expected):
    """Every way a read can fail leaves a ROW that says which way it was.

    The `completed` assertion is the important half. A failed observation filed as completed is the bias
    this whole outbox exists to remove: the collected sample would then contain only the reads that went
    well, which is the population that must not be over-represented when deciding whether the executive
    may be given authority.
    """
    _shadow_on(monkeypatch, budget=budget)
    uid = uuid4()

    async def _go():
        store = await _queued(uid)
        observed = await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=reader)
        return observed, await store.counts()

    observed, counts = asyncio.run(_go())
    assert observed is not None and observed.outcome == expected
    assert observed.status == semantic_shadow.TERMINAL_FAILED
    assert counts["completed"] == 0, f"a {expected} observation was marked completed"
    assert counts["outcomes"] == {expected: 1}, "the failure was not recorded as a typed outcome"


def test_a_deadline_is_recorded_as_a_timeout_not_as_a_budget_kill(monkeypatch):
    """The triage deadline and the shadow's own ceiling are different findings and must stay distinct.

    Collapsing them would hide which one is firing — and a slow executive versus a worker holding a
    lease too long need different fixes.
    """
    _shadow_on(monkeypatch, budget="5")
    from bruce_engine import semantic_triage
    monkeypatch.setattr(semantic_triage, "TRIAGE_TIMEOUT_S", 0.05)
    uid = uuid4()

    async def _go():
        store = await _queued(uid)
        return await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Hangs())

    observed = asyncio.run(_go())
    assert observed.outcome == semantic_shadow.TIMEOUT
    assert observed.status == semantic_shadow.TERMINAL_FAILED


def test_a_missing_turn_is_recorded_rather_than_invented(monkeypatch):
    """The job references the turn instead of copying its text, so the text can legitimately be gone.

    That is a feature — a deletion must reach the student's words — and the honest record of it is an
    outcome, never a silent completion or a resurrected copy.
    """
    _shadow_on(monkeypatch)
    uid = uuid4()

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()          # enqueued, but no conversation turn
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), store=store)
        return await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Reads())

    observed = asyncio.run(_go())
    assert observed.outcome == semantic_shadow.TURN_MISSING
    assert observed.status == semantic_shadow.TERMINAL_FAILED


def test_only_infrastructure_failure_is_retried():
    """A measurement that is retried until it succeeds is a biased measurement.

    A lost packet says nothing about the executive, so it is retried. A timeout, a rejection or an
    unusable answer are facts ABOUT the executive on that turn, and re-rolling them would resample the
    population until it looked healthy.
    """
    for outcome in (semantic_shadow.TIMEOUT, semantic_shadow.PROVIDER_REJECTED,
                    semantic_shadow.INVALID_SCHEMA, semantic_shadow.BUDGET_EXCEEDED,
                    semantic_shadow.TURN_MISSING):
        assert semantic_shadow.status_for(outcome, can_retry=True) == semantic_shadow.TERMINAL_FAILED, (
            f"{outcome} would be retried — that resamples the turn until the read succeeds")
    assert semantic_shadow.status_for(semantic_shadow.TRANSPORT,
                                      can_retry=True) == semantic_shadow.RETRYABLE_FAILED
    assert semantic_shadow.status_for(semantic_shadow.TRANSPORT,
                                      can_retry=False) == semantic_shadow.TERMINAL_FAILED
    assert semantic_shadow.status_for(semantic_shadow.OK,
                                      can_retry=True) == semantic_shadow.COMPLETED


def test_a_retried_job_converges_to_one_record(monkeypatch):
    """Retrying must not double-count a turn.

    The observation lives ON the job row, so the second attempt UPDATEs the first rather than appending
    a second reading. Two records for one turn would inflate agreement (or disagreement) by however many
    times the transport happened to fail.
    """
    _shadow_on(monkeypatch)
    uid = uuid4()

    async def _go():
        store = await _queued(uid)
        dropped = await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Drops())
        # Expire the backoff lease rather than sleeping through it — a retryable row becomes claimable
        # again by exactly this route in production, whether the worker backed off or crashed.
        row = next(iter(store.jobs.values()))
        row.lease_expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        second = await semantic_shadow.claim_and_observe(worker_id="w2", store=store, triage=_Reads())
        return dropped, second, row, await store.counts()

    dropped, second, row, counts = asyncio.run(_go())
    assert dropped.outcome == semantic_shadow.TRANSPORT
    assert dropped.status == semantic_shadow.RETRYABLE_FAILED
    assert second.outcome == semantic_shadow.OK and second.status == semantic_shadow.COMPLETED
    assert len(counts["outcomes"]) == 1 and counts["outcomes"][semantic_shadow.OK] == 1, (
        "the retry left two outcomes for one turn")
    assert counts["completed"] == 1 and counts["queued"] == 0
    assert row.attempts == 2, "the retry did not reuse the same job row"


# --- measurement ----------------------------------------------------------------------------------

def test_backlog_and_outcome_counts_make_a_dropped_observation_visible(monkeypatch):
    """The numbers that say whether the collected sample can be trusted.

    A queued count that only grows is the signal that the drain has stopped — which is precisely what
    was UNMEASURABLE while shadow was a detached task, because a dropped observation left nothing behind
    at all.
    """
    _shadow_on(monkeypatch)

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        for i in range(3):
            uid = uuid4()
            store.add_turn(uid, channel=CHANNEL, provider_message_id=f"pm-{i}", text="hey")
            await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=f"pm-{i}",
                                          decision=_Decision(), store=store)
        before = await semantic_shadow.backlog(store)
        await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Reads())
        await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Throws())
        return before, await semantic_shadow.backlog(store), await semantic_shadow.counts(store)

    before, after, counts = asyncio.run(_go())
    assert before == 3, "queued turns are not visible as a backlog"
    assert after == 1, "the drained turns are still counted as queued"
    assert counts["completed"] == 1 and counts["terminal_failed"] == 1
    assert counts["outcomes"] == {semantic_shadow.OK: 1, semantic_shadow.INVALID_SCHEMA: 1}, (
        "the per-outcome breakdown is what says whether the sample is healthy")


# --- the request path, driven end to end --------------------------------------------------------


@pytest.fixture()
def _runtime_pg(pg_test_db, monkeypatch):
    """A real database for the one test that drives a whole turn.

    NullPool, matching `test_conversation_runtime`: the turn is driven under its own `asyncio.run`, and a
    pooled connection bound to a closed loop is reused by the next test as a hang rather than an error.
    """
    from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
    from sqlalchemy.pool import NullPool

    import bruce_engine.db as db

    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield pg_test_db
    db._engine = None
    db._sessionmaker = None


def test_a_students_turn_never_waits_on_the_semantic_model(clean_db, _runtime_pg, monkeypatch):
    """Drive a REAL turn through `conversation_runtime.handle` and prove no semantic read happened in it.

    Every other test in this file calls `enqueue` directly, so none of them can see what the runtime
    actually does at the call site. This one can: it runs the production inbound handler against real
    Postgres with a tripwire on `SemanticTriage.read` — the single seam every semantic read passes
    through, whether it is reached via `semantic_executive.interpret`, `semantic_triage.triage`, or a
    reintroduced inline observe.

    The tripwire RECORDS and then raises, and the recording is what is asserted on. Raising alone would
    prove nothing: `interpret` is documented never to raise, so an awaited read that blew up would be
    swallowed into a validation note and the turn would look perfectly healthy — which is exactly how the
    first wiring passed review while putting the whole triage deadline plus a transport retry on every
    student's reply.

    Not a clock assertion, for the reason the enqueue tripwire gives: a stopwatch is flaky on a loaded
    machine and cold-start alone can cost seconds. "Did a model get called" is the property, and it is
    also strictly stronger — a read that is fast today is still a read that can hang tomorrow.

    BRUCE_ROUTER_SEMANTIC is cleared deliberately. That flag grants the ROUTER authority to read, which is
    a legitimate model call on the request path; leaving it ambiguous would make a tripwire hit mean two
    different things.
    """
    from sqlalchemy import select

    from bruce_engine import conversation_runtime, schema, semantic_triage
    from bruce_engine.conversation_contract import ConversationDecision, IntentKind, ResponseType, RiskLevel
    from bruce_engine.conversation_model import ReasonResult
    from bruce_engine.db import user_session
    from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage

    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.delenv("BRUCE_ROUTER_SEMANTIC", raising=False)

    reads: list[str] = []

    async def _tripwire(self, body):
        reads.append("read")
        raise RuntimeError("the semantic model was called on the request path")

    monkeypatch.setattr(semantic_triage.SemanticTriage, "read", _tripwire)

    class _Reasoner:
        """The conversation model, faked — it is not the read under test, and a real one would measure
        someone else's network."""
        provider = model = "fake"
        supports_vision = True

        async def decide(self, *, text, images, context):
            return ReasonResult(
                decision=ConversationDecision(
                    intent=IntentKind.casual, response_type=ResponseType.direct_answer,
                    user_visible_response="not much, what's good", extracted_entities=[],
                    required_capabilities=[], needs_mission=False, risk_level=RiskLevel.none,
                    confidence=0.8),
                provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)

    uid = uuid4()

    async def _go():
        async with user_session(uid) as s:
            s.add(schema.User(id=uid, auth_provider="alpha_bridge"))
        msg = InboundMessage(provider_message_id=PMID, channel=ChannelKind.self_hosted_imessage,
                             channel_identity="+15557654321", text="yo what's up", attachments=[],
                             timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)
        out = await conversation_runtime.handle(FakeChannel(), msg, user_id=uid,
                                                reply_target="+15557654321", reasoner=_Reasoner())
        async with user_session(uid) as s:
            replies = (await s.execute(select(schema.OutboundMessageRow).where(
                schema.OutboundMessageRow.user_id == uid))).scalars().all()
            jobs = (await s.execute(select(schema.SemanticShadowJob).where(
                schema.SemanticShadowJob.user_id == uid))).scalars().all()
        return out, replies, jobs

    out, replies, jobs = asyncio.run(_go())

    assert reads == [], (
        "the request path called the semantic model — a slow or failing read now delays or breaks a "
        "student's turn, while 'semantic authority is off' stays technically true")
    assert out.status == "processed" and len(replies) == 1, "the turn did not complete normally"
    assert len(jobs) == 1 and jobs[0].status == semantic_shadow.PENDING, (
        "the turn left no queued observation — the read was either performed inline or lost")
    assert jobs[0].outcome is None, "the request path recorded an outcome, so it did the read itself"




