"""Phase 2 — relay device credential boundary against REAL Postgres.

The server stores only a hash of the device secret; a stale timestamp is a replay; a revoked or
rotated credential stops working. Skips when Postgres isn't configured (pg_test_db).
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import relay_auth, schema
from bruce_engine.db import worker_session
from bruce_engine.relay_auth import RelayAuthError


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


def test_secret_is_stored_only_as_a_hash(clean_db):
    dev_id, secret = asyncio.run(relay_auth.register_device("mac-alpha"))

    async def _row():
        async with worker_session() as s:
            return (await s.execute(select(schema.RelayDevice).where(schema.RelayDevice.id == dev_id))).scalar_one()
    dev = asyncio.run(_row())
    assert dev.credential_hash != secret
    assert dev.credential_hash == hashlib.sha256(secret.encode()).hexdigest()


def test_valid_credential_authenticates(clean_db):
    _, secret = asyncio.run(relay_auth.register_device("mac-alpha"))
    dev = asyncio.run(relay_auth.authenticate(secret, timestamp=_now().isoformat()))
    assert dev.name == "mac-alpha" and dev.last_seen_at is not None


def test_wrong_credential_is_rejected(clean_db):
    asyncio.run(relay_auth.register_device("mac-alpha"))
    with pytest.raises(RelayAuthError, match="invalid"):
        asyncio.run(relay_auth.authenticate("not-the-secret", timestamp=_now().isoformat()))


def test_stale_timestamp_is_rejected_as_replay(clean_db):
    _, secret = asyncio.run(relay_auth.register_device("mac-alpha"))
    old = (_now() - datetime.timedelta(minutes=30)).isoformat()
    with pytest.raises(RelayAuthError, match="replay"):
        asyncio.run(relay_auth.authenticate(secret, timestamp=old))


def test_revoked_credential_stops_working(clean_db):
    dev_id, secret = asyncio.run(relay_auth.register_device("mac-alpha"))
    assert asyncio.run(relay_auth.revoke_device(dev_id)) is True
    with pytest.raises(RelayAuthError, match="revoked"):
        asyncio.run(relay_auth.authenticate(secret, timestamp=_now().isoformat()))


def test_rotation_invalidates_the_old_secret(clean_db):
    dev_id, old_secret = asyncio.run(relay_auth.register_device("mac-alpha"))
    new_secret = asyncio.run(relay_auth.rotate_device(dev_id))
    assert new_secret and new_secret != old_secret
    with pytest.raises(RelayAuthError):
        asyncio.run(relay_auth.authenticate(old_secret, timestamp=_now().isoformat()))
    assert asyncio.run(relay_auth.authenticate(new_secret, timestamp=_now().isoformat())).id == dev_id
