"""Phase 7 (server) — attachment upload validation + consumption, against REAL Postgres.

Allowlist + size cap + executable reject at the boundary; an uploaded image, referenced from an
inbound event, becomes the durable intake source and the staged bytes are cleared. Skips without PG.
"""

from __future__ import annotations

import asyncio
import base64
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import relay_auth, relay_uploads, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.messaging import ChannelKind
from bruce_engine.relay_uploads import UploadRejected

client = TestClient(api.app)
PHONE = "+15550002222"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # not a real PNG, but a non-executable image blob


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _hdrs(secret):
    import datetime
    return {"Authorization": f"Bearer {secret}", "X-Bruce-Timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def _device():
    return asyncio.run(relay_auth.register_device("mac-test"))


async def _ensure_user(uid):
    async with user_session(uid) as s:
        if (await s.execute(select(schema.User).where(schema.User.id == uid))).scalar_one_or_none() is None:
            s.add(schema.User(id=uid, auth_provider="apple"))


async def _link(uid):
    async with worker_session() as s:
        s.add(schema.MessagingIdentity(user_id=uid, channel=ChannelKind.self_hosted_imessage.value, channel_identity=PHONE))


def test_valid_image_upload_returns_ref_and_hash(clean_db):
    _, secret = _device()
    r = client.post("/v1/relay/upload", headers=_hdrs(secret),
                    json={"content_base64": base64.b64encode(PNG).decode(), "media_type": "image/png", "filename": "f.png"})
    assert r.status_code == 200 and r.json()["upload_ref"] and len(r.json()["content_hash"]) == 64


def test_executable_upload_is_rejected(clean_db):
    _, secret = _device()
    macho = b"\xca\xfe\xba\xbe" + b"\x00" * 32
    r = client.post("/v1/relay/upload", headers=_hdrs(secret),
                    json={"content_base64": base64.b64encode(macho).decode(), "media_type": "application/pdf"})
    assert r.status_code == 415 and r.json()["detail"]["error"] == "upload_rejected"


def test_unsupported_type_is_rejected(clean_db):
    _, secret = _device()
    r = client.post("/v1/relay/upload", headers=_hdrs(secret),
                    json={"content_base64": base64.b64encode(b"hello").decode(), "media_type": "application/x-sh"})
    assert r.status_code == 415


def test_oversize_is_rejected():
    with pytest.raises(UploadRejected, match="too large"):
        asyncio.run(relay_uploads.store_upload(relay_device_id=None, data=b"x" * (relay_uploads.MAX_UPLOAD_BYTES + 1), media_type="image/png"))


def test_duplicate_upload_dedups_by_hash(clean_db):
    _, secret = _device()
    body = {"content_base64": base64.b64encode(PNG).decode(), "media_type": "image/png"}
    a = client.post("/v1/relay/upload", headers=_hdrs(secret), json=body).json()
    b = client.post("/v1/relay/upload", headers=_hdrs(secret), json=body).json()
    assert a["upload_ref"] == b["upload_ref"]


def test_uploaded_image_becomes_intake_and_is_consumed(clean_db):
    uid = uuid4(); asyncio.run(_ensure_user(uid)); asyncio.run(_link(uid))
    _, secret = _device()
    ref = client.post("/v1/relay/upload", headers=_hdrs(secret),
                      json={"content_base64": base64.b64encode(PNG).decode(), "media_type": "image/png"}).json()["upload_ref"]
    r = client.post("/v1/relay/inbound", headers=_hdrs(secret), json={
        "provider_message_id": "img1", "channel_identity": PHONE,
        "attachments": [{"kind": "image", "upload_ref": ref, "media_type": "image/png"}]}).json()
    assert r["status"] == "processed" and r["mission_id"]

    async def _check():
        async with worker_session() as s:
            up = (await s.execute(select(schema.RelayUpload).where(schema.RelayUpload.id == UUID(ref)))).scalar_one()
            job = (await s.execute(select(schema.IntakeJob).where(schema.IntakeJob.user_id == uid))).scalar_one()
        return up, job
    up, job = asyncio.run(_check())
    assert up.consumed_at is not None and up.data is None          # staged bytes cleared
    assert job.source_kind == "image" and job.input_bytes == PNG   # bytes landed in the durable intake
