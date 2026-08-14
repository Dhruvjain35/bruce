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
    # A REGISTERED rich channel is passed through unchanged. This assertion previously used "rich_web",
    # a name that is not a ChannelKind member — so what it actually pinned was the UNKNOWN-channel
    # branch, i.e. "a channel nobody has classified is passed through ungated". That was the fail-open
    # defect the Phase 0 audit found, in the function whose own docstring calls itself the last-line
    # floor with "no bypasses". The INTENT here — a rich surface keeps its formatting — is unchanged and
    # still asserted; it is now demonstrated with a channel that is genuinely classified as rich.
    assert messaging_outbound.gate_outbound_text("a — b", "in_app") == "a — b"


def test_an_unclassified_channel_gets_the_strictest_treatment_not_the_loosest():
    """THE INVERSION, asserted directly. A channel that is unknown, empty, misspelled or simply not yet
    classified must be treated as PLAIN TEXT — the strictest option — never waved through.

    Being wrong in this direction costs a comma where an em dash was wanted. Being wrong in the other
    direction ships corporate filler and raw punctuation to a surface nobody reviewed, silently, on the
    day that surface is added."""
    for channel in ("rich_web", "totally_unknown", "", "future_surface_v2"):
        out = messaging_outbound.gate_outbound_text("I'd be happy to help — great question", channel)
        assert EM not in out, f"{channel!r} bypassed the em-dash rewrite"
        assert "i'd be happy to" not in out.lower(), f"{channel!r} bypassed the phrase strip"


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
