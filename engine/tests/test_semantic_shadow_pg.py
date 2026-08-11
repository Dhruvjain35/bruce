"""ONE SHADOW JOB PER TURN, proved against real Postgres — the constraint, not a stand-in for it.

`test_shadow_isolation` asserts this property through `InMemoryShadowStore`, whose uniqueness is a dict
lookup someone wrote by hand. That proves the fake is idempotent. It cannot prove the shipped path is,
and the two are enforced by completely different machinery: in production the guarantee is
UNIQUE(user_id, channel, provider_message_id) plus `ON CONFLICT ... DO NOTHING`, and deleting BOTH of
those left the whole offline suite green.

Why it has to be the database. A duplicate shadow job is not a duplicate row, it is a duplicate
OBSERVATION: the same turn read twice, counted twice in the agreement rate that decides whether the
executive may be given authority. Whichever turns the provider happened to redeliver would carry double
weight. And a caller-side "have I already queued this?" check cannot prevent it — two concurrent
deliveries of one webhook both pass the SELECT before either INSERTs, which is why the concurrency case
below is a test and not a comment.

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
from bruce_engine.db import worker_session
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

CHANNEL = "imessage"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    yield
    db._engine = None; db._sessionmaker = None


class _Decision:
    """The router decision snapshotted onto the job. Shape-compatible with RouterDecision."""
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


def _run(c):
    return asyncio.run(c)


async def _enqueue(uid: UUID, pmid: str,
                   tally: semantic_shadow.EnqueueLedger | None = None) -> bool:
    """The real request path, on the real store — no `store=` override, deliberately."""
    return await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=pmid,
                                         decision=_Decision(), tally=tally)


async def _rows(uid: UUID, pmid: str) -> int:
    """How many jobs the database actually holds for one turn. Counted under a worker session because
    the point of some of these tests is what exists ACROSS owners, which an owner's session cannot see."""
    async with worker_session() as s:
        return int((await s.execute(
            sa_text(f"SELECT count(*) FROM {semantic_shadow.TABLE} "
                    "WHERE user_id = :uid AND channel = :ch AND provider_message_id = :pmid"),
            {"uid": str(uid), "ch": CHANNEL, "pmid": pmid})).scalar())


def test_a_redelivered_turn_produces_exactly_one_shadow_job(clean_db, caplog):
    """The ordinary case: a provider delivers the same message id twice.

    Asserted on the ROW COUNT rather than on `enqueue`'s return value, because the return value can be
    False for two very different reasons — "already queued" and "the insert blew up and was swallowed" —
    and only one of those is this property holding.

    The log assertion separates those two reasons, and it is not decoration. `enqueue` swallows every
    exception, so if idempotency were left to the constraint ALONE — no `ON CONFLICT` — the row count
    would still be 1 and every routine redelivery would be logged as a failed enqueue. The one telemetry
    signal that is supposed to say observations are being lost would then fire loudest on the case where
    nothing is wrong.
    """
    import logging

    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    first = _run(_enqueue(uid, "pm-redelivered"))
    with caplog.at_level(logging.INFO, logger="bruce.semantic_shadow"):
        second = _run(_enqueue(uid, "pm-redelivered"))
    assert first is True and second is False
    assert _run(_rows(uid, "pm-redelivered")) == 1, "one turn was queued for observation twice"
    assert not [r for r in caplog.records if "shadow_enqueue_failed" in r.getMessage()], (
        "a redelivery was reported as a failed enqueue — a duplicate turn is a normal event, and "
        "logging it as a failure hides the real dropped observations it is meant to surface")


def test_two_concurrent_deliveries_of_one_turn_produce_one_job(clean_db):
    """The race a caller-side check cannot win.

    Two webhook deliveries land at once; both would pass a SELECT-then-INSERT guard before either
    committed. Only the unique constraint can decide this, so this is the test that says the guarantee
    lives in the database.
    """
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))

    async def _both():
        return await asyncio.gather(_enqueue(uid, "pm-raced"), _enqueue(uid, "pm-raced"))

    created = _run(_both())
    assert _run(_rows(uid, "pm-raced")) == 1, "a concurrent redelivery became a second observation"
    assert sum(bool(c) for c in created) == 1, (
        "both concurrent deliveries reported creating a job — the winner is ambiguous")


def test_a_redelivery_is_counted_as_a_duplicate_and_never_as_a_failed_enqueue(clean_db):
    """The two halves of the guarantee, separated — because the ROW COUNT alone cannot tell them apart.

    Delete `ON CONFLICT ... DO NOTHING` and keep the constraint: the second INSERT raises, `enqueue`
    swallows it (telemetry may never break a student's turn), and the table still holds exactly one row.
    Every row-count assertion in this file still passes. What changes is the disposition — the turn is
    filed as `enqueue_failed` instead of `duplicate` — and `enqueue_failed` is THE number that is supposed
    to mean observations are being lost. Routine webhook redelivery would drive it forever, and the one
    signal that says the sample is incomplete would be permanently on.

    Delete the UNIQUE constraint instead and the same assertion fails from the other side: two rows, two
    `enqueued`, and one turn counted twice in the agreement rate that decides whether the executive may
    be given authority.

    Asserted on the ledger rather than on a log line because this is the accounting the health emission
    publishes, and `self_consistent` says no turn left `intake` unnamed. It says nothing about whether
    every turn REACHED `intake` — that claim belongs to `reconcile`, against the turn ledger.
    """
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    tally = semantic_shadow.EnqueueLedger()   # a fresh ledger: the module-global one spans the process

    assert _run(_enqueue(uid, "pm-ledger", tally)) is True
    assert _run(_enqueue(uid, "pm-ledger", tally)) is False

    assert _run(_rows(uid, "pm-ledger")) == 1, "one turn was queued for observation twice"
    assert tally.as_json() == {
        "offered": 2, "enqueued": 1, "duplicate": 1, "ineligible_disabled": 0,
        "ineligible_no_message_id": 0, "enqueue_failed": 0,
        "excluded_no_trusted_text": 0, "excluded_untrusted_only": 0, "excluded_link_protocol": 0,
        "excluded_platform_command": 0,
        "unaccounted": 0, "self_consistent": True}, (
        "a redelivered turn was not accounted as a duplicate — either the constraint let it through as a "
        "second observation, or the insert failed and was counted as a lost one")


def test_distinct_turns_are_still_queued_separately(clean_db):
    """The other half of the constraint, and the reason it is keyed on three columns.

    A guarantee that is too BROAD loses turns instead of duplicating them, which is the same bias
    wearing the opposite sign: two students sharing a provider message id, or one student's next
    message, must each get their own observation.
    """
    a, b = uuid4(), uuid4()
    _run(users.ensure(a, auth_provider="test")); _run(users.ensure(b, auth_provider="test"))

    assert _run(_enqueue(a, "pm-shared")) is True
    assert _run(_enqueue(b, "pm-shared")) is True, "another owner's turn collided with this one"
    assert _run(_enqueue(a, "pm-next")) is True, "the student's next message was swallowed as a duplicate"

    assert _run(_rows(a, "pm-shared")) == 1
    assert _run(_rows(b, "pm-shared")) == 1
    assert _run(_rows(a, "pm-next")) == 1
