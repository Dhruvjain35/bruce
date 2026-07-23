"""P0 — the universal outbound channel-safety gate (integration invariant 7: no bypasses).

Every student-facing outbound passes through gate_outbound_text inside enqueue(), so no path — the
conversation runtime, a legacy ACK, an error, a status update — can ship an em dash or a corporate filler
phrase to a plain-text channel. Also pins the rewritten legacy constants (no more 'Got it — I'm
understanding this now').
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import messaging_inbound, messaging_outbound, schema
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind

import pytest

EM = "—"
PHONE = "+15557654321"
_LEGACY_CONSTANTS = [
    messaging_inbound.ACK_TEXT, messaging_inbound.LINK_PROMPT, messaging_inbound.LINKED_TEXT,
    messaging_inbound.BAD_CODE_TEXT, messaging_inbound.RATE_LIMITED_TEXT,
]


def test_gate_strips_em_dash_and_corporate_on_plain_channels():
    out = messaging_outbound.gate_outbound_text("i'd be happy to help — got it", "self_hosted_imessage")
    assert EM not in out and "i'd be happy to" not in out.lower()


def test_gate_is_idempotent_and_leaves_non_plain_channels_alone():
    once = messaging_outbound.gate_outbound_text("a — b", "self_hosted_imessage")
    assert messaging_outbound.gate_outbound_text(once, "self_hosted_imessage") == once and EM not in once
    # a non-plain channel (hypothetical rich channel) is passed through unchanged
    assert messaging_outbound.gate_outbound_text("a — b", "rich_web") == "a — b"


def test_legacy_constants_have_no_em_dash_and_survive_the_gate_unchanged():
    for c in _LEGACY_CONSTANTS:
        assert EM not in c                                            # rewritten out at the source
        assert messaging_outbound.gate_outbound_text(c, "self_hosted_imessage") == c   # already gate-clean


def test_old_canned_ack_is_gone():
    assert "understanding this now" not in messaging_inbound.ACK_TEXT.lower()
    assert "i'll message you when it needs review" not in messaging_inbound.ACK_TEXT.lower()
    assert messaging_inbound.ACK_TEXT == messaging_inbound.ACK_TEXT.lower() or "👀" in messaging_inbound.ACK_TEXT


# --- real-PG: enqueue gates EVERY caller (no bypass) ----------------------------------------------

@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _mk_user():
    uid = uuid4()
    async with user_session(uid) as s:
        s.add(schema.User(id=uid, auth_provider="apple"))
    return uid


def test_enqueue_gates_an_em_dash_text_before_persisting(clean_db):
    uid = _run(_mk_user())

    async def go():
        await messaging_outbound.enqueue(
            user_id=uid, to_handle=PHONE, channel=ChannelKind.self_hosted_imessage,
            kind="acknowledged", text="here's the plan — bring a pen", idempotency_key="g1")
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.OutboundMessageRow).where(
                schema.OutboundMessageRow.idempotency_key == "g1"))).scalar_one()
            return row.text
    stored = _run(go())
    assert EM not in stored                                          # gated at the enqueue boundary
