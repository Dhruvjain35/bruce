"""ONE SHADOW JOB PER CANONICAL CONVERSATION TURN — the invariant, stated over the thing being counted.

WHAT WAS WRONG. The key was `UNIQUE(user_id, channel, provider_message_id)`: the PROVIDER's metadata,
which identifies a turn only if you assume the provider never reuses a message id across channels. The
invariant the sample actually depends on is one observation per TURN, and the two are not the same
statement. Two consequences were measured:

  THE LOSS BIAS   narrow that key to (user_id, provider_message_id) and the whole 27-test shadow suite
                  stays green while the second channel's turn is silently swallowed as a duplicate. A
                  lost observation is not a neutral error: the turns that get lost are the ones that
                  arrived second, and a sample missing them is a sample nobody chose.
  THE JOIN        reconciliation had to RE-DERIVE the same triple to match a job back to its turn, so
                  the two ledgers were joined on a reconstruction rather than on an identity — which is
                  how a reconciliation ends up agreeing with itself.

So `semantic_shadow_jobs.conversation_turn_id` (migration 0036) is UNIQUE and is the canonical key. The
old triple is kept and demoted to ingress dedupe: it still wins the race between two simultaneous
webhook deliveries, and it still covers rows written before 0036 (whose canonical id is NULL, and NULLs
do not collide). Both are asserted here, and they are asserted to be DIFFERENT constraints — the last
test is the one that fails if the canonical key is deleted and the triple is left to carry the invariant
on its own.

Real Postgres throughout. A dict-lookup fake cannot prove which constraint the database is enforcing,
and this whole file is about which constraint the database is enforcing.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import conversation_store, semantic_shadow
from bruce_engine.db import user_session, worker_session
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

IMESSAGE = "imessage"
SMS = "sms"
IDENTITY = "+15550142"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(
        db, "create_async_engine",
        lambda url, **kw: (kw.pop("poolclass", None),
                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.delenv("BRUCE_ROUTER_SEMANTIC", raising=False)   # authority stays off
    yield
    db._engine = None
    db._sessionmaker = None


class _Decision:
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


def _run(c):
    return asyncio.run(c)


async def _turn(uid: UUID, *, channel: str, pmid: str, text: str) -> UUID:
    """The REAL canonical writer, which is also the thing that hands the id to shadow."""
    return await conversation_store.persist_user_turn(
        uid, channel=channel, channel_identity=IDENTITY, provider_message_id=pmid, text=text)


async def _intake(uid: UUID, *, channel: str, pmid: str, turn_id: UUID | None) -> bool:
    return await semantic_shadow.intake(uid, channel=channel, provider_message_id=pmid,
                                        decision=_Decision(), conversation_turn_id=turn_id,
                                        tally=semantic_shadow.EnqueueLedger())


async def _jobs_for_turn(turn_id: UUID) -> int:
    async with worker_session() as s:
        return int((await s.execute(
            sa_text(f"SELECT count(*) FROM {semantic_shadow.TABLE} WHERE conversation_turn_id = :t"),
            {"t": str(turn_id)})).scalar())


async def _all_jobs(uid: UUID) -> int:
    async with worker_session() as s:
        return int((await s.execute(
            sa_text(f"SELECT count(*) FROM {semantic_shadow.TABLE} WHERE user_id = :u"),
            {"u": str(uid)})).scalar())


async def _turns(uid: UUID) -> int:
    # UNDER THE OWNER'S SESSION, not a worker one. A worker session has no row-level read of
    # `conversation_turns` at all since migration 0037 — the reconciliation counts through an
    # aggregate-only function instead — so a worker-session count here would be a permanent, silent zero.
    async with user_session(uid) as s:
        return int((await s.execute(
            sa_text("SELECT count(*) FROM conversation_turns WHERE user_id = :u AND role = 'user'"),
            {"u": str(uid)})).scalar())


# --- one turn, one job -------------------------------------------------------------------------------

def test_two_concurrent_enqueues_of_one_turn_create_exactly_one_job(clean_db):
    """The race a caller-side check cannot win, now stated over the canonical id.

    Both deliveries carry the SAME conversation_turn_id, so both pass any SELECT-then-INSERT guard before
    either commits. Only the database can decide this, and only one of them may end up describing the
    turn — a turn observed twice carries double weight in the agreement rate that the authority decision
    is made on.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    turn_id = _run(_turn(uid, channel=IMESSAGE, pmid="pm-raced", text="can you email my teacher"))

    async def _both():
        return await asyncio.gather(_intake(uid, channel=IMESSAGE, pmid="pm-raced", turn_id=turn_id),
                                    _intake(uid, channel=IMESSAGE, pmid="pm-raced", turn_id=turn_id))

    created = _run(_both())
    assert _run(_jobs_for_turn(turn_id)) == 1, "one conversation turn was queued for observation twice"
    assert sum(bool(c) for c in created) == 1, (
        "both concurrent deliveries reported creating a job — the winner is ambiguous, which means the "
        "constraint did not decide it")


def test_retrying_one_turn_never_creates_a_second_job(clean_db):
    """Redelivery, retry, replay — the ordinary case, repeated until it would show.

    Asserted on the ROW COUNT and on the disposition, because `intake` returning False has two very
    different meanings ("already queued" and "the insert blew up and was swallowed") and only one of them
    is this property holding.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    turn_id = _run(_turn(uid, channel=IMESSAGE, pmid="pm-retry", text="make it professional and send it"))
    tally = semantic_shadow.EnqueueLedger()

    async def _five():
        return [await semantic_shadow.intake(
            uid, channel=IMESSAGE, provider_message_id="pm-retry", decision=_Decision(),
            conversation_turn_id=turn_id, tally=tally) for _ in range(5)]

    created = _run(_five())
    assert created == [True, False, False, False, False]
    assert _run(_jobs_for_turn(turn_id)) == 1, "a retried turn accumulated observations"
    ledger = tally.as_json()
    assert ledger["enqueued"] == 1 and ledger["duplicate"] == 4, (
        "a retry was not accounted as a duplicate. `enqueue_failed` is THE number that means observations "
        "are being lost; driving it with routine retries leaves that signal permanently on")


# --- one turn per (channel, message id), and the loss this file exists to stop -----------------------

def test_one_message_id_on_two_channels_is_two_turns_and_two_jobs(clean_db):
    """THE LOSS BIAS, reproduced end to end.

    A provider message id is unique WITHIN a channel and nothing more; two channels can hand back the
    same string. `conversation_turns` already knows this — `uq_turn_msg_role` includes the channel — so
    the student really has two distinct turns here, and each of them is a separate thing the executive
    must be measured on.

    This is the case that escapes every other test in the suite. Narrow the shadow key to
    (user_id, provider_message_id) and all 27 shadow tests stay green while the second channel's turn is
    swallowed as a duplicate: no row, no disposition, no trace, and a sample quietly missing whichever
    turn lost the race.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    shared = "pm-collision"
    first = _run(_turn(uid, channel=IMESSAGE, pmid=shared, text="email alvarez a thank you note"))
    second = _run(_turn(uid, channel=SMS, pmid=shared, text="actually make it shorter"))

    assert first != second, (
        "the canonical ledger collapsed two channels' turns into one row — this test is about the shadow "
        "key and it cannot say anything if conversation_turns lost the turn first")
    assert _run(_turns(uid)) == 2

    assert _run(_intake(uid, channel=IMESSAGE, pmid=shared, turn_id=first)) is True
    assert _run(_intake(uid, channel=SMS, pmid=shared, turn_id=second)) is True, (
        "the second channel's turn was swallowed as a duplicate of the first — a LOST observation, and "
        "the turns that get lost are whichever ones arrived second")

    assert _run(_jobs_for_turn(first)) == 1
    assert _run(_jobs_for_turn(second)) == 1
    assert _run(_all_jobs(uid)) == 2


def test_two_different_turns_from_one_user_are_never_collapsed(clean_db):
    """The other half of any uniqueness rule: a key too BROAD loses turns instead of duplicating them,
    which is the same bias wearing the opposite sign. The student's next message is a different turn and
    gets its own observation."""
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    a = _run(_turn(uid, channel=IMESSAGE, pmid="pm-1", text="can u email alvarez"))
    b = _run(_turn(uid, channel=IMESSAGE, pmid="pm-2", text="make it heartfelt"))

    assert _run(_intake(uid, channel=IMESSAGE, pmid="pm-1", turn_id=a)) is True
    assert _run(_intake(uid, channel=IMESSAGE, pmid="pm-2", turn_id=b)) is True, (
        "the student's next message was swallowed as a duplicate")
    assert _run(_jobs_for_turn(a)) == 1 and _run(_jobs_for_turn(b)) == 1
    assert _run(_all_jobs(uid)) == 2


def test_another_owners_turn_never_collides_with_this_one(clean_db):
    """Two students on the same provider, handed the same message id. Each turn is its own observation —
    and the canonical ids differ, so the constraint decides this without needing the user_id at all."""
    a, b = uuid4(), uuid4()
    _run(users.ensure(a, auth_provider="test"))
    _run(users.ensure(b, auth_provider="test"))
    ta = _run(_turn(a, channel=IMESSAGE, pmid="pm-shared", text="hey bruce"))
    tb = _run(_turn(b, channel=IMESSAGE, pmid="pm-shared", text="hey bruce"))

    assert _run(_intake(a, channel=IMESSAGE, pmid="pm-shared", turn_id=ta)) is True
    assert _run(_intake(b, channel=IMESSAGE, pmid="pm-shared", turn_id=tb)) is True, (
        "another owner's turn collided with this one")
    assert _run(_jobs_for_turn(ta)) == 1 and _run(_jobs_for_turn(tb)) == 1


# --- and the canonical key is the one carrying the invariant -----------------------------------------

def test_the_canonical_turn_id_alone_stops_a_second_observation_of_one_turn(clean_db):
    """THE TEST THAT DISTINGUISHES THE TWO CONSTRAINTS, and the reason the canonical one exists.

    One conversation turn, offered twice under DIFFERENT provider metadata — the shape a channel
    migration, a re-ingested webhook or an amended provider id produces. The
    (user_id, channel, provider_message_id) constraint does not fire: the triples differ. Only
    `uq_semantic_shadow_job_conversation_turn` can refuse the second row, and refusing it is the
    invariant: one observation per TURN, not per piece of provider metadata that happens to name it.

    Delete the canonical constraint and this is the only test in the suite that goes red.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    turn_id = _run(_turn(uid, channel=IMESSAGE, pmid="pm-original", text="YES WRITE IT AND SEND IT"))

    assert _run(_intake(uid, channel=IMESSAGE, pmid="pm-original", turn_id=turn_id)) is True
    assert _run(_intake(uid, channel=SMS, pmid="pm-reingested", turn_id=turn_id)) is False, (
        "one conversation turn was observed twice under two different provider ids — the ingress triple "
        "cannot see that, so only the canonical key can, and it did not")
    assert _run(_jobs_for_turn(turn_id)) == 1
    assert _run(_all_jobs(uid)) == 1


def test_a_duplicate_by_either_key_is_a_duplicate_and_never_a_failed_enqueue(clean_db):
    """Two constraints, one disposition. `enqueue` swallows every exception, so a conflict that is not
    handled by `ON CONFLICT` becomes `enqueue_failed` — the counter that is supposed to mean observations
    are being LOST. Naming one constraint in the ON CONFLICT clause would leave the other raising, and
    routine redelivery would drive the lost-observation signal forever."""
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    turn_id = _run(_turn(uid, channel=IMESSAGE, pmid="pm-a", text="hey"))
    tally = semantic_shadow.EnqueueLedger()

    async def _go():
        # first: created. second: conflicts on the ingress triple. third: conflicts on the canonical id.
        return [
            await semantic_shadow.intake(uid, channel=IMESSAGE, provider_message_id="pm-a",
                                         decision=_Decision(), conversation_turn_id=turn_id,
                                         tally=tally),
            await semantic_shadow.intake(uid, channel=IMESSAGE, provider_message_id="pm-a",
                                         decision=_Decision(), conversation_turn_id=None, tally=tally),
            await semantic_shadow.intake(uid, channel=SMS, provider_message_id="pm-b",
                                         decision=_Decision(), conversation_turn_id=turn_id,
                                         tally=tally),
        ]

    assert _run(_go()) == [True, False, False]
    assert tally.as_json()["duplicate"] == 2 and tally.as_json()["enqueue_failed"] == 0, (
        "a conflict on one of the two keys was recorded as a failed enqueue rather than as a duplicate")
    assert _run(_all_jobs(uid)) == 1


# --- and the cascade, which is the other half of the reference ---------------------------------------

def test_deleting_the_canonical_turn_deletes_the_telemetry_that_points_at_it(clean_db):
    """TELEMETRY MUST NOT OUTLIVE THE CONTENT IT DESCRIBES.

    The shadow row holds no words — that is deliberate, and it is why a second copy of the most sensitive
    free-text Bruce stores was refused. But it holds a REFERENCE, and a reference that survives its
    referent is a record of a message the student has deleted: which channel it came in on, when it
    arrived, what the router thought of it. A deletion that leaves that behind is a deletion that did not
    happen.

    Asserted by actually deleting the turn, because ON DELETE CASCADE is a property of the database and
    the only way to read it is to make the database act on it.
    """
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    turn_id = _run(_turn(uid, channel=IMESSAGE, pmid="pm-erased", text="please forget I said this"))
    assert _run(_intake(uid, channel=IMESSAGE, pmid="pm-erased", turn_id=turn_id)) is True
    assert _run(_jobs_for_turn(turn_id)) == 1, "nothing was written, so the deletion proves nothing"

    async def _delete():
        async with user_session(uid) as s:
            await s.execute(sa_text("DELETE FROM conversation_turns WHERE id = :t"), {"t": str(turn_id)})

    _run(_delete())
    assert _run(_jobs_for_turn(turn_id)) == 0, (
        "the shadow row outlived the turn it describes. The row is telemetry ABOUT a message; once the "
        "message is gone it is a record of something the student deleted")
    assert _run(_all_jobs(uid)) == 0
