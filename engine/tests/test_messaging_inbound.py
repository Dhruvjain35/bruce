"""Phase 6 — inbound handoff against REAL Postgres: a texted item becomes the SAME durable intake.

Verifies the handoff (create source+mission+job via the existing intake), idempotency on the provider
message id, unlinked-sender handling, and the immediate (non-promising) acknowledgement. Uses
FakeChannel so no provider is needed. Skips when Postgres isn't configured (pg_test_db).
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import messaging_inbound, messaging_store, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import Attachment, AttachmentKind, ChannelKind, FakeChannel, InboundMessage
from bruce_engine.messaging_inbound import ACK_TEXT, LINK_PROMPT, handle_inbound

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


def _msg(*, mid="m1", text=None, attachments=None, frm=PHONE):
    return InboundMessage(provider_message_id=mid, channel=ChannelKind.self_hosted_imessage, channel_identity=frm,
                          text=text, attachments=attachments or [], timestamp=_now())


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="apple"))


async def _link(uid, phone=PHONE):
    """Directly bind an identity to a user (the app-side link flow is tested in Phase 5)."""
    async with worker_session() as s:
        s.add(schema.MessagingIdentity(user_id=uid, channel=ChannelKind.self_hosted_imessage.value, channel_identity=phone))


async def _mission_count(uid):
    async with user_session(uid) as s:
        return (await s.execute(select(func.count()).select_from(schema.Mission).where(schema.Mission.user_id == uid))).scalar_one()


def test_unlinked_sender_gets_a_link_prompt_and_no_intake(clean_db):
    ch = FakeChannel()
    out = asyncio.run(handle_inbound(ch, _msg(text="hi")))
    assert out.status == "unlinked_prompt" and out.mission_id is None
    assert ch.sent and ch.sent[-1][1].text == LINK_PROMPT


def test_unlinked_sender_texts_a_valid_code_and_links(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid))
    code, _ = asyncio.run(messaging_store.create_link_code(uid))
    ch = FakeChannel()
    out = asyncio.run(handle_inbound(ch, _msg(text=code)))
    assert out.status == "linked" and out.user_id == uid
    # now a follow-up flyer is processed as that user
    assert asyncio.run(handle_inbound(ch, _msg(mid="m2", text="Applications due May 1, 2026"))).status == "processed"


def test_bad_code_is_rejected_without_linking(clean_db):
    ch = FakeChannel()
    out = asyncio.run(handle_inbound(ch, _msg(text="ZZZZZZ")))
    assert out.status == "bad_code" and out.user_id is None


def test_private_alpha_copy_never_references_a_nonexistent_app():
    """Requirement 1: the unlinked/bad-code copy must NOT reference an iPhone app or profile screen
    that doesn't exist yet."""
    from bruce_engine.messaging_inbound import LINK_PROMPT, BAD_CODE_TEXT, RATE_LIMITED_TEXT
    for t in (LINK_PROMPT, BAD_CODE_TEXT, RATE_LIMITED_TEXT):
        low = t.lower()
        for banned in ("app", "profile", "open bruce", "download", "tap your"):
            assert banned not in low, f"copy references '{banned}': {t!r}"
    assert "alpha" in LINK_PROMPT.lower()          # explicitly a private-alpha bridge


def test_rate_limited_handle_gets_generic_reply(clean_db):
    """Requirement 5/2: a brute-forced handle gets a generic rate-limit reply, not account info."""
    from bruce_engine.messaging_store import LINK_ATTEMPT_MAX
    from bruce_engine.messaging_inbound import RATE_LIMITED_TEXT
    ch = FakeChannel()
    for i in range(LINK_ATTEMPT_MAX):
        asyncio.run(handle_inbound(ch, _msg(mid=f"bf{i}", text=f"WRONG{i}")))
    out = asyncio.run(handle_inbound(ch, _msg(mid="bf-final", text="ABCDEF")))
    assert out.status == "rate_limited"
    assert ch.sent[-1][1].text == RATE_LIMITED_TEXT


def test_linked_flyer_becomes_a_durable_mission_with_ack_and_lineage(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); asyncio.run(_link(uid))
    ch = FakeChannel()
    att = Attachment(kind=AttachmentKind.image, media_type="image/png", data=b"\x89PNG\r\n\x1a\n", filename="flyer.png")
    out = asyncio.run(handle_inbound(ch, _msg(mid="flyer1", attachments=[att])))
    assert out.status == "processed" and out.mission_id is not None
    # exactly one mission, ack sent (and NOT a success promise), lineage persisted
    assert asyncio.run(_mission_count(uid)) == 1
    assert ch.sent[-1][1].text == ACK_TEXT and "found" not in ACK_TEXT.lower()

    async def _lineage():
        async with user_session(uid) as s:
            inb = (await s.execute(select(schema.InboundMessageRow).where(schema.InboundMessageRow.user_id == uid))).scalar_one()
            atts = (await s.execute(select(schema.MessageAttachment).where(schema.MessageAttachment.inbound_message_id == inb.id))).scalars().all()
            outb = (await s.execute(select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.user_id == uid))).scalars().all()
        return inb, atts, outb
    inb, atts, outb = asyncio.run(_lineage())
    assert inb.source_id is not None and inb.mission_id == out.mission_id
    assert len(atts) == 1 and atts[0].source_id == inb.source_id
    assert len(outb) == 1 and outb[0].kind == "acknowledged"


def test_redelivered_message_does_not_create_a_second_mission(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); asyncio.run(_link(uid))
    ch = FakeChannel()
    m = _msg(mid="dup1", text="Science fair March 14, 2026")
    a = asyncio.run(handle_inbound(ch, m))
    b = asyncio.run(handle_inbound(ch, m))   # webhook redelivery, same provider_message_id
    assert a.status == "processed" and b.status == "duplicate"
    assert a.mission_id == b.mission_id
    assert asyncio.run(_mission_count(uid)) == 1


def test_blocked_identity_is_ignored(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid))

    async def _blocked():
        async with worker_session() as s:
            s.add(schema.MessagingIdentity(user_id=uid, channel=ChannelKind.self_hosted_imessage.value,
                                           channel_identity=PHONE, blocked_at=_now()))
    asyncio.run(_blocked())
    out = asyncio.run(handle_inbound(FakeChannel(), _msg(text="hello")))
    assert out.status == "blocked"
    assert asyncio.run(_mission_count(uid)) == 0
