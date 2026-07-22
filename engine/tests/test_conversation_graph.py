"""Bite 2 A2 — ConversationContextGraph persistence against REAL Postgres (RLS + functional).

Exercises the tenant_or_worker FORCE-RLS isolation and the graph semantics: idempotent canonical
messages, reply/thread edges, resolve-later reconciliation (never across owners), reaction add/remove/
unknown, unsent/edit marking, account-deletion cascade. Skips when Postgres isn't configured (the
offline suite runs Postgres-free); CI provides a real PG16 with the restricted bruce_app role so RLS is
genuinely enforced.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import conversation_graph, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind, InboundMessage

import pytest

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


async def _user(handle):
    uid = uuid4()
    async with user_session(uid) as s:
        s.add(schema.User(id=uid, auth_provider="apple"))
    async with worker_session() as s:
        s.add(schema.MessagingIdentity(user_id=uid, channel=PROV, channel_identity=handle))
    return uid, handle


def _msg(uid, handle, pmid, **kw):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=handle, user_id=uid, timestamp=_now(),
                          reply_to_message_id=kw.get("reply_to"),
                          thread_root_message_id=kw.get("thread_root"),
                          thread_id=kw.get("chat"), service=kw.get("service"))


async def _count(model, uid=None):
    ctx = user_session(uid) if uid else worker_session()
    async with ctx as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar()


def _run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------- functional

def test_ordinary_inbound_persisted(clean_db):
    async def go():
        uid, h = await _user("+1a")
        mid = await conversation_graph.ingest_inbound_message(_msg(uid, h, "m1", chat="chatA", service="iMessage"))
        assert mid is not None
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.ConversationMessage).where(schema.ConversationMessage.id == mid))).scalar_one()
            assert row.direction == "inbound" and row.provider_chat_id == "chatA" and row.service == "iMessage"
            assert row.sender_identity_id is not None
    _run(go())


def test_duplicate_message_delivery_is_idempotent(clean_db):
    async def go():
        uid, h = await _user("+1b")
        a = await conversation_graph.ingest_inbound_message(_msg(uid, h, "dup1"))
        b = await conversation_graph.ingest_inbound_message(_msg(uid, h, "dup1"))
        assert a == b
        assert await _count(schema.ConversationMessage, uid) == 1
    _run(go())


def test_reply_to_known_message_resolves_now(clean_db):
    async def go():
        uid, h = await _user("+1c")
        target = await conversation_graph.ingest_inbound_message(_msg(uid, h, "t1"))
        src = await conversation_graph.ingest_inbound_message(_msg(uid, h, "s1", reply_to="t1"))
        async with user_session(uid) as s:
            edge = (await s.execute(select(schema.ConversationMessageRelationship).where(
                schema.ConversationMessageRelationship.source_message_id == src))).scalar_one()
            assert edge.relationship_type == "reply_to" and edge.target_message_id == target
            assert edge.unresolved_target_provider_message_id is None
    _run(go())


def test_reply_target_arrives_after_source_reconciles(clean_db):
    async def go():
        uid, h = await _user("+1d")
        src = await conversation_graph.ingest_inbound_message(_msg(uid, h, "s2", reply_to="later"))
        async with user_session(uid) as s:
            edge = (await s.execute(select(schema.ConversationMessageRelationship))).scalar_one()
            assert edge.target_message_id is None and edge.unresolved_target_provider_message_id == "later"
        target = await conversation_graph.ingest_inbound_message(_msg(uid, h, "later"))
        async with user_session(uid) as s:
            edge = (await s.execute(select(schema.ConversationMessageRelationship))).scalar_one()
            assert edge.target_message_id == target and edge.unresolved_target_provider_message_id is None
    _run(go())


def test_thread_root_distinct_from_chat(clean_db):
    async def go():
        uid, h = await _user("+1e")
        root = await conversation_graph.ingest_inbound_message(_msg(uid, h, "root1"))
        src = await conversation_graph.ingest_inbound_message(_msg(uid, h, "s3", thread_root="root1", chat="chatX"))
        async with user_session(uid) as s:
            node = (await s.execute(select(schema.ConversationMessage).where(schema.ConversationMessage.id == src))).scalar_one()
            assert node.provider_chat_id == "chatX"       # chat identity
            edge = (await s.execute(select(schema.ConversationMessageRelationship).where(
                schema.ConversationMessageRelationship.relationship_type == "thread_root"))).scalar_one()
            assert edge.target_message_id == root         # thread root is a SEPARATE edge, not the chat
    _run(go())


def test_reaction_add_remove_unknown_and_idempotent(clean_db):
    async def go():
        uid, h = await _user("+1f")
        await conversation_graph.ingest_inbound_message(_msg(uid, h, "tgt1"))
        await conversation_graph.record_reaction(uid, provider=PROV, provider_event_id="rx1",
                                                 reaction_type="love", removed=False, channel_identity=h,
                                                 target_provider_message_id="tgt1")
        await conversation_graph.record_reaction(uid, provider=PROV, provider_event_id="rx1",
                                                 reaction_type="love", removed=False, channel_identity=h,
                                                 target_provider_message_id="tgt1")  # duplicate
        await conversation_graph.record_reaction(uid, provider=PROV, provider_event_id="rx2",
                                                 reaction_type="love", removed=True, channel_identity=h,
                                                 target_provider_message_id="tgt1")  # removal = own event
        await conversation_graph.record_reaction(uid, provider=PROV, provider_event_id="rx3",
                                                 reaction_type="unknown", removed=False, channel_identity=h)
        async with user_session(uid) as s:
            rows = (await s.execute(select(schema.ConversationReactionEvent))).scalars().all()
            assert len(rows) == 3                          # rx1 deduped, rx2 removal, rx3 unknown
            assert {r.reaction_type for r in rows} == {"love", "unknown"}
            assert any(r.removed for r in rows) and any(r.reaction_type == "unknown" for r in rows)
    _run(go())


def test_unsent_and_edit_mark_message(clean_db):
    async def go():
        uid, h = await _user("+1g")
        await conversation_graph.ingest_inbound_message(_msg(uid, h, "u1"))
        await conversation_graph.mark_unsent(uid, provider=PROV, provider_message_id="u1")
        await conversation_graph.mark_edited(uid, provider=PROV, provider_message_id="u1")
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.ConversationMessage))).scalar_one()
            assert row.unsent_at is not None and row.edited_at is not None
    _run(go())


def test_outbound_message_persisted(clean_db):
    async def go():
        uid, h = await _user("+1h")
        mid = await conversation_graph.ingest_outbound_message(uid, provider=PROV, provider_message_id="ob1",
                                                               provider_chat_id="chatA", outbound_message_id=None)
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.ConversationMessage).where(schema.ConversationMessage.id == mid))).scalar_one()
            assert row.direction == "outbound"
    _run(go())


# ------------------------------------------------------------------- RLS isolation

def test_user_cannot_read_another_users_message(clean_db):
    async def go():
        a, ha = await _user("+2a")
        b, hb = await _user("+2b")
        await conversation_graph.ingest_inbound_message(_msg(a, ha, "am"))
        await conversation_graph.ingest_inbound_message(_msg(b, hb, "bm"))
        assert await _count(schema.ConversationMessage, a) == 1     # A sees only A's
        assert await _count(schema.ConversationMessage, b) == 1     # B sees only B's
        assert await _count(schema.ConversationMessage) == 2        # worker sees both
    _run(go())


def test_user_cannot_read_another_users_relationships_or_reactions(clean_db):
    async def go():
        a, ha = await _user("+2c")
        b, hb = await _user("+2d")
        await conversation_graph.ingest_inbound_message(_msg(a, ha, "at"))
        await conversation_graph.ingest_inbound_message(_msg(a, ha, "as", reply_to="at"))
        await conversation_graph.record_reaction(a, provider=PROV, provider_event_id="arx",
                                                 reaction_type="like", removed=False, channel_identity=ha,
                                                 target_provider_message_id="at")
        assert await _count(schema.ConversationMessageRelationship, b) == 0
        assert await _count(schema.ConversationReactionEvent, b) == 0
        assert await _count(schema.ConversationMessageRelationship, a) == 1
        assert await _count(schema.ConversationReactionEvent, a) == 1
    _run(go())


def test_unresolved_edge_never_reconciles_to_another_owner(clean_db):
    async def go():
        a, ha = await _user("+2e")
        b, hb = await _user("+2f")
        await conversation_graph.ingest_inbound_message(_msg(a, ha, "asrc", reply_to="shared_guid"))
        # B ingests a message with the SAME provider guid target — must NOT link A's edge
        await conversation_graph.ingest_inbound_message(_msg(b, hb, "shared_guid"))
        async with user_session(a) as s:
            edge = (await s.execute(select(schema.ConversationMessageRelationship))).scalar_one()
            assert edge.target_message_id is None and edge.unresolved_target_provider_message_id == "shared_guid"
    _run(go())


def test_resolve_owner_only_for_linked_identity(clean_db):
    async def go():
        a, ha = await _user("+2g")
        assert await conversation_graph.resolve_owner(PROV, ha) == a
        assert await conversation_graph.resolve_owner(PROV, "+never-linked") is None
    _run(go())


def test_force_rls_enabled_on_every_graph_table(clean_db):
    async def go():
        async with worker_session() as s:
            for t in ("conversation_messages", "conversation_message_relationships",
                      "conversation_message_attachments", "conversation_reaction_events"):
                forced = (await s.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE relname=:t"), {"t": t})).scalar()
                assert forced is True, f"{t} is not FORCE RLS"
    _run(go())


def test_account_deletion_cascades_graph(clean_db):
    async def go():
        a, ha = await _user("+2h")
        await conversation_graph.ingest_inbound_message(_msg(a, ha, "dm", reply_to="x"))
        await conversation_graph.record_reaction(a, provider=PROV, provider_event_id="drx",
                                                 reaction_type="like", removed=False, channel_identity=ha)
        assert await _count(schema.ConversationMessage) == 1
        async with user_session(a) as s:                 # user deletes own account
            await s.execute(text("DELETE FROM users WHERE id = app_current_user()"))
        assert await _count(schema.ConversationMessage) == 0
        assert await _count(schema.ConversationMessageRelationship) == 0
        assert await _count(schema.ConversationReactionEvent) == 0
    _run(go())
