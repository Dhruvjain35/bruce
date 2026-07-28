"""An unlinked sender never reaches conversation intake — including when redemption fails.

The provenance audit established that every branch of the unlinked-sender path already returns, so this
closes the one route that did not: an exception escaping `redeem_link_code`, which would have propagated
out of `handle_inbound` and left the caller deciding what an unrecognised number is told.

It also pins the schema fact the audit corrected. `locked_until` belongs to `messaging_link_attempts`,
not to `account_link_codes` — a "fix" that added it to the wrong table would have created the drift it
was meant to repair, and nothing else in the suite would have noticed.
"""

from __future__ import annotations

import asyncio
import datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import messaging_inbound, messaging_store, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage

HANDLE = "+15550199"


@pytest.fixture(autouse=True)
def _pg(clean_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _msg(text: str, pmid: str = "pm-1"):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=HANDLE, text=text, attachments=[],
                          timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)


async def _counts():
    async with worker_session() as s:
        return {
            "missions": (await s.execute(select(func.count()).select_from(schema.Mission))).scalar(),
            "runs": (await s.execute(select(func.count()).select_from(schema.AgentRun))).scalar(),
            "identities": (await s.execute(
                select(func.count()).select_from(schema.MessagingIdentity))).scalar(),
            "turns": (await s.execute(
                select(func.count()).select_from(schema.ConversationTurn))).scalar(),
        }


# --- the gap this closes ------------------------------------------------------------------------------

def test_a_redemption_exception_never_becomes_generic_intake():
    """The one route that could fall through. A database or schema fault must produce a truthful
    temporary failure, not a conversation."""
    channel = FakeChannel()

    async def _boom(*a, **kw):
        raise RuntimeError("relation does not exist")

    with patch.object(messaging_store, "redeem_link_code", _boom):
        outcome = _run(messaging_inbound.handle_inbound(channel, _msg("ABC123")))

    assert outcome.status == "link_error"
    assert len(channel.sent) == 1
    assert channel.sent[0][1].text == messaging_inbound.LINK_TEMP_FAIL_TEXT


def test_a_redemption_exception_creates_no_work_of_any_kind():
    channel = FakeChannel()
    before = _run(_counts())

    async def _boom(*a, **kw):
        raise RuntimeError("boom")

    with patch.object(messaging_store, "redeem_link_code", _boom):
        _run(messaging_inbound.handle_inbound(channel, _msg("ABC123")))

    after = _run(_counts())
    assert after == before, f"a failed redemption created state: {before} -> {after}"


def test_the_temporary_failure_is_not_the_bad_code_copy():
    """Telling someone their code is invalid when the database was unreachable sends them to mint
    another one that fails identically, and hides an operator problem as a user explanation."""
    assert messaging_inbound.LINK_TEMP_FAIL_TEXT != messaging_inbound.BAD_CODE_TEXT
    assert "expired" not in messaging_inbound.LINK_TEMP_FAIL_TEXT
    assert "invalid" not in messaging_inbound.LINK_TEMP_FAIL_TEXT


def test_the_failure_reply_carries_no_message_content_or_handle():
    channel = FakeChannel()

    async def _boom(*a, **kw):
        raise RuntimeError("boom")

    with patch.object(messaging_store, "redeem_link_code", _boom):
        _run(messaging_inbound.handle_inbound(channel, _msg("ABC123")))
    body = channel.sent[0][1].text
    assert "ABC123" not in body and HANDLE not in body


# --- the branches that were already correct, pinned ---------------------------------------------------

def test_an_unlinked_sender_with_no_code_gets_the_prompt_and_nothing_else():
    channel = FakeChannel()
    before = _run(_counts())
    outcome = _run(messaging_inbound.handle_inbound(channel, _msg("hey whats up")))
    assert outcome.status == "unlinked_prompt"
    assert _run(_counts()) == before


def test_an_invalid_code_creates_no_identity():
    channel = FakeChannel()
    outcome = _run(messaging_inbound.handle_inbound(channel, _msg("ZZZZZZ")))
    assert outcome.status == "bad_code"
    assert _run(_counts())["identities"] == 0


def test_a_valid_code_links_exactly_one_identity_and_is_consumed_once():
    from bruce_engine.repositories import PostgresUserRepository

    uid = uuid4()
    _run(PostgresUserRepository().ensure(uid, auth_provider="test"))
    code, _exp = _run(messaging_store.create_link_code(uid, ChannelKind.self_hosted_imessage))

    channel = FakeChannel()
    first = _run(messaging_inbound.handle_inbound(channel, _msg(code, "pm-a")))
    assert first.status == "linked" and first.user_id == uid
    assert _run(_counts())["identities"] == 1

    # Replay of the SAME code is no longer valid — single use, and it must not mint a second identity.
    second = _run(messaging_inbound.handle_inbound(channel, _msg(code, "pm-b")))
    assert second.status != "linked"
    assert _run(_counts())["identities"] == 1


def test_an_expired_code_creates_no_identity():
    from bruce_engine.repositories import PostgresUserRepository

    uid = uuid4()
    _run(PostgresUserRepository().ensure(uid, auth_provider="test"))
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    code, _exp = _run(messaging_store.create_link_code(uid, ChannelKind.self_hosted_imessage,
                                                       now=past))
    outcome = _run(messaging_inbound.handle_inbound(FakeChannel(), _msg(code)))
    assert outcome.status == "bad_code"
    assert _run(_counts())["identities"] == 0


# --- the schema fact the audit corrected ---------------------------------------------------------------

def test_locked_until_belongs_to_link_attempts_not_link_codes():
    """A "fix" that added this column to `account_link_codes` would have CREATED the drift it was meant
    to repair, and the model would then disagree with the database in a new way."""
    assert "locked_until" in schema.MessagingLinkAttempt.__table__.c
    assert "locked_until" not in schema.AccountLinkCode.__table__.c


def test_both_link_table_models_match_their_migrations(pg_test_db):
    """Model and database agree, column for column, for both tables. Read from the live catalog rather
    than from Base metadata — the whole point is to catch the two disagreeing."""
    import sqlalchemy as sa

    async def _live(table):
        async with worker_session() as s:
            rows = (await s.execute(sa.text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table})).scalars().all()
            return set(rows)

    for model in (schema.AccountLinkCode, schema.MessagingLinkAttempt):
        declared = {c.name for c in model.__table__.c}
        live = _run(_live(model.__tablename__))
        assert declared == live, (
            f"{model.__tablename__} drift — model only: {declared - live}, db only: {live - declared}")
