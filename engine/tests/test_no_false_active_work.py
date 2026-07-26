"""Live-path regressions for the P0 that broke the phone acceptance.

WHAT ACTUALLY HAPPENED. E1's conversation-runtime enrollments were revoked, so
`conversation_access` denied, and `messaging_inbound` silently fell through to the legacy intake path.
Every message — "yo", "i'm saying yo", "email me and tell me when i reply" — was turned into an INTAKE
MISSION and answered with the constant ACK_TEXT ("gotchu, i've got it. give me a sec to look"). The
FastRouter, ToolBroker, planner and mission_planner never ran at all: they live inside the runtime,
behind that gate. The system claimed active work on missions it had manufactured out of a greeting.

THE INVARIANT: a claim of active work must be TRUE. These tests pin the fallback's behaviour when the
runtime is unavailable, because that is the state the system silently degraded into for three days.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import access_control, messaging_inbound, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.messaging_inbound import (ACK_TEXT, NO_RUNTIME_CHATTER_TEXT, NO_RUNTIME_GOAL_TEXT,
                                            handle_inbound)

_phone_seq = iter(range(6000, 6999))


def _fresh_phone() -> str:
    """uq_msg_identity binds a handle to one user, so each test needs its own."""
    return f"+1555765{next(_phone_seq)}"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _msg(*, mid, text=None, attachments=None, frm=None):
    return InboundMessage(provider_message_id=mid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=frm, text=text, attachments=attachments or [],
                          timestamp=datetime.datetime.now(datetime.timezone.utc))


async def _linked_user(phone=None):
    uid, phone = uuid.uuid4(), phone or _fresh_phone()
    async with user_session(uid) as s:
        s.add(schema.User(id=uid, auth_provider="apple"))
    async with worker_session() as s:
        s.add(schema.MessagingIdentity(user_id=uid, channel=ChannelKind.self_hosted_imessage.value,
                                       channel_identity=phone))
    return uid, phone


async def _mission_count(uid):
    async with user_session(uid) as s:
        return (await s.execute(select(func.count()).select_from(schema.Mission)
                                .where(schema.Mission.user_id == uid))).scalar_one()


async def _outbound_texts(uid):
    async with user_session(uid) as s:
        return list((await s.execute(
            select(schema.OutboundMessageRow.text).where(schema.OutboundMessageRow.user_id == uid)
        )).scalars().all())


def _deny(monkeypatch):
    """The exact state that broke production: linked user, runtime gate DENIES."""
    async def _no(_uid, _cap="conversation"):
        return access_control.Decision(False, "no_grant", "no active entitlement or live staging enrollment")
    monkeypatch.setattr(access_control, "conversation_access", _no)


# --- no false active-work claim ---------------------------------------------------------------------

def test_greeting_with_runtime_off_creates_no_mission_and_claims_no_work(monkeypatch):
    """The exact failing turn. "yo" must not become an intake mission, and Bruce must not say it is
    working on something."""
    _deny(monkeypatch)

    async def go():
        uid, phone = await _linked_user()
        ch = FakeChannel()
        out = await handle_inbound(ch, _msg(mid="p0-yo", text="yo", frm=phone))
        assert out.status == "no_runtime"
        assert await _mission_count(uid) == 0, "a greeting must never manufacture an intake mission"
        texts = await _outbound_texts(uid)
        assert texts == [NO_RUNTIME_CHATTER_TEXT]
        assert ACK_TEXT not in texts, "no claim of active work without work"
        assert "give me a sec" not in " ".join(texts)
    asyncio.run(go())


def test_curly_apostrophe_greeting_is_still_chatter(monkeypatch):
    """Regression on the real message: iMessage sent a CURLY apostrophe, and a straight-quote-only
    pattern would have let "i'm saying yo" through to intake."""
    _deny(monkeypatch)

    async def go():
        uid, phone = await _linked_user()
        out = await handle_inbound(FakeChannel(), _msg(mid="p0-curly", text="i’m saying yo", frm=phone))
        assert out.status == "no_runtime"
        assert await _mission_count(uid) == 0
    asyncio.run(go())


def test_gmail_goal_with_runtime_off_names_the_blocker_and_creates_no_mission(monkeypatch):
    """"email me and tell me when i reply" must never be answered with a cheerful ack for work that will
    never happen. The generic fallback must decline an executable Gmail goal."""
    _deny(monkeypatch)

    async def go():
        uid, phone = await _linked_user()
        out = await handle_inbound(FakeChannel(), _msg(mid="p0-goal", text="email me and tell me when i reply", frm=phone))
        assert out.status == "no_runtime"
        assert out.mission_status == "executable_goal"
        assert await _mission_count(uid) == 0, "an executable goal must not become an intake mission"
        assert await _outbound_texts(uid) == [NO_RUNTIME_GOAL_TEXT]
    asyncio.run(go())


def test_ack_text_is_only_sent_when_a_real_mission_exists(monkeypatch):
    """The fallback still WORKS for what it is for: a real note becomes a real intake mission, and only
    then is the 'i've got it' claim true."""
    _deny(monkeypatch)

    async def go():
        uid, phone = await _linked_user()
        out = await handle_inbound(FakeChannel(), _msg(mid="p0-note", text="applications for the summer program are due may 1 2026", frm=phone))
        assert out.status == "processed" and out.mission_id is not None
        assert await _mission_count(uid) == 1
        assert await _outbound_texts(uid) == [ACK_TEXT]      # true claim: a mission really exists
    asyncio.run(go())


def test_duplicate_inbound_does_not_duplicate_mission_or_acknowledgement(monkeypatch):
    _deny(monkeypatch)

    async def go():
        uid, phone = await _linked_user()
        ch = FakeChannel()
        m = _msg(mid="p0-dupe", text="chem test moved to friday the 12th", frm=phone)
        first = await handle_inbound(ch, m)
        second = await handle_inbound(ch, m)          # same provider message id = redelivery
        assert first.status == "processed" and second.status == "duplicate"
        assert await _mission_count(uid) == 1
        assert await _outbound_texts(uid) == [ACK_TEXT]
    asyncio.run(go())


def test_repeated_chatter_never_repeats_a_work_claim(monkeypatch):
    """Three unrelated conversational turns produced three identical 'i've got it' claims in production.
    They may produce an honest reply each, but never a claim of active work."""
    _deny(monkeypatch)

    async def go():
        uid, phone = await _linked_user()
        ch = FakeChannel()
        for i, t in enumerate(["yo", "hey", "what's up"]):
            await handle_inbound(ch, _msg(mid=f"p0-rep-{i}", text=t, frm=phone))
        texts = await _outbound_texts(uid)
        assert ACK_TEXT not in texts
        assert await _mission_count(uid) == 0
    asyncio.run(go())


# --- the gate itself must be observable -------------------------------------------------------------

def test_denied_access_is_logged_with_its_reason(monkeypatch, caplog):
    """A lapsed enrollment degraded the whole product in silence for three days. The denial must be
    visible in the logs with its source and reason."""
    _deny(monkeypatch)

    async def go():
        _uid, phone = await _linked_user()
        with caplog.at_level("WARNING"):
            await handle_inbound(FakeChannel(), _msg(mid="p0-log", text="yo", frm=phone))
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "conversation_runtime_unavailable" in joined
        assert "no_grant" in joined
    asyncio.run(go())


# --- routing invariants, asserted where they are actually decided -----------------------------------

def test_greeting_routes_to_fast_conversation_not_a_mission():
    """With the runtime ON, a greeting is conversation. Asserted on the router's own classification so it
    holds without a live turn."""
    from bruce_engine import fast_router, mission_planner
    assert not fast_router._SEND_INTENT.search("yo")
    assert not mission_planner.is_enqueueable(None)


def test_the_failing_phrase_is_a_background_gmail_mission():
    from bruce_engine import fast_router
    text = "email me and tell me when i reply"
    assert fast_router._SEND_INTENT.search(text) and fast_router._FOLLOWUP.search(text)


def test_unrelated_messages_do_not_look_like_missions():
    from bruce_engine import fast_router
    for t in ("yo", "what's up", "chem test moved to friday", "thanks"):
        assert not fast_router._SEND_INTENT.search(t), t
