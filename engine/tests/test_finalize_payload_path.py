"""THE CALL SITE: `_finalize` must send an approved payload down the payload path, not the voice path.

`consequential_payload` established the boundary as a type and `messaging_outbound.enqueue` grew a
`payload=` seam that appends the exact bytes AFTER the voice gate has run on Bruce's own words. Until
`_finalize` uses that seam, none of it is live: the proposal still reaches the student as one gated
string, so the draft is styled on its way to the screen and the bytes the student approves are not the
bytes that were composed.

WHAT THIS SUITE PINS, and each assertion is one of the four properties that make the boundary real:

  1. the payload reaches `enqueue` as an ApprovedConsequentialPayload, not spliced into `text`
  2. `gate_outbound_text` is NEVER handed the payload, in any form
  3. Bruce's surrounding conversational text still goes through it, in full
  4. the rendered bytes are byte-identical to the frozen payload

Property 2 is asserted by RECORDING every argument the gate receives, rather than by checking the
output. Checking the output would pass if the payload went through a gate that happened not to change
it — a body with no em dash and no filler survives the gate unchanged, so an output check would go green
on a draft that got lucky and red only on the ones that mattered.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import conversation_runtime, messaging_outbound, schema
from bruce_engine.consequential_payload import ApprovedConsequentialPayload
from bruce_engine.conversation_style import PROHIBITED_PHRASES
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind

PHONE = "+15557654321"
PROHIBITED = "I'd be happy to"
assert any(p in PROHIBITED.lower() for p in PROHIBITED_PHRASES)

BODY = (f"Hi Professor Chen,\n\n{PROHIBITED} talk about the extension — Tuesday or Wednesday "
        f"works for me.\n\nThanks,\nSam")
SUBJECT = "Extension request — CS 121"
BRUCE_SAYS = f"{PROHIBITED} send this — want me to?"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _payload() -> ApprovedConsequentialPayload:
    return ApprovedConsequentialPayload(capability="gmail.send_message",
                                        fields={"subject": SUBJECT, "body": BODY})


async def _ensure_user(uid):
    async with user_session(uid) as s:
        s.add(schema.User(id=uid, auth_provider="alpha_bridge"))


async def _outbound_text(uid) -> str:
    async with user_session(uid) as s:
        rows = (await s.execute(select(schema.OutboundMessageRow).where(
            schema.OutboundMessageRow.user_id == uid))).scalars().all()
    assert len(rows) == 1, f"expected exactly one outbound, got {len(rows)}"
    return rows[0].text


def _finalize_with_payload(monkeypatch, payload):
    """Drive the REAL _finalize and record everything the voice gate is handed."""
    seen: list = []
    real_gate = messaging_outbound.gate_outbound_text

    def _spy(text, channel_value):
        seen.append(text)
        return real_gate(text, channel_value)

    monkeypatch.setattr(messaging_outbound, "gate_outbound_text", _spy)

    uid = uuid4()
    pmid = f"fin-{uuid4().hex[:8]}"

    async def _go():
        await _ensure_user(uid)
        rt = conversation_runtime._Runtime()
        await rt._finalize(uid, ChannelKind.self_hosted_imessage.value, PHONE, pmid,
                           BRUCE_SAYS, PHONE, decision=None, intent="casual", payload=payload)
        return await _outbound_text(uid)

    return uid, seen, asyncio.run(_go())


def test_the_payload_never_enters_the_voice_gate(clean_db, monkeypatch):
    """PROPERTY 2, asserted on what the gate RECEIVED rather than on what it returned."""
    payload = _payload()
    _uid, seen, _text = _finalize_with_payload(monkeypatch, payload)

    assert seen, "the voice gate was never called at all — Bruce's own words must still go through it"
    for handed in seen:
        assert not isinstance(handed, ApprovedConsequentialPayload), (
            "the payload object was handed to the voice gate")
        if isinstance(handed, str):
            assert BODY not in handed, "the payload body was spliced into text and then gated"
            assert SUBJECT not in handed, "the payload subject was spliced into text and then gated"


def test_bruce_conversational_text_still_goes_through_the_gate(clean_db, monkeypatch):
    """PROPERTY 3. Nothing here is a loosening: Bruce's half is still fully governed."""
    _uid, seen, text = _finalize_with_payload(monkeypatch, _payload())

    assert BRUCE_SAYS in seen, "Bruce's own reply was not handed to the voice gate"
    bruce_part = text.split(SUBJECT)[0]
    assert PROHIBITED.lower() not in bruce_part.lower(), (
        f"the prohibited phrase survived in BRUCE'S OWN words: {bruce_part!r}")
    assert "—" not in bruce_part


def test_the_rendered_bytes_equal_the_frozen_payload_exactly(clean_db, monkeypatch):
    """PROPERTIES 1 AND 4. What the student reads is byte-identical to what was frozen and what will be
    sent — including the em dash and the phrase Bruce may not say."""
    payload = _payload()
    _uid, _seen, text = _finalize_with_payload(monkeypatch, payload)

    for name, value in payload.for_execution().items():
        assert value in text, f"the rendered {name} is not byte-identical to the frozen payload"
    assert PROHIBITED in text
    assert text.count("—") >= 2, "the payload's em dashes were rewritten on the way to the student"


def test_paragraph_breaks_in_the_payload_survive(clean_db, monkeypatch):
    _uid, _seen, text = _finalize_with_payload(monkeypatch, _payload())
    assert "Hi Professor Chen,\n\n" in text
    assert "\n\nThanks,\nSam" in text


def test_a_turn_with_no_payload_is_unchanged(clean_db, monkeypatch):
    """The overwhelmingly common turn. A default of None must not alter ordinary replies at all."""
    _uid, seen, text = _finalize_with_payload(monkeypatch, None)

    assert BRUCE_SAYS in seen
    assert PROHIBITED.lower() not in text.lower()
    assert "—" not in text
    assert text == messaging_outbound.gate_outbound_text(
        BRUCE_SAYS, ChannelKind.self_hosted_imessage.value)


def test_exactly_one_outbound_is_still_enqueued(clean_db, monkeypatch):
    """The payload is part of the SAME message, not a second one — one turn, one reply."""
    uid, _seen, _text = _finalize_with_payload(monkeypatch, _payload())

    async def _count():
        async with user_session(uid) as s:
            return len((await s.execute(select(schema.OutboundMessageRow).where(
                schema.OutboundMessageRow.user_id == uid))).scalars().all())

    assert asyncio.run(_count()) == 1
