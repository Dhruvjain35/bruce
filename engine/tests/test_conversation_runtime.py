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
    img = Attachment(kind=AttachmentKind.image, media_type="image/png", data=b"\x89PNG")
    out = _run(conversation_runtime.handle(FakeChannel(), _msg("f1", text="?", attachments=[img]),
                                           user_id=uid, reply_target=PHONE, reasoner=FakeReasoner(raises=True)))
    assert out.status == "model_error"
    ob = _run(_outbound(uid))
    assert len(ob) == 1 and "resend" in ob[0].text.lower()      # honest, never a fabricated read


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


def test_flag_off_falls_through_to_legacy(monkeypatch):
    monkeypatch.delenv("BRUCE_CONVERSATION_RUNTIME", raising=False)
    assert conversation_runtime.enabled_for(PHONE) is False
    monkeypatch.setenv("BRUCE_CONVERSATION_RUNTIME", "1")
    monkeypatch.setenv("BRUCE_CONVERSATION_TEST_HANDLES", "+1999,%s" % PHONE)
    assert conversation_runtime.enabled_for(PHONE) is True
    assert conversation_runtime.enabled_for("+15550000000") is False   # not allow-listed
