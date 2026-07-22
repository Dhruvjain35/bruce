"""Bite 1 conversation runtime — fake reasoner + fake channel + real Postgres.

Proves the required behaviors WITHOUT any model call or real iMessage: worksheet->tutoring (no
mission), event->candidate persisted + honest "calendar not connected" (never "added"), casual reply,
unreadable/timeout->honest fallback, exactly ONE outbound + redelivery-idempotent, group inbound
never answered, and no chain-of-thought persisted. Skips cleanly without Postgres.
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
from bruce_engine import conversation_runtime, schema
from bruce_engine.conversation_contract import (
    ConversationDecision, ExtractedEntity, IntentKind, ResponseType, RiskLevel,
)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import Attachment, AttachmentKind, ChannelKind, FakeChannel, InboundMessage

PHONE = "+15557654321"


def _valid_png() -> bytes:
    import io
    from PIL import Image
    b = io.BytesIO(); Image.new("RGB", (32, 32), "white").save(b, format="PNG"); return b.getvalue()


_PNG = _valid_png()


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


def _msg(pmid="c1", text=None, attachments=None, is_group=False, attachment_unavailable=False):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=PHONE, text=text, attachments=attachments or [],
                          timestamp=_now(), is_group=is_group, attachment_unavailable=attachment_unavailable)


def _decision(intent, response_type, text="ok", entities=None, caps=None, needs_mission=False,
              risk=RiskLevel.none):
    return ConversationDecision(
        intent=intent, response_type=response_type, user_visible_response=text,
        extracted_entities=entities or [], required_capabilities=caps or [],
        needs_mission=needs_mission, risk_level=risk, confidence=0.8)


class FakeReasoner:
    provider = "fake"
    model = "fake"
    supports_vision = True

    def __init__(self, decision=None, raises=False):
        self._decision = decision
        self._raises = raises

    async def decide(self, *, text, images, context):
        if self._raises:
            raise RuntimeError("boom")
        return ReasonResult(decision=self._decision, provider="fake", model="fake",
                            input_tokens=0, output_tokens=0, latency_ms=1)


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="alpha_bridge"))


def _run(coro):
    return asyncio.run(coro)


async def _outbound(uid):
    async with user_session(uid) as s:
        return (await s.execute(select(schema.OutboundMessageRow).where(
            schema.OutboundMessageRow.user_id == uid))).scalars().all()


async def _counts(uid):
    # event_candidates + conversation_turns are tenant-isolated (user-only), so query under the user's
    # RLS context, not a worker session (which their policy does NOT admit).
    async with user_session(uid) as s:
        ec = (await s.execute(select(func.count()).select_from(schema.EventCandidate).where(schema.EventCandidate.user_id == uid))).scalar_one()
        ij = (await s.execute(select(func.count()).select_from(schema.IntakeJob).where(schema.IntakeJob.user_id == uid))).scalar_one()
        at = (await s.execute(select(func.count()).select_from(schema.ConversationTurn).where(
            schema.ConversationTurn.user_id == uid, schema.ConversationTurn.role == "assistant"))).scalar_one()
    return ec, ij, at


def test_worksheet_help_is_tutoring_no_mission(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    img = Attachment(kind=AttachmentKind.image, media_type="image/png", data=b"\x89PNG", filename="hw.png")
    d = _decision(IntentKind.educational_help, ResponseType.tutoring,
                  text="looks like parametric motion. want a hint or should i check your answers?")
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("hw1", text="can u help w this", attachments=[img]),
                                           user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(d)))
    assert out.status == "processed"
    ec, ij, at = _run(_counts(uid))
    assert ec == 0 and ij == 0 and at == 1                    # tutoring: no candidate, NO intake mission
    ob = _run(_outbound(uid))
    assert len(ob) == 1 and "check your answers" in ob[0].text.lower()


def test_event_add_to_calendar_persists_candidate_never_claims_added(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    img = Attachment(kind=AttachmentKind.image, media_type="image/png", data=b"\x89PNG", filename="ticket.png")
    d = _decision(IntentKind.actionable, ResponseType.extraction_result,
                  text="found the event", caps=["calendar_write"], needs_mission=True,
                  entities=[
                      ExtractedEntity(type="event_title", value="Startup School 2026", source_span="Startup School 2026"),
                      ExtractedEntity(type="date", value="July 25-26", normalized="2026-07-25", source_span="July 25–26"),
                      ExtractedEntity(type="location", value="Chase Center, SF", source_span="Chase Center"),
                  ])
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("ev1", text="add this to my calendar", attachments=[img]),
                                           user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(d)))
    assert out.status == "processed"
    ob = _run(_outbound(uid))
    assert len(ob) == 1
    reply = ob[0].text.lower()
    assert "startup school 2026" in reply and "calendar" in reply
    assert "added to your calendar" not in reply and "added it" not in reply    # NEVER false completion

    async def _ec():
        async with user_session(uid) as s:
            return (await s.execute(select(schema.EventCandidate).where(schema.EventCandidate.user_id == uid))).scalar_one()
    ec = _run(_ec())
    assert ec.status == "proposed" and ec.title == "Startup School 2026"
    assert ec.provenance and ec.provenance.get("inbound_provider_message_id") == "ev1"   # grounded


def test_event_detected_by_entities_not_capability_string(clean_db):
    """Regression (found in LEVEL B staging): the real model phrases the capability freely
    ('calendar integration'/'calendar creation'), so the event branch must fire on title+date
    entities — NOT an exact 'calendar_write' id — and still persist the candidate + honest template."""
    uid = uuid4(); _run(_ensure_user(uid))
    d = _decision(IntentKind.unsupported, ResponseType.refusal, text="here's what i can read",
                  caps=["calendar integration"],   # NOT the exact 'calendar_write' id
                  entities=[ExtractedEntity(type="title", value="Club Fair", source_span="Club Fair"),
                            ExtractedEntity(type="date", value="Fri Sep 12", normalized="2026-09-12", source_span="Fri Sep 12")])
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("ev2", text="add this to my calendar"),
                                           user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(d)))
    assert out.status == "processed"
    ec, ij, at = _run(_counts(uid))
    assert ec == 1                                       # candidate persisted despite free-form cap string
    ob = _run(_outbound(uid))
    assert "added to your calendar" not in ob[0].text.lower() and "club fair" in ob[0].text.lower()


def test_casual_reply(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    d = _decision(IntentKind.casual, ResponseType.direct_answer, text="not much, what's good")
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("c1", text="yo what's up"),
                                           user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(d)))
    ec, ij, at = _run(_counts(uid))
    assert out.status == "processed" and ec == 0 and ij == 0
    assert len(_run(_outbound(uid))) == 1


def test_model_failure_is_honest_fallback(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    img = Attachment(kind=AttachmentKind.image, media_type="image/png", data=_PNG)
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("f1", text="?", attachments=[img]),
                                           user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(raises=True)))
    assert out.status == "model_error"
    ob = _run(_outbound(uid))
    # A model/backend glitch OWNS it and asks to retry — it must NOT blame a healthy image.
    assert len(ob) == 1 and "again" in ob[0].text.lower()
    assert "couldn't read" not in ob[0].text.lower() and "couldn't open" not in ob[0].text.lower()


def test_unreadable_attachment_only_asks_to_resend(clean_db):
    """A genuinely-corrupt file with no text -> honest 'couldn't open', not a fabricated read."""
    uid = uuid4(); _run(_ensure_user(uid))
    bad = Attachment(kind=AttachmentKind.image, media_type="image/png", data=b"\x89PNG not real")
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("f2", text=None, attachments=[bad]),
                                           user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(raises=True)))
    assert out.status == "processed"        # handled without even calling the model
    ob = _run(_outbound(uid))
    assert len(ob) == 1 and "open" in ob[0].text.lower() and "resend" in ob[0].text.lower()


def test_exactly_one_outbound_and_redelivery_idempotent(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    d = _decision(IntentKind.casual, ResponseType.direct_answer, text="hey")
    m = _msg("dup1", text="hi")
    a = _run(conversation_runtime.handle(FakeChannel(), m, user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(d)))
    b = _run(conversation_runtime.handle(FakeChannel(), m, user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(d)))
    assert a.status == "processed" and b.status == "duplicate"
    assert len(_run(_outbound(uid))) == 1                       # exactly one, even after redelivery


def test_group_inbound_is_never_answered(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    d = _decision(IntentKind.casual, ResponseType.direct_answer, text="hi")
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("g1", text="hi", is_group=True),
                                           user_id=uid, reply_target="chat;grp", reasoner=FakeReasoner(d)))
    assert out.status == "skipped_group"
    assert _run(_outbound(uid)) == []                           # no reply into a group


def test_no_chain_of_thought_persisted(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    d = _decision(IntentKind.casual, ResponseType.direct_answer, text="yo")
    _run(conversation_runtime.handle(FakeChannel(), _msg("cot1", text="hi"),
                                     user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(d)))

    async def _decision_keys():
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.ConversationTurn).where(
                schema.ConversationTurn.user_id == uid, schema.ConversationTurn.role == "assistant"))).scalar_one()
        return set(row.decision or {})
    keys = _run(_decision_keys())
    assert not (keys & {"reasoning", "scratchpad", "thoughts", "chain_of_thought", "cot"})


def test_enabled_for_is_async_db_backed_and_denies_by_default(clean_db, monkeypatch):
    """enabled_for is async + DB-backed (Bite 1.5): no allow-list env. With no entitlement/enrollment it
    DENIES (the inbound gate falls through to legacy); a live staging enrollment flips it to ALLOW; and an
    explicit BRUCE_CONVERSATION_RUNTIME hard-off overrides back to DENY."""
    from scripts import capability_admin
    monkeypatch.delenv("BRUCE_CONVERSATION_RUNTIME", raising=False)
    uid = uuid4(); _run(_ensure_user(uid))

    assert _run(conversation_runtime.enabled_for(uid)) is False            # no grant -> DENY (fall through)
    _run(capability_admin.enroll_staging(uid, reason="runtime-wiring", actor="tester@host"))
    assert _run(conversation_runtime.enabled_for(uid)) is True             # live staging enrollment -> ALLOW

    monkeypatch.setenv("BRUCE_CONVERSATION_RUNTIME", "off")                # global hard-off overrides
    assert _run(conversation_runtime.enabled_for(uid)) is False


# --- P0.1: an explicit reply target is AUTHORITATIVE (never answer from the newest image instead) -----

class RecordingReasoner(FakeReasoner):
    def __init__(self, decision):
        super().__init__(decision=decision)
        self.called = False
        self.context = None
        self.images = None

    async def decide(self, *, text, images, context):
        self.called = True
        self.context = context
        self.images = images
        return await super().decide(text=text, images=images, context=context)


def _reply_msg(pmid, text, *, reply_to=None, reply_context=None, attachments=None):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=PHONE, text=text, attachments=attachments or [],
                          timestamp=_now(), is_group=False,
                          reply_to_message_id=reply_to, reply_context=reply_context)


async def _stage_upload_rt(png):
    from bruce_engine import schema as _s
    async with worker_session() as s:
        up = _s.RelayUpload(content_hash="h" + uuid4().hex, media_type="image/png", data=png)
        s.add(up); await s.flush(); return up.id


def test_p0_reply_to_unavailable_image_fails_closed_not_from_newest(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    # a newer image B is answered first, so it sits in the recent-turn window
    _run(conversation_runtime.handle(FakeChannel(),
        _msg("imgB", attachments=[Attachment(kind=AttachmentKind.image, media_type="image/png",
                                              data=_PNG, filename="b.png")]),
        user_id=uid, reply_target=PHONE,
        reasoner=FakeReasoner(_decision(IntentKind.educational_help, ResponseType.tutoring, text="that's image b"))))
    # now the student REPLIES to old image A (bytes not downloaded on the Mac) WITH a text question
    env = {"resolution_source": "relay_exact_lookup", "referenced_direction": "inbound",
           "referenced_attachment_refs": [{"available": False, "unavailable_reason": "not_downloaded"}]}
    rec = RecordingReasoner(_decision(IntentKind.educational_help, ResponseType.tutoring, text="talking about image b"))
    out = _run(conversation_runtime.handle(FakeChannel(),
        _reply_msg("replyA", "wait what is b here again", reply_to="guidA", reply_context=env),
        user_id=uid, reply_target=PHONE, reasoner=rec))
    assert out.status == "processed"
    assert rec.called is False                       # NEVER reasoned -> impossible to answer from image B
    ob = _run(_outbound(uid))
    assert "downloaded" in ob[-1].text.lower()       # honest: replied-to file isn't on the Mac yet


def test_p0_reply_to_lost_target_asks_to_resend(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    rec = RecordingReasoner(_decision(IntentKind.casual, ResponseType.direct_answer, text="sure"))
    out = _run(conversation_runtime.handle(FakeChannel(),
        _reply_msg("r1", "what did this mean again", reply_to="totally-unknown-guid"),
        user_id=uid, reply_target=PHONE, reasoner=rec))
    assert out.status == "processed" and rec.called is False
    ob = _run(_outbound(uid))
    assert "resend" in ob[-1].text.lower()           # honest: can't pull that exact one up, resend it


def test_p0_reply_to_available_image_feeds_it_and_drops_recent_window(clean_db):
    uid = uuid4(); _run(_ensure_user(uid))
    _run(conversation_runtime.handle(FakeChannel(),
        _msg("imgB", attachments=[Attachment(kind=AttachmentKind.image, media_type="image/png",
                                              data=_PNG, filename="b.png")]),
        user_id=uid, reply_target=PHONE,
        reasoner=FakeReasoner(_decision(IntentKind.educational_help, ResponseType.tutoring, text="image b stuff"))))
    ref = _run(_stage_upload_rt(_PNG))
    env = {"resolution_source": "relay_exact_lookup", "referenced_direction": "inbound",
           "referenced_attachment_refs": [{"available": True, "upload_ref": str(ref), "media_type": "image/png"}]}
    rec = RecordingReasoner(_decision(IntentKind.educational_help, ResponseType.tutoring, text="answer about A"))
    out = _run(conversation_runtime.handle(FakeChannel(),
        _reply_msg("replyA", "what is this one", reply_to="guidA", reply_context=env),
        user_id=uid, reply_target=PHONE, reasoner=rec))
    assert out.status == "processed" and rec.called is True
    assert len(rec.images) == 1                       # the REFERENCED image A is fed to the vision pass
    assert "image b stuff" not in (rec.context or "") # the recent-turn window (holding B) is NOT dumped
    assert "No prior conversation" in (rec.context or "")


# D-INT-2 — _present is split into humanity-owned _style and trust-owned _apply_safety_gates

def test_dint2_present_split_composes_to_present_and_gates_guarantee_no_em_dash():
    from bruce_engine.conversation_style import ConversationStyleEngine
    rt = conversation_runtime._Runtime(reasoner=FakeReasoner(None), style=ConversationStyleEngine())
    d = _decision(IntentKind.casual, ResponseType.direct_answer, text="")
    src = "yeah — that works, want me to do it"
    styled = rt._style(src, decision=d, profile=None, channel="self_hosted_imessage")
    gated = rt._apply_safety_gates(styled, decision=d, channel="self_hosted_imessage")
    assert "—" not in gated                                        # trust gate guarantee
    # the split is behavior-preserving: _present == gates(style(...))
    assert rt._present(src, decision=d, profile=None, channel="self_hosted_imessage") == gated
