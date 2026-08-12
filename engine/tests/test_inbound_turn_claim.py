"""N7 — one canonical inbound message, at most one active turn, at most one consequential execution.

THE INVARIANT, and why the existing guard cannot hold it.

`conversation_runtime` dedupes with `_already_answered`, which asks whether an ASSISTANT turn exists for
this provider message id. That row is written by `_finalize`, at the very END of the turn — after the
reasoner, after the draft composer, after any Gmail send. So the check is check-then-act with a window
several seconds wide:

    delivery A   _already_answered -> False ......... reasoner ... compose ... GMAIL SEND ... finalize
    delivery B          _already_answered -> False ......... reasoner ... compose ... GMAIL SEND ...

Both deliveries see no assistant row, because neither has written one yet. `test_conversation_runtime.
test_exactly_one_outbound_and_redelivery_idempotent` does not catch this: it runs the two deliveries
SEQUENTIALLY, which is the case the assistant-row check does handle.

The other two guards named as protection do not close it either. `persist_user_turn` returned the
existing id rather than reporting that it lost, so it could not be used as a decision. The
`conv:{pmid}` outbound idempotency key dedupes the iMessage ROW, which happens after the send. And the
Gmail dedupe is itself check-then-act, with the authorization consumed after `perform()`.

This matters because DEFECT-5's fix is to RETRY a timed-out inbound POST. Adding that retry on top of
this window is what turns a latency bug into a student's professor receiving the same mail twice.

WHAT REPLACES IT. The claim is the canonical user-turn INSERT itself. `uq_turn_msg_role` already
constrains (user_id, channel, provider_message_id, role) to be unique, so `INSERT ... ON CONFLICT DO
NOTHING RETURNING id` lets POSTGRES pick the winner: exactly one caller gets a row back, no matter how
many arrive at once, and the claim is taken BEFORE any model call or provider write.

WHAT THIS DELIBERATELY DOES NOT DO. There is no lease and no takeover, so a turn whose process dies
mid-flight holds its claim forever and a redelivery is refused. That is the honest reading of "at most
one consequential execution", and it is not a regression: `relay.py` already returns "retry" on timeout
without checkpointing or queueing, so a crashed turn is lost today too. Bounded recovery needs a lease
plus a marker for "consequential work has started", which needs columns this table does not have.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import conversation_runtime, conversation_store, schema
from bruce_engine.conversation_contract import (ConversationDecision, IntentKind, ResponseType,
                                                RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage

PHONE = "+15557654321"
PROV = ChannelKind.self_hosted_imessage.value


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _msg(pmid="c1", text="hi"):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=PHONE, text=text, attachments=[], timestamp=_now())


def _decision(text="hey"):
    return ConversationDecision(
        intent=IntentKind.casual, response_type=ResponseType.direct_answer,
        user_visible_response=text, extracted_entities=[], required_capabilities=[],
        needs_mission=False, risk_level=RiskLevel.none, confidence=0.8)


class CountingReasoner:
    """Records how many turns actually reached the model. `calls` is the whole point of this suite."""

    provider = "fake"
    model = "fake"
    supports_vision = True

    def __init__(self, gate: asyncio.Event | None = None):
        self.calls = 0
        self._gate = gate
        # HANDSHAKE, not a sleep. The first version of this suite spun `await asyncio.sleep(0)` hoping
        # delivery A would reach the reasoner first, but A has real database I/O before it — so B could
        # win the claim, park here on the gate, and the `gate.set()` that follows B would never run.
        # The test deadlocked itself. `entered` makes "the claim holder is now inside the model call" an
        # observable fact instead of a hope.
        self.entered = asyncio.Event()

    async def decide(self, *, text, images, context):
        self.calls += 1
        self.entered.set()
        # A BROKEN FIX MUST FAIL HERE, NOT HANG. If the claim gate is missing, delivery B also arrives in
        # this method and would park on a gate that is only released after B returns — so the test
        # deadlocks instead of failing, and a mutation runner waits on it forever. (It did: 36 minutes on
        # a 7-second suite.) A second entrant is already the defect, so release everyone and let the
        # `calls == 1` assertion below report it properly.
        if self.calls > 1 and self._gate is not None:
            self._gate.set()
        if self._gate is not None:
            await self._gate.wait()          # hold the turn open, exactly like a slow model call
        return ReasonResult(decision=_decision(), provider="fake", model="fake",
                            input_tokens=0, output_tokens=0, latency_ms=1)


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="alpha_bridge"))


async def _outbound(uid):
    async with user_session(uid) as s:
        return (await s.execute(select(schema.OutboundMessageRow).where(
            schema.OutboundMessageRow.user_id == uid))).scalars().all()


async def _user_turns(uid, pmid):
    async with user_session(uid) as s:
        return (await s.execute(select(func.count()).select_from(schema.ConversationTurn).where(
            schema.ConversationTurn.user_id == uid,
            schema.ConversationTurn.provider_message_id == pmid,
            schema.ConversationTurn.role == "user"))).scalar_one()


# --- the claim primitive ------------------------------------------------------------------------------

def test_the_claim_is_atomic_under_concurrent_delivery(clean_db):
    """POSTGRES picks the winner, not a SELECT. Eight simultaneous deliveries of the same message must
    produce exactly one claim and one row, and every caller must learn the same turn id."""
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    pmid = "race-1"

    async def _race():
        await _ensure_user(uid)
        return await asyncio.gather(*(
            conversation_store.claim_inbound_turn(
                uid, channel=PROV, channel_identity=PHONE, provider_message_id=pmid, text="hi")
            for _ in range(8)))

    results = asyncio.run(_race())

    claimed = [r for r in results if r.claimed]
    assert len(claimed) == 1, (
        f"{len(claimed)} of 8 concurrent deliveries each believed they owned the turn — every one of "
        f"them would go on to run the reasoner and send")
    ids = {r.turn_id for r in results}
    assert len(ids) == 1 and None not in ids, (
        f"callers disagreed about the canonical turn id: {ids}. The shadow ledger references this id.")
    assert asyncio.run(_user_turns(uid, pmid)) == 1


def test_a_second_delivery_after_the_first_completes_does_not_claim(clean_db):
    uid = uuid4()
    asyncio.run(_ensure_user(uid))

    async def _twice():
        await _ensure_user(uid)
        a = await conversation_store.claim_inbound_turn(
            uid, channel=PROV, channel_identity=PHONE, provider_message_id="seq-1", text="hi")
        b = await conversation_store.claim_inbound_turn(
            uid, channel=PROV, channel_identity=PHONE, provider_message_id="seq-1", text="hi")
        return a, b

    a, b = asyncio.run(_twice())
    assert a.claimed is True and b.claimed is False
    assert a.turn_id == b.turn_id


def test_persist_user_turn_keeps_its_contract(clean_db):
    """The existing callers (and the shadow ledger) still get an id and nothing else. This fix must not
    become a signature change rippling through five test files."""
    uid = uuid4()
    asyncio.run(_ensure_user(uid))

    async def _go():
        await _ensure_user(uid)
        first = await conversation_store.persist_user_turn(
            uid, channel=PROV, channel_identity=PHONE, provider_message_id="keep-1", text="hi")
        again = await conversation_store.persist_user_turn(
            uid, channel=PROV, channel_identity=PHONE, provider_message_id="keep-1", text="hi")
        return first, again

    first, again = asyncio.run(_go())
    assert first is not None and first == again


# --- the runtime honours it ---------------------------------------------------------------------------

def test_concurrent_duplicate_delivery_reaches_the_model_exactly_once(clean_db):
    """THE DEFECT. Two deliveries of one message, genuinely in flight together.

    The losing delivery must be turned away BEFORE the reasoner, which is the only place the decision is
    still free — after it there has already been a model call, and after the composer there may have
    been a send.
    """
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    gate = asyncio.Event()
    reasoner = CountingReasoner(gate=gate)
    m = _msg("race-turn-1", text="hi")

    async def _race():
        await _ensure_user(uid)
        a = asyncio.create_task(conversation_runtime.handle(
            FakeChannel(), m, user_id=uid, reply_target=PHONE, reasoner=reasoner))
        # Wait until A is genuinely INSIDE the model call and holding the claim. This is the moment the
        # old check-then-act guard was blind at: the assistant row does not exist yet and will not for
        # several seconds, so `_already_answered` would wave B straight through.
        await asyncio.wait_for(reasoner.entered.wait(), timeout=30)
        b = await conversation_runtime.handle(
            FakeChannel(), m, user_id=uid, reply_target=PHONE, reasoner=reasoner)
        gate.set()
        return await a, b

    a, b = asyncio.run(_race())

    assert reasoner.calls == 1, (
        f"the reasoner ran {reasoner.calls} times for ONE inbound message — the second delivery was "
        f"already past the point where a Gmail send becomes possible")
    assert {a.status, b.status} == {"processed", "duplicate"}, (
        f"expected one processed and one duplicate, got {a.status!r} and {b.status!r}")
    assert asyncio.run(_user_turns(uid, "race-turn-1")) == 1


def test_concurrent_duplicate_delivery_enqueues_exactly_one_reply(clean_db):
    """One student message earns one reply. Two replies is the visible symptom; a duplicate real email
    is the expensive one."""
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    gate = asyncio.Event()
    reasoner = CountingReasoner(gate=gate)
    m = _msg("race-turn-2", text="hi")

    async def _race():
        await _ensure_user(uid)
        a = asyncio.create_task(conversation_runtime.handle(
            FakeChannel(), m, user_id=uid, reply_target=PHONE, reasoner=reasoner))
        await asyncio.wait_for(reasoner.entered.wait(), timeout=30)
        b = await conversation_runtime.handle(
            FakeChannel(), m, user_id=uid, reply_target=PHONE, reasoner=reasoner)
        gate.set()
        await a
        return b

    asyncio.run(_race())
    assert len(asyncio.run(_outbound(uid))) == 1


def test_a_delivery_that_does_not_hold_the_claim_never_reaches_the_model(clean_db):
    """The deterministic form of the race: the claim is already held when the delivery arrives, exactly
    as it would be for a turn still in flight in another process. No sleeps, no scheduling luck."""
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    reasoner = CountingReasoner()

    async def _go():
        await _ensure_user(uid)
        held = await conversation_store.claim_inbound_turn(
            uid, channel=PROV, channel_identity=PHONE, provider_message_id="held-1", text="hi")
        assert held.claimed is True
        return await conversation_runtime.handle(
            FakeChannel(), _msg("held-1", text="hi"), user_id=uid, reply_target=PHONE, reasoner=reasoner)

    out = asyncio.run(_go())
    assert out.status == "duplicate"
    assert reasoner.calls == 0, "a delivery that lost the claim still called the model"
    assert asyncio.run(_outbound(uid)) == []


def test_sequential_redelivery_still_reports_duplicate(clean_db):
    """The behaviour the old check already had must survive being replaced — a webhook redelivery after
    the turn finished is still exactly one reply."""
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    reasoner = CountingReasoner()
    m = _msg("dup-seq-1", text="hi")

    a = asyncio.run(conversation_runtime.handle(FakeChannel(), m, user_id=uid, reply_target=PHONE,
                                                reasoner=reasoner))
    b = asyncio.run(conversation_runtime.handle(FakeChannel(), m, user_id=uid, reply_target=PHONE,
                                                reasoner=reasoner))
    assert a.status == "processed" and b.status == "duplicate"
    assert reasoner.calls == 1
    assert len(asyncio.run(_outbound(uid))) == 1
