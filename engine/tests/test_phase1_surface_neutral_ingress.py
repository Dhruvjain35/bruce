"""PHASE 1 — a trusted turn can enter Bruce without Mac relay semantics.

THE GOAL IS TRANSPORT NEUTRALITY AND NOTHING ELSE. No second semantic executive, no second goal system,
no second runtime. An authenticated cloud surface must reach the SAME
`conversation_runtime.handle` the relay reaches, and the durable core must not be able to tell the
difference except where the product says it should.

WHAT THE PRODUCT SAYS IS SHARED, and therefore must cross surfaces:
    people · durable memories · Personal Operating Model · active delegations/goals ·
    verified outcomes · explicit policies

WHAT THE PRODUCT SAYS IS THREAD-LOCAL, and therefore must NOT cross surfaces:
    recent raw turns · deictic "this/that" · inline reply context · active clarification ·
    presentation context

Those two lists are the whole design. A goal is a commitment Bruce owns for a person; a raw transcript is
something that happened in one place. Merging the second is how a spoken "send that one" silently binds
to a text message the user is not looking at.

TWO SAFETY DEFECTS THE PHASE-0 AUDIT FOUND ARE FIXED HERE FIRST, GENERICALLY, BEFORE THE NEW CHANNEL
EXISTS — because both fail in the direction of "a new surface is silently less safe than the old one":

  * `gate_outbound_text` FAILS OPEN. `if channel_value not in _PLAIN_TEXT_CHANNELS: return text` returns
    UNGATED text for any channel it does not recognise, in the function whose own docstring calls itself
    the last-line floor with "no bypasses". Adding a channel without touching that set would have
    silently disabled PROHIBITED_PHRASES and enforce_no_dashes for it.
  * The outbound CLAIM has no channel predicate. Its correctness today rests entirely on there being
    exactly one channel in the table, so the first relay to poll would claim a spoken reply and speak it
    into iMessage.

CHANNEL NAMING. The member is `spoken`, not `voice`. "Voice" already means PERSONA throughout this
repo — `conversation_style.VoiceProfile`, "Bruce Voice OS", `product/voice_profiles.yaml`, ~50 uses —
and a `ChannelKind.voice` would read as style to every existing reader.

TRANSPORT-IDENTITY DEBT, DELIBERATELY NOT PAID HERE. `conversation_id` is a phone number
(`conversation_runtime.py` sets it from `msg.channel_identity`) and it reaches the durable consent layer.
Phase 1 gives spoken ingress a STABLE OPAQUE scope instead, and does not redesign authorization. The debt
is recorded in VOICE_PIVOT_BASELINE.md for a dedicated later safety migration.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import time
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import access_control, conversation_runtime, messaging_outbound, schema
from bruce_engine.conversation_contract import (ConversationDecision, IntentKind, ResponseType,
                                                RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage

PHONE = "+15557654321"
client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    monkeypatch.setenv("BRUCE_JWT_SECRET", "test-secret-that-is-at-least-32-bytes-long!!")
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    yield
    db._engine = None
    db._sessionmaker = None


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _auth(uid):
    tok = jwt.encode({"sub": str(uid), "exp": int(time.time()) + 3600},
                     os.environ["BRUCE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _decision(text="ok", caps=None, intent=IntentKind.casual, rt=ResponseType.direct_answer):
    return ConversationDecision(
        intent=intent, response_type=rt, user_visible_response=text, extracted_entities=[],
        required_capabilities=caps or [], needs_mission=False, risk_level=RiskLevel.none,
        confidence=0.8)


class RecordingReasoner:
    provider = model = "fake"
    supports_vision = True

    def __init__(self, decision=None):
        self.calls = 0
        self.contexts: list[str] = []
        self._d = decision or _decision()

    async def decide(self, *, text, images, context):
        self.calls += 1
        self.contexts.append(context or "")
        return ReasonResult(decision=self._d, provider="fake", model="fake",
                            input_tokens=0, output_tokens=0, latency_ms=1)


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="apple"))


async def _grant(uid):
    await access_control.activate_production_entitlement(uid, capability="conversation",
                                                         reason="phase1 test", actor="test")


def _new_user():
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    asyncio.run(_grant(uid))
    return uid


async def _turns(uid, *, channel=None):
    async with user_session(uid) as s:
        q = select(schema.ConversationTurn).where(schema.ConversationTurn.user_id == uid,
                                                  schema.ConversationTurn.role == "user")
        if channel is not None:
            q = q.where(schema.ConversationTurn.channel == channel)
        return (await s.execute(q)).scalars().all()


async def _outbound(uid):
    async with user_session(uid) as s:
        return (await s.execute(select(schema.OutboundMessageRow).where(
            schema.OutboundMessageRow.user_id == uid))).scalars().all()


def _spoken_msg(uid, *, pmid, text, scope=None):
    """A spoken turn: NO phone number anywhere. The thread scope is a stable opaque string."""
    return InboundMessage(
        provider_message_id=pmid, channel=ChannelKind.spoken,
        channel_identity=scope or f"session:{uid}", text=text, attachments=[], timestamp=_now())


# =====================================================================================================
# 1. a cloud JWT user can submit a trusted spoken turn that reaches conversation_runtime.handle
# =====================================================================================================

def test_1_jwt_user_can_submit_a_spoken_turn_that_reaches_the_conversation_runtime(clean_db, monkeypatch):
    """THE PHASE-1 CLAIM. Not a new runtime — the SAME one, reached from a cloud surface."""
    uid = _new_user()
    seen = {}

    real_handle = conversation_runtime.handle

    async def _spy(channel, msg, **kw):
        seen["msg"] = msg
        seen["user_id"] = kw.get("user_id")
        return await real_handle(channel, msg, **kw, reasoner=RecordingReasoner())

    monkeypatch.setattr(conversation_runtime, "handle", _spy)

    r = client.post("/v1/turns", headers=_auth(uid), json={
        "source": "spoken", "trusted_text": "remind me to email coach about practice",
        "source_turn_id": str(uuid4())})

    assert r.status_code in (200, 202), r.text
    assert seen.get("user_id") == uid, "the core was not reached with the authenticated user"
    assert seen["msg"].channel is ChannelKind.spoken
    assert seen["msg"].text == "remind me to email coach about practice"
    assert len(asyncio.run(_turns(uid, channel="spoken"))) == 1


# =====================================================================================================
# 2. no phone-number / channel-identity lookup is required for authenticated spoken ingress
# =====================================================================================================

def test_2_spoken_ingress_needs_no_messaging_identity_row(clean_db, monkeypatch):
    """The relay derives user_id from (channel, handle) because an anonymous SMS proves nothing about who
    is texting. A JWT already proves it, so that lookup must not be on the spoken path at all."""
    uid = _new_user()

    async def _no_identities():
        async with worker_session() as s:
            return (await s.execute(select(func.count()).select_from(schema.MessagingIdentity).where(
                schema.MessagingIdentity.user_id == uid))).scalar_one()

    assert asyncio.run(_no_identities()) == 0, "fixture drift: this user must have no messaging identity"

    monkeypatch.setattr(conversation_runtime, "production_reasoner", lambda: RecordingReasoner())
    r = client.post("/v1/turns", headers=_auth(uid), json={
        "source": "spoken", "trusted_text": "hey", "source_turn_id": str(uuid4())})

    assert r.status_code in (200, 202), r.text
    assert len(asyncio.run(_turns(uid, channel="spoken"))) == 1
    assert asyncio.run(_no_identities()) == 0, "spoken ingress created a messaging identity"


# =====================================================================================================
# 3. duplicate delivery creates ONE canonical turn
# =====================================================================================================

def test_3_duplicate_spoken_delivery_creates_one_canonical_turn(clean_db, monkeypatch):
    uid = _new_user()
    reasoner = RecordingReasoner()
    monkeypatch.setattr(conversation_runtime, "production_reasoner", lambda: reasoner)
    stid = str(uuid4())
    body = {"source": "spoken", "trusted_text": "hey", "source_turn_id": stid}

    a = client.post("/v1/turns", headers=_auth(uid), json=body)
    b = client.post("/v1/turns", headers=_auth(uid), json=body)

    assert a.status_code in (200, 202) and b.status_code in (200, 202)
    assert len(asyncio.run(_turns(uid, channel="spoken"))) == 1, "a redelivery created a second turn"
    assert reasoner.calls == 1, "a redelivery reached the model a second time"
    assert len(asyncio.run(_outbound(uid))) == 1, "a redelivery produced a second reply"


# =====================================================================================================
# 4. SHARED: the same user's durable active goal is visible across text and spoken surfaces
# =====================================================================================================

def test_4_a_durable_goal_is_visible_across_surfaces(clean_db):
    """PRODUCT DECISION A. A delegation is a commitment Bruce owns FOR A PERSON, not a fact about a
    phone number. If Bruce takes work on by text and forgets it when spoken to, there is no delegation
    product."""
    from bruce_engine import goal_runtime

    uid = _new_user()

    async def _open_from(scope):
        return await goal_runtime.open_runs(uid, conversation_id=scope)

    async def _make_goal():
        return await goal_runtime.ensure_goal(
            uid, capability="gmail.send_message", conversation_id=PHONE,
            slots_in={}, turn_index=1, decision=None)

    asyncio.run(_make_goal())

    from_text = asyncio.run(_open_from(PHONE))
    assert from_text, "fixture drift: the goal was not created"

    # THE ASSERTION. The runtime must offer this goal to a spoken turn as a continuation candidate.
    candidates = asyncio.run(conversation_runtime._open_goal_candidates(uid, f"session:{uid}"))
    ids = {str(c.get("id")) for c in candidates}
    assert {str(r.get("id")) for r in from_text} <= ids, (
        "a goal opened over text is invisible to a spoken turn — Bruce forgot a commitment because the "
        "user changed surface")


# =====================================================================================================
# 5. THREAD-LOCAL: raw recent conversational history stays surface/thread scoped
# =====================================================================================================

def test_5_raw_recent_history_does_not_leak_across_surfaces(clean_db):
    """PRODUCT DECISION B, and the counterweight to test 4. Merging raw transcripts is how a spoken
    'send that one' silently binds to a text the user is not looking at."""
    from bruce_engine import conversation_store

    uid = _new_user()

    async def _seed_and_read():
        await conversation_store.persist_user_turn(
            uid, channel=ChannelKind.self_hosted_imessage.value, channel_identity=PHONE,
            provider_message_id="t-1", text="the flyer about the physics review")
        return await conversation_store.load_recent_turns(
            uid, channel=ChannelKind.spoken.value, channel_identity=f"session:{uid}")

    spoken_window = asyncio.run(_seed_and_read())
    texts = [t.text for t in spoken_window if t.text]
    assert not any("physics review" in (t or "") for t in texts), (
        f"a raw text-surface turn leaked into the spoken context window: {texts!r}")


# =====================================================================================================
# 6. ambiguous deictic references across surfaces CLARIFY rather than guess
# =====================================================================================================

def test_6_a_cross_surface_deictic_does_not_silently_bind(clean_db, monkeypatch):
    """PRODUCT DECISION C. 'send that one' spoken, with the referent living only in a text thread, must
    not resolve by reaching across the surface. It is allowed to ask. It is not allowed to guess."""
    from bruce_engine import conversation_store

    uid = _new_user()
    asyncio.run(conversation_store.persist_user_turn(
        uid, channel=ChannelKind.self_hosted_imessage.value, channel_identity=PHONE,
        provider_message_id="t-ref", text="draft an email to coach smith about missing practice"))

    reasoner = RecordingReasoner()
    monkeypatch.setattr(conversation_runtime, "production_reasoner", lambda: reasoner)

    r = client.post("/v1/turns", headers=_auth(uid), json={
        "source": "spoken", "trusted_text": "yeah send that one", "source_turn_id": str(uuid4())})
    assert r.status_code in (200, 202), r.text

    ctx = "\n".join(reasoner.contexts)
    assert "coach smith" not in ctx.lower(), (
        "the text-thread referent was spliced into the spoken turn's context, so the model could bind "
        "'that one' to something the speaker never said aloud")
    assert len(asyncio.run(_outbound(uid))) == 1, "the deictic turn produced more than one reply"


# =====================================================================================================
# 7. an unknown / unconfigured channel cannot bypass gate_outbound_text
# =====================================================================================================

def test_7_an_unknown_channel_cannot_bypass_the_outbound_safety_floor():
    """THE FAIL-OPEN DEFECT, fixed generically BEFORE the new channel exists.

    `gate_outbound_text` is documented as the last-line floor with 'no bypasses', and its unknown-channel
    branch returned the text untouched. A channel nobody has classified must get the STRICTEST treatment,
    not none.
    """
    dirty = "I'd be happy to help — great question!"

    for channel in ("totally_unknown_channel", "", "spoken", "future_surface_v2"):
        out = messaging_outbound.gate_outbound_text(dirty, channel)
        assert "i'd be happy to" not in out.lower(), f"{channel!r} bypassed the phrase strip"
        assert "great question" not in out.lower(), f"{channel!r} bypassed the phrase strip"
        assert "—" not in out, f"{channel!r} bypassed the em-dash rewrite"


def test_7b_the_known_plain_text_channel_is_unchanged():
    """The fix must not change what iMessage already got."""
    dirty = "I'd be happy to help — great question!"
    assert messaging_outbound.gate_outbound_text(dirty, ChannelKind.self_hosted_imessage.value) == \
        messaging_outbound.gate_outbound_text(dirty, "spoken")


# =====================================================================================================
# 8. an outbound row cannot be claimed by an adapter for a different channel
# =====================================================================================================

def test_8_outbound_claim_cannot_be_stolen_across_channels(clean_db):
    """Correctness today rests on there being exactly one channel in the table. The moment a second
    delivery surface exists, the first relay to poll speaks the other surface's reply."""
    uid = _new_user()

    async def _seed():
        await messaging_outbound.enqueue(
            user_id=uid, to_handle=f"session:{uid}", channel=ChannelKind.spoken,
            kind="acknowledged", text="spoken reply", idempotency_key="spk:1")

    asyncio.run(_seed())

    imsg_device = uuid4()
    claimed = asyncio.run(messaging_outbound.claim(
        imsg_device, channel=ChannelKind.self_hosted_imessage.value))
    assert claimed is None, (
        "an iMessage relay claimed a spoken-channel row — the reply would be spoken into the wrong "
        "surface entirely")

    mine = asyncio.run(messaging_outbound.claim(uuid4(), channel=ChannelKind.spoken.value))
    assert mine is not None and mine.text == "spoken reply", "the right channel could not claim its row"


# =====================================================================================================
# 9. existing iMessage behavior is unchanged
# =====================================================================================================

def test_9_existing_imessage_turn_is_unchanged(clean_db, monkeypatch):
    """The regression guard for the whole phase. The relay path must behave exactly as before."""
    uid = _new_user()
    reasoner = RecordingReasoner()
    msg = InboundMessage(provider_message_id="imsg-1", channel=ChannelKind.self_hosted_imessage,
                         channel_identity=PHONE, text="hi", attachments=[], timestamp=_now())

    out = asyncio.run(conversation_runtime.handle(
        FakeChannel(), msg, user_id=uid, reply_target=PHONE, reasoner=reasoner))

    assert out.status == "processed"
    assert reasoner.calls == 1
    rows = asyncio.run(_outbound(uid))
    assert len(rows) == 1
    assert rows[0].channel == ChannelKind.self_hosted_imessage.value, (
        "the iMessage reply is no longer addressed to iMessage")
    turns = asyncio.run(_turns(uid, channel=ChannelKind.self_hosted_imessage.value))
    assert len(turns) == 1


# =====================================================================================================
# 10. one turn still produces at most one consequential execution
# =====================================================================================================

def test_10_concurrent_spoken_delivery_reaches_the_model_once(clean_db):
    """N7's invariant, re-proved on the new surface. Two deliveries genuinely in flight together."""
    uid = _new_user()
    gate = asyncio.Event()
    entered = asyncio.Event()

    class Blocking:
        provider = model = "fake"
        supports_vision = True

        def __init__(self):
            self.calls = 0

        async def decide(self, *, text, images, context):
            self.calls += 1
            entered.set()
            if self.calls > 1:
                gate.set()          # a second entrant IS the defect: release so the test FAILS, not hangs
            await gate.wait()
            return ReasonResult(decision=_decision(), provider="fake", model="fake",
                                input_tokens=0, output_tokens=0, latency_ms=1)

    reasoner = Blocking()
    stid = str(uuid4())

    async def _race():
        m1 = _spoken_msg(uid, pmid=stid, text="hey")
        m2 = _spoken_msg(uid, pmid=stid, text="hey")
        a = asyncio.create_task(conversation_runtime.handle(
            FakeChannel(), m1, user_id=uid, reply_target=f"session:{uid}", reasoner=reasoner))
        await asyncio.wait_for(entered.wait(), timeout=30)
        b = await conversation_runtime.handle(
            FakeChannel(), m2, user_id=uid, reply_target=f"session:{uid}", reasoner=reasoner)
        gate.set()
        return await a, b

    a, b = asyncio.run(_race())
    assert reasoner.calls == 1, f"the model ran {reasoner.calls} times for one spoken message"
    assert {a.status, b.status} == {"processed", "duplicate"}
    assert len(asyncio.run(_turns(uid, channel="spoken"))) == 1
    assert len(asyncio.run(_outbound(uid))) == 1
