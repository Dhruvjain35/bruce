"""THE RETRY PATH ITSELF — a transport failure must come BACK, not merely be labelled retryable.

`test_shadow_isolation.py` already proves that a transport failure is the only outcome allowed to retry,
and that a retried job converges to one record. Both of those are about the LABEL. Neither of them asks
the question this file exists for: does the worker actually leave the row in a state the worker can claim
again?

Found by mutation. Deleting the entire `mark_retryable` branch out of `claim_and_observe` — so a dropped
packet is finalised by the same write every other outcome takes — left the whole suite green. The row it
produces is stuck: `record` clears the lease, `claim` will only take a `retryable_failed` row back when
`lease_expires_at IS NOT NULL AND lease_expires_at < now()`, so the job sits at `retryable_failed` with a
NULL lease forever. Never claimed, never completed, never terminal, and counted as `queued` for the rest
of the table's life. One lost packet permanently loses the observation.

That is the ORIGINAL BIAS wearing a different hat. The detached-task version dropped the slow and failing
reads because they were the ones still in flight when the container went away; this drops them because a
transport blip is the failure a slow or overloaded provider produces most. Either way the sample that
decides whether the executive may be given authority loses exactly the turns that would argue against it.

The existing test could not see it because it assigns `row.lease_expires_at` itself before the second
claim — which hands the row the very state the retry path is supposed to have written. So this file never
touches the lease. It moves the CLOCK instead, and lets the worker's own claim decide.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

from bruce_engine import semantic_shadow

CHANNEL, PMID = "imessage", "pm-retry"


class _Decision:
    """Shape-compatible with RouterDecision — the comparison baseline, not the subject."""
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"
    decision_id = None


class _Drops:
    """A lost packet. Infrastructure, and the one failure that says nothing about the executive."""
    provider, model = "fake", "drops"

    async def read(self, body):
        raise ConnectionError("connection reset")


class _Reads:
    """The read that succeeds once the transport recovers."""
    provider, model = "fake", "reads"

    async def read(self, body):
        from bruce_engine.semantic_contracts import Actionability, SemanticTurn, TurnRole
        return SemanticTurn(turn_role=TurnRole.conversation, actionability=Actionability.no_action,
                            confidence=0.9)


class _LaterStore(semantic_shadow.InMemoryShadowStore):
    """The same store, claimed from a moment further along.

    Time is moved rather than state: a backoff is only real if the WORKER'S OWN claim honours it, and the
    claim decides on the lease the worker left behind. Writing that lease from the test would prove the
    test can write a lease.
    """

    def __init__(self) -> None:
        super().__init__()
        self.skew = datetime.timedelta(0)

    async def claim(self, worker_id, lease_seconds=semantic_shadow.DEFAULT_LEASE_SECONDS, **kw):
        return await super().claim(
            worker_id, lease_seconds,
            now=datetime.datetime.now(datetime.timezone.utc) + self.skew)


def test_a_transport_failure_comes_back_and_is_eventually_observed(monkeypatch):
    """A dropped packet must cost a delay, never the observation.

    Three moments, and the middle one is the one the label-level tests already cover:

      immediately   the job is NOT re-claimable — a backoff that does not hold is a hot loop against a
                    provider that is already failing
      after backoff the WORKER reclaims it, with no help from this test
      afterwards    the turn is observed exactly once, and the backlog returns to zero

    If the middle assertion is the only one that holds, the observation was labelled retryable and then
    stranded, which counts as `queued` forever and quietly re-creates the sampling bias the outbox removes.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    uid = uuid4()

    async def _go():
        store = _LaterStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID, text="put lunch on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), store=store)

        dropped = await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=_Drops())
        owed = await store.counts()
        too_soon = await semantic_shadow.claim_and_observe(worker_id="w2", store=store, triage=_Reads())

        # The backoff elapses. Nothing about the ROW is touched — only when it is looked at.
        store.skew = datetime.timedelta(seconds=semantic_shadow.DEFAULT_RETRY_BACKOFF_SECONDS + 1)
        retried = await semantic_shadow.claim_and_observe(worker_id="w3", store=store, triage=_Reads())
        return dropped, owed, too_soon, retried, await store.counts()

    dropped, owed, too_soon, retried, counts = asyncio.run(_go())

    assert dropped.outcome == semantic_shadow.TRANSPORT
    assert dropped.status == semantic_shadow.RETRYABLE_FAILED
    assert owed["retryable_failed"] == 1 and owed["queued"] == 1, (
        "a transport failure must leave the turn OWED, neither completed nor abandoned")

    assert too_soon is None, "the backoff did not hold — the worker re-claimed the job immediately"

    assert retried is not None, (
        "the worker never got the job back: a transport failure left it at retryable_failed with no "
        "lease, so nothing can ever claim it again and the observation is lost while still counting "
        "as queued")
    assert retried.outcome == semantic_shadow.OK
    assert retried.status == semantic_shadow.COMPLETED
    assert counts["completed"] == 1 and counts["queued"] == 0, (
        "the retried turn did not finish — the backlog never drains")
    assert counts["outcomes"] == {semantic_shadow.OK: 1}, (
        "one turn produced more than one outcome across its attempts")
