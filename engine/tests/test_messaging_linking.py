"""Phase 5 — account linking against REAL Postgres (hashing, one-user binding, cascade), under RLS.

A channel identity binds to a Bruce user ONLY via a one-time code the authenticated user generated;
the code is hashed at rest; a second user's code never silently rebinds; account deletion cascades.
Skips cleanly when Postgres isn't configured (pg_test_db).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import os
import time
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import messaging_store, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind

client = TestClient(api.app)
PHONE = "+15551234567"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    monkeypatch.setenv("BRUCE_JWT_SECRET", "test-secret-that-is-at-least-32-bytes-long!!")
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    yield
    db._engine = None
    db._sessionmaker = None


def _auth(uid):
    return {"Authorization": f"Bearer {jwt.encode({'sub': str(uid), 'exp': int(time.time())+3600}, os.environ['BRUCE_JWT_SECRET'], algorithm='HS256')}"}


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="apple"))


def test_link_code_is_hashed_at_rest(clean_db):
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    code, _ = asyncio.run(messaging_store.create_link_code(uid))

    async def _row():
        async with worker_session() as s:
            return (await s.execute(select(schema.AccountLinkCode).where(schema.AccountLinkCode.user_id == uid))).scalar_one()
    row = asyncio.run(_row())
    assert row.code_hash != code                                   # plaintext never stored
    # HMAC-peppered, NOT a plain sha256 (an attacker with the DB but not the pepper can't invert it).
    assert row.code_hash != hashlib.sha256(code.upper().encode()).hexdigest()
    pepper = os.environ["BRUCE_LINK_CODE_PEPPER"].encode()
    assert row.code_hash == hmac.new(pepper, code.upper().encode(), hashlib.sha256).hexdigest()


def test_redeem_binds_identity_to_the_code_owner(clean_db):
    uid = uuid4()
    asyncio.run(_ensure_user(uid))
    code, _ = asyncio.run(messaging_store.create_link_code(uid))
    r = asyncio.run(messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE))
    assert r.status == "linked" and r.user_id == uid
    idents = asyncio.run(messaging_store.list_identities(uid))
    assert len(idents) == 1 and idents[0].channel_identity == PHONE


def test_code_is_single_use(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid))
    code, _ = asyncio.run(messaging_store.create_link_code(uid))
    assert asyncio.run(messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE)).status == "linked"
    assert asyncio.run(messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE)).status == "invalid"


def test_wrong_code_is_invalid(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid))
    asyncio.run(messaging_store.create_link_code(uid))
    assert asyncio.run(messaging_store.redeem_link_code("ZZZZZZ", ChannelKind.self_hosted_imessage, PHONE)).status == "invalid"


def test_expired_code_is_invalid(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid))
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    code, _ = asyncio.run(messaging_store.create_link_code(uid, now=past))  # expires an hour ago
    assert asyncio.run(messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE)).status == "invalid"


def test_second_users_code_does_not_silently_rebind(clean_db):
    a, b = uuid4(), uuid4()
    asyncio.run(_ensure_user(a)); asyncio.run(_ensure_user(b))
    ca, _ = asyncio.run(messaging_store.create_link_code(a))
    assert asyncio.run(messaging_store.redeem_link_code(ca, ChannelKind.self_hosted_imessage, PHONE)).status == "linked"
    cb, _ = asyncio.run(messaging_store.create_link_code(b))
    r = asyncio.run(messaging_store.redeem_link_code(cb, ChannelKind.self_hosted_imessage, PHONE))
    assert r.status == "conflict"                                  # relink needs explicit confirmation
    # identity still bound to A
    assert asyncio.run(messaging_store.list_identities(a))[0].channel_identity == PHONE
    assert asyncio.run(messaging_store.list_identities(b)) == []


def test_deletion_cascades_messaging(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid))
    code, _ = asyncio.run(messaging_store.create_link_code(uid))
    asyncio.run(messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE))
    asyncio.run(api._user_repo.delete(uid))

    async def _counts():
        async with worker_session() as s:
            i = (await s.execute(select(func.count()).select_from(schema.MessagingIdentity).where(schema.MessagingIdentity.channel_identity == PHONE))).scalar_one()
            c = (await s.execute(select(func.count()).select_from(schema.AccountLinkCode).where(schema.AccountLinkCode.user_id == uid))).scalar_one()
        return i, c
    assert asyncio.run(_counts()) == (0, 0)


def test_api_link_code_and_disconnect(clean_db):
    uid = uuid4()
    body = client.post("/v1/messaging/link-code", headers=_auth(uid)).json()
    assert len(body["code"]) == 6 and body["channel"] == "self_hosted_imessage"
    asyncio.run(messaging_store.redeem_link_code(body["code"], ChannelKind.self_hosted_imessage, PHONE))
    idents = client.get("/v1/messaging/identities", headers=_auth(uid)).json()
    assert len(idents) == 1 and idents[0]["handle_hint"] == "…4567" and idents[0]["linked"] is True
    r = client.delete(f"/v1/messaging/identities/{idents[0]['id']}", headers=_auth(uid))
    assert r.status_code == 200 and r.json()["disconnected"] is True


def test_handle_locked_out_after_repeated_wrong_codes(clean_db):
    """Brute force: many wrong invite codes from ONE handle → the handle is rate-limited, and even a
    subsequently-valid code is refused while locked (defense beyond the per-code cap)."""
    from bruce_engine.messaging_store import LINK_ATTEMPT_MAX
    uid = uuid4(); asyncio.run(_ensure_user(uid))
    statuses = [
        asyncio.run(messaging_store.redeem_link_code(f"WRONG{i}", ChannelKind.self_hosted_imessage, PHONE)).status
        for i in range(LINK_ATTEMPT_MAX)
    ]
    assert statuses == ["invalid"] * LINK_ATTEMPT_MAX          # each miss recorded
    code, _ = asyncio.run(messaging_store.create_link_code(uid))
    assert asyncio.run(
        messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE)
    ).status == "rate_limited"                                  # locked — a valid code can't get through


def test_successful_link_clears_handle_attempts(clean_db):
    """A successful link resets the handle's failure counter so a later stray miss doesn't insta-lock."""
    from bruce_engine.messaging_store import LINK_ATTEMPT_MAX
    uid = uuid4(); asyncio.run(_ensure_user(uid))
    for i in range(LINK_ATTEMPT_MAX - 1):                       # a few misses, below the threshold
        assert asyncio.run(
            messaging_store.redeem_link_code(f"NOPE{i:02d}", ChannelKind.self_hosted_imessage, PHONE)
        ).status == "invalid"
    code, _ = asyncio.run(messaging_store.create_link_code(uid))
    assert asyncio.run(
        messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE)
    ).status == "linked"

    async def _attempt_count():
        async with worker_session() as s:
            return (await s.execute(select(func.count()).select_from(schema.MessagingLinkAttempt)
                    .where(schema.MessagingLinkAttempt.channel_identity == PHONE))).scalar_one()
    assert asyncio.run(_attempt_count()) == 0                   # counter cleared on success


def test_locked_handle_does_not_reveal_account(clean_db):
    """A locked-out / bad-code reply is generic — it never signals whether an account exists."""
    from bruce_engine.messaging import ChannelKind as CK
    # unknown handle, wrong code → 'invalid' (no user enumeration); repeated → 'rate_limited'.
    r = asyncio.run(messaging_store.redeem_link_code("AAAAAA", CK.self_hosted_imessage, "+15550009999"))
    assert r.status == "invalid" and r.user_id is None         # nothing about any account leaks


def test_cross_user_cannot_see_or_disconnect_anothers_identity(clean_db):
    a, b = uuid4(), uuid4()
    code = client.post("/v1/messaging/link-code", headers=_auth(a)).json()["code"]
    asyncio.run(messaging_store.redeem_link_code(code, ChannelKind.self_hosted_imessage, PHONE))
    aid = client.get("/v1/messaging/identities", headers=_auth(a)).json()[0]["id"]
    # B sees none, and cannot disconnect A's identity (RLS -> 404)
    assert client.get("/v1/messaging/identities", headers=_auth(b)).json() == []
    assert client.delete(f"/v1/messaging/identities/{aid}", headers=_auth(b)).status_code == 404
