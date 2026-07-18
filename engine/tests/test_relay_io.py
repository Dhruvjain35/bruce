"""Steps 2+3 — relay inbound endpoint + durable outbound queue, against REAL Postgres.

Covers: device-authenticated inbound (echo-ignore, group reply target, routes into the existing
mission flow), the outbound queue state machine (claim/lease/sent/retry/terminal, SKIP-LOCKED dedup),
and the relay claim/ack/heartbeat endpoints. Live iMessage is NOT exercised (no Mac). Skips when
Postgres isn't configured (pg_test_db).
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import messaging_outbound, relay_auth, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind

client = TestClient(api.app)
PHONE = "+15550001111"
GROUP = "iMessage;+;chat0000"


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


def _device():
    return asyncio.run(relay_auth.register_device("mac-test"))


def _hdrs(secret):
    return {"Authorization": f"Bearer {secret}", "X-Bruce-Timestamp": _now().isoformat(),
            "X-Bruce-Nonce": uuid4().hex, "X-Bruce-Request-Id": uuid4().hex}


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="apple"))


async def _link(uid, handle=PHONE):
    async with worker_session() as s:
        s.add(schema.MessagingIdentity(user_id=uid, channel=ChannelKind.self_hosted_imessage.value, channel_identity=handle))


# --------------------------------------------------------------------------- auth on the boundary


def test_inbound_requires_a_valid_device_credential(clean_db):
    assert client.post("/v1/relay/inbound", json={"provider_message_id": "m1", "channel_identity": PHONE}).status_code == 401
    assert client.post("/v1/relay/inbound", json={"provider_message_id": "m1", "channel_identity": PHONE},
                       headers={"Authorization": "Bearer nope", "X-Bruce-Timestamp": _now().isoformat()}).status_code == 401


def test_revoked_device_is_rejected(clean_db):
    dev_id, secret = _device()
    asyncio.run(relay_auth.revoke_device(dev_id))
    r = client.post("/v1/relay/heartbeat", headers=_hdrs(secret))
    assert r.status_code == 401


def test_stale_timestamp_is_rejected(clean_db):
    _, secret = _device()
    hdrs = {"Authorization": f"Bearer {secret}", "X-Bruce-Timestamp": (_now() - datetime.timedelta(hours=1)).isoformat()}
    assert client.post("/v1/relay/heartbeat", headers=hdrs).status_code == 401


# --------------------------------------------------------------------------- inbound


def test_echo_from_bruce_itself_is_ignored(clean_db):
    _, secret = _device()
    r = client.post("/v1/relay/inbound", headers=_hdrs(secret),
                    json={"provider_message_id": "e1", "channel_identity": PHONE, "is_from_me": True, "text": "hi"})
    assert r.status_code == 200 and r.json()["status"] == "ignored_echo"


def test_linked_inbound_creates_a_mission_and_queues_a_reply_to_the_chat(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); asyncio.run(_link(uid))
    _, secret = _device()
    r = client.post("/v1/relay/inbound", headers=_hdrs(secret), json={
        "provider_message_id": "g1", "channel_identity": PHONE, "chat_guid": GROUP, "is_group": True,
        "text": "Science fair March 14, 2026"})
    assert r.status_code == 200 and r.json()["status"] == "processed" and r.json()["mission_id"]

    async def _reply_target():
        async with user_session(uid) as s:
            outb = (await s.execute(select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.user_id == uid))).scalars().all()
        return outb
    outb = asyncio.run(_reply_target())
    assert len(outb) == 1 and outb[0].to_handle == GROUP and outb[0].kind == "acknowledged"


def test_duplicate_inbound_does_not_create_a_second_mission(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); asyncio.run(_link(uid))
    _, secret = _device()
    body = {"provider_message_id": "dup", "channel_identity": PHONE, "text": "May 1 2026"}
    a = client.post("/v1/relay/inbound", headers=_hdrs(secret), json=body).json()
    b = client.post("/v1/relay/inbound", headers=_hdrs(secret), json=body).json()
    assert a["status"] == "processed" and b["status"] == "duplicate" and a["mission_id"] == b["mission_id"]


# --------------------------------------------------------------------------- outbound queue


def _enqueue(uid, key="k1"):
    asyncio.run(messaging_outbound.enqueue(user_id=uid, to_handle=PHONE, channel=ChannelKind.self_hosted_imessage,
                                           kind="acknowledged", text="hi", idempotency_key=key))


def test_claim_then_sent_records_a_delivery_attempt(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); _enqueue(uid)
    dev_id, secret = _device()
    c = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret)).json()
    assert c["to"] == PHONE and c["text"] == "hi"
    r = client.post(f"/v1/relay/outbound/{c['id']}/ack", headers=_hdrs(secret), json={"status": "sent", "provider_message_id": "p1"})
    assert r.status_code == 200

    async def _state():
        async with worker_session() as s:
            row = (await s.execute(select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.id == UUID(c["id"])))).scalar_one()
            att = (await s.execute(select(func.count()).select_from(schema.DeliveryAttempt).where(schema.DeliveryAttempt.outbound_message_id == UUID(c["id"])))).scalar_one()
        return row.status, att
    assert asyncio.run(_state()) == ("sent", 1)


def test_empty_queue_returns_204(clean_db):
    _, secret = _device()
    assert client.post("/v1/relay/outbound/claim", headers=_hdrs(secret)).status_code == 204


def test_second_poller_does_not_claim_the_same_message(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); _enqueue(uid)
    _, secret = _device()
    first = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret))
    second = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret))
    assert first.status_code == 200 and second.status_code == 204   # lease held -> only one claim


def test_terminal_failure_is_not_retried(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); _enqueue(uid)
    _, secret = _device()
    c = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret)).json()
    client.post(f"/v1/relay/outbound/{c['id']}/ack", headers=_hdrs(secret), json={"status": "terminal_failed", "error": "not on iMessage"})

    async def _status():
        async with worker_session() as s:
            return (await s.execute(select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.id == UUID(c["id"])))).scalar_one().status
    assert asyncio.run(_status()) == "terminal_failed"
    # and it is not reclaimable
    assert client.post("/v1/relay/outbound/claim", headers=_hdrs(secret)).status_code == 204


def test_expired_lease_is_reclaimed(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); _enqueue(uid)
    dev_id, secret = _device()
    c = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret)).json()

    async def _expire():
        from sqlalchemy import text as sa_text
        async with worker_session() as s:
            await s.execute(sa_text("UPDATE outbound_messages SET lease_expires_at = now() - interval '1 hour' WHERE id=:i"), {"i": c["id"]})
    asyncio.run(_expire())
    again = client.post("/v1/relay/outbound/claim", headers=_hdrs(secret))
    assert again.status_code == 200 and again.json()["id"] == c["id"] and again.json()["attempts"] == 2
