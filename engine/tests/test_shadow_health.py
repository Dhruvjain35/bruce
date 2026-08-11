"""SHADOW HEALTH IS EMITTED BY A REAL CALLER, AND IT RECONCILES.

`counts()` and `backlog()` were written, documented, tested — and called by nothing in production. A
queue that is measurable in principle and measured nowhere has the same failure mode as the detached task
it replaced: work disappears and the first person to notice is whoever eventually asks why the sample is
small. Slower to discover, identical in effect.

So the worker's `/process` — the wake that just drained the queue — emits the numbers, and this file
holds it to that. Two things are asserted and they are different claims:

  IT IS EMITTED       the worker's own response and log carry the health block, so removing the call
                      fails here rather than going unnoticed for a release
  IT ADDS UP          every turn this process offered lands in exactly one named disposition, and
                      enqueued == pending + leased + retryable + terminal, with anything that fits
                      neither named in `unaccounted` instead of quietly vanishing from every bucket

NEITHER OF THOSE IS RECONCILIATION, and the rename in this file is the correction. The intake counter
used to call its total `eligible` and define it as the sum of its own dispositions, so "eligible ==
enqueued + everything else" was an identity that could not fail — and did not fail while every
continuation and approval turn skipped the call site entirely. What a self-consistent counter proves is
that nothing left the function unnamed. Whether every turn REACHED the function is a different question,
answered against a ledger shadow does not write: see tests/test_shadow_intake_coverage.py.

And "ineligible" is EXPLICIT. A turn skipped because the kill switch is off, or because the provider sent
no message id, is counted under its reason — an absence and a bug produce the same empty table otherwise.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from bruce_engine import semantic_shadow

CHANNEL = "imessage"


class _Decision:
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


class _Reads:
    provider, model = "fake", "reads"

    async def read(self, body):
        from bruce_engine.semantic_contracts import Actionability, SemanticTurn, TurnRole
        return SemanticTurn(turn_role=TurnRole.conversation, actionability=Actionability.no_action,
                            confidence=0.9)


class _Malformed:
    provider, model = "fake", "malformed"

    async def read(self, body):
        return {"not": "a semantic turn"}


# --- the intake ledger -------------------------------------------------------------------------------

def test_every_turn_offered_lands_in_exactly_one_explicit_bucket(monkeypatch):
    """The self-consistency invariant, exercised through all six dispositions.

    Each is reached by the real route rather than by calling `record` directly: a ledger tested by
    incrementing it proves the counter increments.
    """
    tally = semantic_shadow.EnqueueLedger()

    class _Broken:
        async def enqueue(self, *a, **kw):
            raise RuntimeError("the database is on fire")

    async def _go():
        uid = uuid4()
        monkeypatch.delenv("BRUCE_SEMANTIC_SHADOW", raising=False)
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-off",
                                      decision=_Decision(), tally=tally)      # disabled
        monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
        store = semantic_shadow.InMemoryShadowStore()
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=None,
                                      decision=_Decision(), store=store, tally=tally)  # no message id
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-1",
                                      decision=_Decision(), store=store, tally=tally)  # enqueued
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-1",
                                      decision=_Decision(), store=store, tally=tally)  # duplicate
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-2",
                                      decision=_Decision(), store=_Broken(), tally=tally)  # failed
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-3",
                                      decision=_Decision(), store=store, tally=tally,
                                      disposition=semantic_shadow.EXCLUDED_LINK_PROTOCOL)  # excluded

    asyncio.run(_go())
    intake = tally.as_json()

    assert intake["offered"] == 6, "a turn offered for observation was not counted at all"
    assert intake["enqueued"] == 1
    assert intake["duplicate"] == 1
    assert intake["ineligible_disabled"] == 1, (
        "a turn skipped because the switch was off was not accounted for — 'shadow is off' and 'shadow "
        "is on and losing turns' then produce the same empty table")
    assert intake["ineligible_no_message_id"] == 1
    assert intake["enqueue_failed"] == 1
    assert intake["excluded_link_protocol"] == 1, (
        "an excluded turn was not accounted under its own reason — an exclusion that is not named is "
        "indistinguishable from a turn that was silently dropped, which is the whole failure here")
    assert intake["self_consistent"] is True and intake["unaccounted"] == 0


# --- the queue ledger --------------------------------------------------------------------------------

def test_the_queue_numbers_reconcile_and_name_every_outcome_class(monkeypatch):
    """The second invariant, over a queue holding one of everything.

    The bucket names are the emission contract: `completed`, `malformed`, `model_failures`,
    `infrastructure_failures` and `exhausted` must each be their own number, because they have entirely
    different owners — an unusable answer is an output-contract fix, a lost packet is infrastructure, and
    a job that never got read at all is neither.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    tally = semantic_shadow.EnqueueLedger()

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        for i in range(4):
            uid = uuid4()
            store.add_turn(uid, channel=CHANNEL, provider_message_id=f"pm-{i}", text="hey")
            await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=f"pm-{i}",
                                          decision=_Decision(), store=store, tally=tally)
        await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Reads())
        await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Malformed())
        return await semantic_shadow.health(store, tally=tally)

    health = asyncio.run(_go())
    q, intake = health["queue"], health["intake"]

    assert intake["offered"] == 4 and intake["enqueued"] == 4 and intake["self_consistent"] is True
    assert q["enqueued"] == 4, "the durable row count disagrees with what was enqueued"
    assert q["completed"] == 1 and q["malformed"] == 1
    assert q["pending"] == 2 and q["leased"] == 0 and q["retryable"] == 0
    assert q["terminal"] == 2, "completed + terminal_failed is not the terminal count"
    assert q["enqueued"] == q["pending"] + q["leased"] + q["retryable"] + q["terminal"], (
        "the queue does not add up — work that went missing between states would be invisible")
    assert q["reconciled"] is True and q["unaccounted"] == 0
    for bucket in ("model_failures", "infrastructure_failures", "exhausted", "turn_missing",
                   "unclassified_outcome"):
        assert bucket in q, f"{bucket} is not emitted, so nothing can alert on it"


def test_an_outcome_no_bucket_claims_is_emitted_under_its_own_name(monkeypatch):
    """`unclassified_outcome` was computed and then dropped.

    `_tally` routed any outcome outside HEALTH_BUCKETS into it, and `health()` then built its emission
    from a hand-written list of keys that did not include it. So a new or misspelled outcome vanished
    from the published numbers entirely — the same silent-loss shape as the rest of this queue's history,
    one level up in the telemetry that is supposed to notice silent loss.

    Reached by putting a real row in an outcome nothing classifies, which is what a half-deployed rename
    looks like, rather than by calling `_tally` with a made-up tuple.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        uid = uuid4()
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-u",
                                     decision=_Decision(), store=store,
                                     tally=semantic_shadow.EnqueueLedger())
        row = next(iter(store.jobs.values()))
        row.status, row.outcome = semantic_shadow.TERMINAL_FAILED, "outcome_from_a_newer_deploy"
        return await semantic_shadow.health(store, tally=semantic_shadow.EnqueueLedger())

    q = asyncio.run(_go())["queue"]
    assert q["unclassified_outcome"] == 1, (
        "an outcome no bucket claims was counted nowhere in the emission — the one number that says "
        "'this queue holds something I do not understand' was itself unpublished")
    assert q["completed"] == 0 and q["malformed"] == 0, "it was quietly folded into a real bucket"


def test_a_status_outside_the_vocabulary_is_named_rather_than_lost(monkeypatch):
    """The reconciliation must FAIL LOUDLY when it should fail.

    A test that only ever sees a reconciling queue cannot tell a real invariant from `return True`. A row
    in a status this module does not know about — a half-applied migration, a hand-edited row — has to
    surface as `unaccounted`, which is the whole reason the check counts statuses instead of trusting
    that the buckets are exhaustive.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        uid = uuid4()
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-x",
                                      decision=_Decision(), store=store,
                                      tally=semantic_shadow.EnqueueLedger())
        next(iter(store.jobs.values())).status = "half_migrated"
        return await semantic_shadow.counts(store)

    q = asyncio.run(_go())
    assert q["unaccounted"] == 1 and q["reconciled"] is False, (
        "a row in an unknown status vanished from every bucket while the totals still looked fine")


def test_the_ages_say_whether_the_drain_is_running(monkeypatch):
    """`oldest_pending_age_s` and `oldest_leased_age_s` are the two numbers that separate a busy queue
    from a stopped one, and they measure different things: how long a turn has waited to be read at all,
    versus how long ONE claim has been held. A worker stuck mid-read and a drain that never wakes need
    different fixes, and a single 'oldest job' number cannot tell them apart."""
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    import datetime

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        for i in range(2):
            uid = uuid4()
            store.add_turn(uid, channel=CHANNEL, provider_message_id=f"pm-{i}", text="hey")
            await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=f"pm-{i}",
                                          decision=_Decision(), store=store,
                                          tally=semantic_shadow.EnqueueLedger())
        claimed = await store.claim("w1")
        # Look at the queue from 10 minutes later. The rows are not touched; only the clock moves.
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
        return claimed, await store.counts(now=later)

    claimed, q = asyncio.run(_go())
    assert claimed is not None
    assert q["leased"] == 1 and q["pending"] == 1
    assert q["oldest_leased_age_s"] >= 600, "a lease held for ten minutes reads as fresh"
    assert q["oldest_pending_age_s"] >= 600, "a turn waiting ten minutes reads as fresh"


def test_the_health_block_reconciles_the_ledgers_own_population(monkeypatch):
    """The third scope, and the only one that can catch a turn that never reached intake.

    `intake` and `queue` are shadow marking its own homework: both are counted from rows shadow wrote.
    `turns` counts the canonical inbound-turn ledger, which shadow does not write, so a turn with no
    disposition anywhere is a GAP instead of an invariant agreeing with itself.

    AND THE USER LIST IS NOT AN ARGUMENT ANY MORE — AND IS NOT A RETURN VALUE EITHER. It used to be
    `health(reconcile_users=...)`, filled by the worker from the jobs the drain had just returned, so a
    user whose intake call was missing had no job, was never in the list, and was never checked. Then it
    became the aggregate function's per-owner rows, which the worker looped to finish the join. `health()`
    now takes no population and receives none: the comparison happens inside the definer and what comes
    back is counts. That is why this test passes NO user id and asserts on no user count.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    uid = uuid4()

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        # Two trusted turns on the ledger; only one of them is ever offered for observation.
        seen = store.add_turn(uid, channel=CHANNEL, provider_message_id="pm-a", text="hey")
        store.add_turn(uid, channel=CHANNEL, provider_message_id="pm-b", text="and this one")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id="pm-a",
                                     decision=_Decision(), store=store, conversation_turn_id=seen,
                                     tally=semantic_shadow.EnqueueLedger())
        return await semantic_shadow.health(store, tally=semantic_shadow.EnqueueLedger())

    turns = asyncio.run(_go())["turns"]

    assert turns["canonical_trusted_turns"] == 2
    assert turns["eligible_turns"] == 1 and turns["explicitly_excluded_turns"] == 0
    assert turns["trusted_turns_without_intake"] == 1
    assert turns["unobserved_turns"] == 1, "a turn that never reached intake was not reported as a gap"
    assert "checked_users" not in turns and "users" not in turns, (
        "the emission still carries a user count. Nothing out here is supposed to know how many owners "
        "are in the sample — knowing that is one query away from knowing which")
    assert turns["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "the emission said the turn ledger reconciled while a trusted turn had no disposition at all — "
        "which is the exact state the whole continuation population was in, undetected")


# --- the one function that PUBLISHES a reconciliation status -------------------------------------------
#
# The status used to be an expression at the site that published it:
#   `clean if unobserved == 0 and unreconciled == 0 else failed`
# Both of those are SUMS OVER THE POPULATION, and a sum over an empty population is zero — so a pass that
# checked NOBODY reported clean while the canonical ledger held turns. Then the rules moved into
# `reconciliation_verdict`, which was better but still a RE-DERIVATION from summary numbers.
#
# The verdict is now decided inside the aggregate function, in the same statement and the same snapshot
# as the counts, from anti-joins nothing out here can see. So `reconciliation_verdict` publishes rather
# than decides, and what is stated below one case at a time is the boundary between the two: a
# VALIDATION that can only ever refuse a verdict, never invent, upgrade or soften one.


def _verdict(**kw):
    kw.setdefault("ledger_visibility_status", semantic_shadow.LEDGER_PROVEN)
    kw.setdefault("database_status", semantic_shadow.RECON_CLEAN)
    return semantic_shadow.reconciliation_verdict(**kw)


def test_an_unproven_ledger_is_unknown_with_no_counts_at_all():
    """RULE 1, and it comes first for a reason: the counts mean nothing until the read is proven, and a
    fabricated zero says the sample is complete precisely when nobody can tell. The database's verdict is
    discarded here too — a `clean` obtained without the authority to look is not evidence of anything."""
    out = _verdict(ledger_visibility_status=semantic_shadow.LEDGER_UNPROVEN,
                   database_status=semantic_shadow.RECON_CLEAN,
                   canonical_trusted_turns=9, unobserved_turns=9)
    assert out["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN
    assert out["reconciliation_status"] != semantic_shadow.RECON_CLEAN
    for key in semantic_shadow.DEPENDENT_COUNTS:
        assert out[key] is None, f"{key} survived an unproven read as {out[key]!r}"


def test_the_database_verdict_is_carried_through_verbatim():
    """RULE 3, and it is the whole posture. Whatever the function decided is what gets published, with
    the counts it decided it beside."""
    for status in (semantic_shadow.RECON_CLEAN, semantic_shadow.RECON_FAILED):
        out = _verdict(database_status=status, canonical_trusted_turns=5, eligible_turns=4,
                       explicitly_excluded_turns=1, unobserved_turns=0)
        assert out["reconciliation_status"] == status
        assert out["canonical_trusted_turns"] == 5
        assert out["eligible_turns"] == 4 and out["explicitly_excluded_turns"] == 1


def test_a_failed_verdict_is_not_softened_by_counts_that_look_clean():
    """THE DIRECTION A WELL-MEANING FIX-UP WOULD TAKE, and the reason passthrough is stated as a rule.

    Every count here reconciles: the canonical total equals eligible plus excluded, and nothing is
    missing intake. The database still said FAILED, because it saw an anti-join these numbers cannot
    express. Re-deriving from the summary is strictly worse information; publishing it would be this
    function deciding again, from the weaker numbers, which is the thing that has gone wrong three times.
    """
    out = _verdict(database_status=semantic_shadow.RECON_FAILED,
                   canonical_trusted_turns=4, trusted_turns_with_intake=4,
                   trusted_turns_without_intake=0, eligible_turns=3, explicitly_excluded_turns=1,
                   unobserved_turns=1)
    assert out["reconciliation_status"] == semantic_shadow.RECON_FAILED, (
        "a database FAILED was talked back down to clean by counts that happen to balance")


def test_a_verdict_outside_the_vocabulary_is_unknown_and_never_clean():
    """RULE 2. `unknown`, a NULL, a typo, or a value some future migration introduced that this build
    does not understand: none of them is a verdict this process may publish, and none of them may be
    completed into one from the counts sitting beside it."""
    for status in (semantic_shadow.RECON_UNKNOWN, None, "", "reconciled", "CLEAN"):
        out = _verdict(database_status=status, canonical_trusted_turns=4,
                       trusted_turns_without_intake=0, eligible_turns=4, unobserved_turns=0)
        assert out["reconciliation_status"] == semantic_shadow.RECON_UNKNOWN, (
            f"the database answered {status!r} and the publisher worked out a verdict for itself")
        assert out["reconciliation_status"] != semantic_shadow.RECON_CLEAN
        for key in semantic_shadow.DEPENDENT_COUNTS:
            assert out[key] is None, f"{key} was published beside an unreadable verdict"


def test_every_count_the_database_returns_is_published():
    """The whole tuple travels, and it travels by ONE name. `UNCLASSIFIED_OUTCOME` was computed in the
    tally and then dropped by a hand-written key list at the emission site, so a bucket existed and was
    never emitted; a count that survives in SQL and vanishes in Python is the same failure."""
    out = _verdict(**{name: i + 1 for i, name in enumerate(semantic_shadow.RECONCILIATION_COUNTS)})
    for i, name in enumerate(semantic_shadow.RECONCILIATION_COUNTS):
        assert out[name] == i + 1, f"{name} was computed by the database and dropped on the way out"


# --- the production caller ---------------------------------------------------------------------------

def test_the_worker_wake_emits_shadow_health_and_sweeps_exhausted_jobs(monkeypatch, caplog):
    """The wiring itself. `/process` must call the measurement, not merely be able to.

    Everything else on the wake is stubbed out — this is about the shadow block, and a real intake or
    mission drain would make the test about someone else's queue. The stubs are the seams `/process`
    already uses, so removing the health call still fails here.
    """
    from bruce_engine import worker_api

    async def _no_intake(*a, **kw):
        return False

    async def _observe(**kw):
        return None

    async def _sweep(*a, **kw):
        return 2

    async def _backlog(*a, **kw):
        return 11

    async def _health(*a, **kw):
        return {"intake": {"offered": 7, "enqueued": 7, "self_consistent": True},
                "queue": {"pending": 1, "leased": 0, "exhausted": 2, "reconciled": True},
                "turns": {"users": 0, "unobserved": 0, "reconciled": True}}

    monkeypatch.setattr(worker_api.worker, "process_one", _no_intake)
    monkeypatch.setattr(worker_api, "background_runner_enabled", lambda: False)
    monkeypatch.setattr(worker_api.semantic_shadow, "claim_and_observe", _observe)
    monkeypatch.setattr(worker_api.semantic_shadow, "sweep_exhausted", _sweep)
    monkeypatch.setattr(worker_api.semantic_shadow, "backlog", _backlog)
    monkeypatch.setattr(worker_api.semantic_shadow, "health", _health)

    with caplog.at_level(logging.INFO, logger="bruce_engine.worker_api"):
        out = asyncio.run(worker_api.process())

    assert out["shadow_exhausted"] == 2, "the wake did not close out exhausted jobs"
    assert out["shadow_health"]["queue"]["exhausted"] == 2, (
        "the worker drained the shadow queue and reported nothing about its health — counts() and "
        "backlog() with no production caller is how a dropped observation stays invisible")
    assert out["shadow_backlog_before"] == 11, (
        "backlog() still has no production caller. The backlog the wake INHERITED is what says whether "
        "the drain keeps up; the queue it leaves behind cannot distinguish a drain that is working from "
        "one that is not running at all")
    assert out["shadow_errors"] == 0, "a stubbed-out wake reported an error"
    assert any("shadow_health" in r.getMessage() for r in caplog.records), (
        "shadow health is not emitted on the log path, so nothing off-box can see it")


def test_a_failing_health_read_is_reported_not_reported_as_zero(monkeypatch):
    """"The queue is empty" and "I could not read the queue" must never look the same.

    That confusion is the original sin this outbox exists to correct, and a metric that returns zeros on
    failure re-creates it one layer up — the dashboard would look healthiest exactly when the database is
    unreachable.
    """
    from bruce_engine import worker_api

    async def _no_intake(*a, **kw):
        return False

    async def _observe(**kw):
        return None

    async def _sweep(*a, **kw):
        return 0

    async def _backlog(*a, **kw):
        return 0

    async def _boom(*a, **kw):
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr(worker_api.worker, "process_one", _no_intake)
    monkeypatch.setattr(worker_api, "background_runner_enabled", lambda: False)
    monkeypatch.setattr(worker_api.semantic_shadow, "claim_and_observe", _observe)
    monkeypatch.setattr(worker_api.semantic_shadow, "sweep_exhausted", _sweep)
    monkeypatch.setattr(worker_api.semantic_shadow, "backlog", _backlog)
    monkeypatch.setattr(worker_api.semantic_shadow, "health", _boom)

    out = asyncio.run(worker_api.process())
    assert out["shadow_errors"] == 1, "a failed health read was swallowed into a clean-looking response"
    assert out["shadow_health"] == {}, "a failed health read reported numbers it never obtained"
