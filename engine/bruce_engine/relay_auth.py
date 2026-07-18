"""Phase 2 — relay device credential boundary (self-hosted iMessage alpha).

A dedicated Mac relay authenticates to Bruce with a rotating device secret held in the macOS
Keychain. The SERVER stores only the sha256 HASH of that secret — never the secret. Every request
carries a timestamp (+ nonce + request id at the HTTP layer); a stale timestamp is rejected as a
replay. A device is remotely revocable (revoked_at). The relay holds NO cloud/OpenAI/DB keys — only
this one narrow credential.

relay_devices is worker-only (infrastructure, not user-owned), so all access is in a worker session.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import secrets
from uuid import UUID

from sqlalchemy import select

from . import schema
from .db import worker_session
from .messaging import REPLAY_WINDOW


class RelayAuthError(Exception):
    """Authentication failed — bad/revoked credential, or a replayed (stale-timestamp) request."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def register_device(name: str, *, bruce_handle: str | None = None) -> tuple[UUID, str]:
    """Register a relay device. Returns (device_id, secret). The secret is shown ONCE — it is stored
    only as a hash; put it in the Mac Keychain and never again."""
    secret = secrets.token_urlsafe(32)
    async with worker_session() as s:
        dev = schema.RelayDevice(name=name, credential_hash=_hash(secret), bruce_handle=bruce_handle)
        s.add(dev)
        await s.flush()
        return dev.id, secret


async def authenticate(secret: str, *, timestamp: str | None = None,
                       now: datetime.datetime | None = None) -> schema.RelayDevice:
    """Verify a relay's bearer secret (hash match, not revoked) and reject a stale timestamp. Returns
    the device (and stamps last_seen_at). Constant-time-safe: the hash is looked up, then confirmed
    with compare_digest so a partial match can't be probed by timing."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not secret:
        raise RelayAuthError("missing credential")

    # Replay window on the caller-supplied timestamp (defense in depth over TLS).
    if timestamp is not None:
        try:
            sent_at = datetime.datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise RelayAuthError("bad timestamp") from exc
        if abs((now - sent_at).total_seconds()) > REPLAY_WINDOW.total_seconds():
            raise RelayAuthError("stale request (replay)")

    h = _hash(secret)
    async with worker_session() as s:
        dev = (await s.execute(
            select(schema.RelayDevice).where(schema.RelayDevice.credential_hash == h)
        )).scalar_one_or_none()
        if dev is None or not hmac.compare_digest(dev.credential_hash, h):
            raise RelayAuthError("invalid credential")
        if dev.revoked_at is not None:
            raise RelayAuthError("credential revoked")
        dev.last_seen_at = now
        return dev


async def revoke_device(device_id: UUID, now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    async with worker_session() as s:
        dev = (await s.execute(select(schema.RelayDevice).where(schema.RelayDevice.id == device_id))).scalar_one_or_none()
        if dev is None:
            return False
        dev.revoked_at = now
        return True


async def rotate_device(device_id: UUID, now: datetime.datetime | None = None) -> str | None:
    """Issue a new secret for a device (old one stops working immediately). Returns the new secret."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    secret = secrets.token_urlsafe(32)
    async with worker_session() as s:
        dev = (await s.execute(select(schema.RelayDevice).where(schema.RelayDevice.id == device_id))).scalar_one_or_none()
        if dev is None or dev.revoked_at is not None:
            return None
        dev.credential_hash = _hash(secret)
        dev.rotated_at = now
        return secret
