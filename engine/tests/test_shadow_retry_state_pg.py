"""THE RETRY, AS THE DATABASE ACTUALLY HOLDS IT — status, attempts, lease, and nothing recorded.

`test_shadow_isolation.py` and `test_shadow_retry_durability.py` both ask what the retry path RETURNS: a
label of `retryable_failed`, and (in memory) that a later claim comes back. Neither of them looks at the
row. Deleting the whole `mark_retryable` branch out of `claim_and_observe` — so a dropped packet is
finalised by the same `record` write every other outcome takes — left both of them green, because
`status_for(TRANSPORT, can_retry=True)` returns `retryable_failed` too. The LABEL is identical. The ROW is
not: `record` stamps `observed_at`, and it NULLs the lease, and `claim` will only take a
`retryable_failed` row back when `lease_expires_at IS NOT NULL AND lease_expires_at < now()`. So the job
sits at `retryable_failed` with a NULL lease forever — never claimed, never completed, never terminal,
counted as `queued` for the life of the table, and carrying a timestamp that says it was observed.

That is the ORIGINAL SAMPLING BIAS wearing a different hat. The detached-task version lost the slow and
failing reads because they were the ones still in flight at shutdown; this loses them because a transport
blip is what a slow or overloaded provider produces most. Either way the sample that decides whether the
executive may be given authority is missing exactly the turns that would argue against it.

So every assertion here is on columns read back out of Postgres, and the in-memory store is not involved:
the retry state is written by SQL, and only SQL can be asked whether it was written.

WHY THE BACKOFF DEADLINE IS ASSERTED AS `lease_expires_at`. There is no `next_attempt_at` column; the
lease deadline IS the next-attempt gate (see `claim`), which is why the retry write REPLACES the claim's
60s lease with a 30s backoff rather than leaving it alone — a retry that inherited the claim's lease
would be held past its own delay by the failure that caused it.

Skips cleanly when Postgres isn't configured, via `pg_test_db`.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import semantic_shadow
from bruce_engine.db import user_session, worker_session
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

CHANNEL, PMID = "imessage", "pm-retry-state"
TEXT = "put lunch with sam on friday"

# The claim's lease, stated rather than defaulted, because one assertion below is that the retry write
# MOVED the deadline: 30s of backoff must replace 60s of claim, not sit behind it.
CLAIM_LEASE_S = 60
BACKOFF_S = semantic_shadow.DEFAULT_RETRY_BACKOFF_SECONDS


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    yield
    db._engine = None; db._sessionmaker = None


class _Decision:
    """Shape-compatible with RouterDecision — the snapshot, not the subject."""
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


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


def _run(c):
    return asyncio.run(c)


async def _seed_turn(uid: UUID) -> None:
    """The student's words, where they actually live. The job row only references them."""
    async with user_session(uid) as s:
        await s.execute(sa_text(
            "INSERT INTO conversation_turns (user_id, channel, channel_identity, provider_message_id, "
            "role, text) VALUES (:uid, :ch, '+15550001111', :pmid, 'user', :t)"),
            {"uid": str(uid), "ch": CHANNEL, "pmid": PMID, "t": TEXT})


async def _row(uid: UUID) -> dict:
    """The whole job row, worker-side, so the assertions are about what Postgres holds."""
    async with worker_session() as s:
        return dict((await s.execute(sa_text(
            "SELECT status, outcome, attempts, last_error, observation, agrees, divergence, "
            "latency_ms, observed_at, lease_owner, lease_token, lease_expires_at, "
            "EXTRACT(EPOCH FROM (lease_expires_at - now())) AS lease_in_s "
            f"FROM {semantic_shadow.TABLE} WHERE user_id = :uid"), {"uid": str(uid)})).mappings().one())


async def _rows(uid: UUID) -> int:
    async with worker_session() as s:
        return int((await s.execute(sa_text(
            f"SELECT count(*) FROM {semantic_shadow.TABLE} WHERE user_id = :uid"),
            {"uid": str(uid)})).scalar())


async def _age_out_the_backoff(uid: UUID) -> None:
    """Advance the CLOCK past the delay the retry path wrote — never write the delay.

    Strictly `lease_expires_at - interval`: it moves whatever the code under test put there and invents
    nothing. If the retry write is deleted the column is NULL, the arithmetic yields NULL, and the row
    stays unclaimable — which is the failure this file is here to see, not something this helper can
    paper over. Sleeping 30s instead would only make the suite slower.
    """
    async with worker_session() as s:
        await s.execute(sa_text(
            f"UPDATE {semantic_shadow.TABLE} SET lease_expires_at = "
            "lease_expires_at - make_interval(secs => :s) WHERE user_id = :uid"),
            {"s": BACKOFF_S + 1, "uid": str(uid)})


def test_a_transport_failure_leaves_a_retryable_row_and_is_eventually_observed_once(clean_db):
    """One turn, one dropped packet, one recovery — and every intermediate column checked.

    The three moments that matter, and the row is read at each:

      after the drop   `retryable_failed`, one attempt spent, the backoff deadline in the future, and NO
                       terminal result: no observation, no agreement, no latency, no `observed_at`. A row
                       that carries an observed timestamp for a read that never happened is a completed
                       measurement of nothing.
      during backoff   the worker does NOT get it back. A backoff that does not hold is a hot loop
                       against a provider that is already failing.
      after backoff    the WORKER reclaims it with no help from this test, records ONE observation, and
                       the lease is cleared.
    """
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))

    async def _go():
        await _seed_turn(uid)
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), tally=semantic_shadow.EnqueueLedger())
        dropped = await semantic_shadow.claim_and_observe(
            worker_id="w1", lease_seconds=CLAIM_LEASE_S, triage=_Drops())
        owed_row, owed_counts = await _row(uid), await semantic_shadow.counts()

        # The backoff is still running: this claim must find nothing to do.
        too_soon = await semantic_shadow.claim_and_observe(
            worker_id="w2", lease_seconds=CLAIM_LEASE_S, triage=_Reads())

        await _age_out_the_backoff(uid)
        retried = await semantic_shadow.claim_and_observe(
            worker_id="w3", lease_seconds=CLAIM_LEASE_S, triage=_Reads())
        return (dropped, owed_row, owed_counts, too_soon, retried,
                await _row(uid), await _rows(uid), await semantic_shadow.counts())

    dropped, owed, owed_counts, too_soon, retried, final, n_rows, counts = _run(_go())

    # --- the drop was classified as infrastructure, which is what makes it retryable at all ---------
    assert dropped is not None and dropped.outcome == semantic_shadow.TRANSPORT, (
        "a dropped packet was not classified as transport, so this test is not exercising the retry path")
    assert dropped.recorded is True, "the retry write was fenced out, so the row below proves nothing"

    # --- STATUS and ATTEMPTS ------------------------------------------------------------------------
    assert owed["status"] == semantic_shadow.RETRYABLE_FAILED, (
        f"a transport failure left the row at {owed['status']!r}")
    assert owed["status"] not in (semantic_shadow.COMPLETED, semantic_shadow.TERMINAL_FAILED), (
        "an infrastructure failure was finalised — the turn is now permanently unobserved")
    assert owed["attempts"] == 1, (
        "the attempt was not counted on the claim, so a worker that dies mid-read costs nothing and the "
        "retry bound never converges")
    assert owed["outcome"] == semantic_shadow.TRANSPORT and owed["last_error"] == semantic_shadow.TRANSPORT, (
        "a job stuck retrying transport failures must SAY so mid-retry; a NULL outcome makes it look idle")

    # --- NO TERMINAL RESULT RECORDED ----------------------------------------------------------------
    assert owed["observation"] is None, "a failed read wrote an observation"
    assert owed["agrees"] is None and owed["divergence"] is None, (
        "a read that never happened was counted in the agreement rate the authority decision rests on")
    assert owed["latency_ms"] is None, "a read that never happened contributed a latency"
    assert owed["observed_at"] is None, (
        "the row is stamped as observed while it is still owed — this is the exact column that says the "
        "retry took the terminal `record` write instead of the retry write")

    # --- THE LEASE: released to a backoff deadline, not held at the claim's ---------------------------
    assert owed["lease_expires_at"] is not None, (
        "the lease was cleared outright, so `claim` can never take this row back: it requires "
        "lease_expires_at IS NOT NULL AND lease_expires_at < now(). One lost packet, one lost observation")
    assert 0 < owed["lease_in_s"] <= BACKOFF_S + 1, (
        f"the next attempt is not gated at ~{BACKOFF_S}s: {owed['lease_in_s']}s")
    assert owed["lease_in_s"] < CLAIM_LEASE_S, (
        "the retry inherited the CLAIM's lease instead of writing its own backoff — the job would be held "
        "past its delay by the failure that caused it")
    assert owed_counts["queued"] == 1 and owed_counts["retryable"] == 1, (
        "a transport failure must leave the turn OWED — neither completed nor abandoned")

    # --- the backoff HOLDS ---------------------------------------------------------------------------
    assert too_soon is None, (
        "the worker re-claimed the job before its backoff elapsed, which is a hot loop against a provider "
        "that is already failing")

    # --- the turn comes BACK, and lands exactly once --------------------------------------------------
    assert retried is not None, (
        "the worker never got the job back: the row was left in a state `claim` cannot select, so the "
        "observation is lost while still counting as queued forever")
    assert retried.outcome == semantic_shadow.OK and retried.recorded is True
    assert n_rows == 1, "the retry inserted a second job instead of converging onto the claimed row"
    assert final["status"] == semantic_shadow.COMPLETED and final["outcome"] == semantic_shadow.OK
    assert final["attempts"] == 2, "the second claim did not spend an attempt"
    assert final["observation"] is not None and final["observed_at"] is not None
    assert final["agrees"] is not None, "the completed observation recorded no comparison"
    assert final["last_error"] is None, "a successful observation kept the failed attempt's error"
    assert (final["lease_owner"] is None and final["lease_token"] is None
            and final["lease_expires_at"] is None), (
        "a finished job is still holding a lease, so an expired-lease sweep can reclaim a completed turn")
    assert counts["completed"] == 1 and counts["queued"] == 0, "the backlog never drained"
    assert counts["outcomes"] == {semantic_shadow.OK: 1}, (
        "one turn produced more than one outcome across its attempts — the retry did not converge onto "
        "a single record, so this turn carries double weight in the sample")


# --- the retry bound as the DATABASE holds it ---------------------------------------------------------

def test_a_job_can_never_be_written_with_a_retry_bound_of_zero(clean_db):
    """SILENT TOTAL LOSS, made unrepresentable.

    `claim` takes a row only while `attempts < max_attempts`. At a bound of 0 that is false the instant
    the row is written, so a brand-new job is NEVER claimable: every observation is lost, the queue drains
    to an empty backlog, `counts()` looks healthy, and not one error is raised anywhere. It is the exact
    failure shape this whole outbox exists to make impossible, arriving through a column default.

    So it is a CHECK, not a convention. The insert below is the one a bad default (or a careless caller)
    would produce, and the database refuses it.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    _run(_seed_turn(uid))

    async def _write(bound: int):
        async with user_session(uid) as s:
            await s.execute(sa_text(
                f"INSERT INTO {semantic_shadow.TABLE} (user_id, channel, provider_message_id, "
                "max_attempts) VALUES (:uid, :ch, :pmid, :n)"),
                {"uid": str(uid), "ch": CHANNEL, "pmid": f"pm-bound-{bound}", "n": bound})

    for bound in (0, -1):
        with pytest.raises(Exception) as exc:
            _run(_write(bound))
        assert "max_attempts" in str(exc.value), (
            f"a shadow job with max_attempts={bound} was accepted. Nothing will ever claim it, nothing "
            f"will ever error, and the observation is gone: {exc.value}")

    # and the DEFAULT — what a caller that says nothing gets — leaves the job claimable.
    async def _default_row():
        async with user_session(uid) as s:
            await s.execute(sa_text(
                f"INSERT INTO {semantic_shadow.TABLE} (user_id, channel, provider_message_id) "
                "VALUES (:uid, :ch, 'pm-default')"), {"uid": str(uid), "ch": CHANNEL})
        async with worker_session() as s:
            return dict((await s.execute(sa_text(
                f"SELECT attempts, max_attempts FROM {semantic_shadow.TABLE} "
                "WHERE provider_message_id = 'pm-default'"))).mappings().first())

    row = _run(_default_row())
    assert row["max_attempts"] > row["attempts"], (
        f"a job written with only the defaults is already exhausted ({row!r}) — every fresh observation "
        "would be dropped before anything looked at it")
