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

from sqlalchemy import func, select

from . import schema
from .db import worker_session
from .messaging import REPLAY_WINDOW

# Registration rate limit: at most this many register attempts per environment per window.
_REGISTER_MAX_PER_WINDOW = 10
_REGISTER_WINDOW = datetime.timedelta(minutes=10)


class RelayAuthError(Exception):
    """Authentication failed — bad/revoked credential, or a replayed (stale-timestamp) request."""


class BootstrapError(RelayAuthError):
    """Bootstrap device registration was refused (bad/expired/used token, env/device mismatch, rate limit)."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def _audit_registration(s, *, action: str, environment: str, result: str, actor: str | None,
                              device_name: str | None = None, device_id=None, reason: str | None = None) -> None:
    s.add(schema.RelayRegistrationAudit(actor=actor, action=action, environment=environment, result=result,
                                        device_name=device_name, device_id=device_id, reason=reason))


async def mint_bootstrap_token(device_name: str, *, environment: str, ttl_seconds: int = 600,
                               actor: str | None = None, max_uses: int = 1,
                               now: datetime.datetime | None = None) -> str:
    """Mint a SHORT-LIVED, SINGLE-USE bootstrap token bound to (environment, device_name). Only its hash
    is stored. Returns the raw token ONCE (the installer uses it to register, then it is consumed). This
    is the temporary bootstrap material — NOT the permanent device credential."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    token = secrets.token_urlsafe(32)
    async with worker_session() as s:
        s.add(schema.RelayBootstrapToken(
            token_hash=_hash(token), environment=environment, device_name=device_name,
            max_uses=max_uses, expires_at=now + datetime.timedelta(seconds=ttl_seconds), created_by=actor))
        await _audit_registration(s, action="mint", environment=environment, result="ok", actor=actor,
                                  device_name=device_name)
    return token


async def register_with_bootstrap(token: str, *, device_name: str, environment: str,
                                  actor: str = "installer", bruce_handle: str | None = None,
                                  now: datetime.datetime | None = None) -> tuple[UUID, str]:
    """Register (or idempotently ROTATE) the relay device named ``device_name`` using a valid bootstrap
    token. FAIL-CLOSED + audited: the token must exist, be unexpired, unconsumed, and bound to the SAME
    environment and device_name; the env must not be over its rate limit. On success the token is
    consumed (single-use → a replay fails), an existing same-named device is ROTATED (new secret, old
    credential invalidated — never a silent rebind), and (device_id, permanent_secret) is returned. The
    secret is returned in-memory to the caller (the installer) and never logged."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if not token:
        raise BootstrapError("missing bootstrap token")
    h = _hash(token)
    deny: tuple[str, str, str] | None = None    # (action, reason, message) if denied — audited then raised
    dev_id = None
    secret = None
    # Single session that COMMITS whether we allow or deny, so the audit (and single-use consumption)
    # always persist — the deny is raised AFTER the commit, never inside a rolled-back transaction.
    async with worker_session() as s:
        recent = (await s.execute(select(func.count()).select_from(schema.RelayRegistrationAudit).where(
            schema.RelayRegistrationAudit.environment == environment,
            schema.RelayRegistrationAudit.action.in_(("register", "rotate", "deny", "replay")),
            schema.RelayRegistrationAudit.created_at >= now - _REGISTER_WINDOW))).scalar_one()
        tok = (await s.execute(select(schema.RelayBootstrapToken).where(
            schema.RelayBootstrapToken.token_hash == h))).scalar_one_or_none()

        if recent >= _REGISTER_MAX_PER_WINDOW:
            deny = ("rate_limited", "rate_limited", "registration rate limit exceeded")
        elif tok is None or not hmac.compare_digest(tok.token_hash, h):
            deny = ("deny", "unknown_token", "invalid bootstrap token")
        elif tok.consumed or tok.used_count >= tok.max_uses:
            deny = ("replay", "token_used", "bootstrap token already used")
        elif tok.expires_at <= now:
            deny = ("deny", "token_expired", "bootstrap token expired")
        elif tok.environment != environment:
            deny = ("deny", "env_mismatch", "bootstrap token environment mismatch")
        elif tok.device_name != device_name:
            deny = ("deny", "device_mismatch", "bootstrap token device mismatch")

        if deny is not None:
            await _audit_registration(s, action=deny[0], environment=environment, result="denied",
                                      actor=actor, device_name=device_name, reason=deny[1])
        else:
            tok.used_count += 1              # consume FIRST (single-use) so a concurrent replay can't reuse it
            tok.used_at = now
            tok.consumed = tok.used_count >= tok.max_uses
            secret = secrets.token_urlsafe(32)
            existing = (await s.execute(select(schema.RelayDevice).where(
                schema.RelayDevice.name == device_name, schema.RelayDevice.revoked_at.is_(None)
            ).order_by(schema.RelayDevice.created_at))).scalars().first()
            if existing is not None:
                existing.credential_hash = _hash(secret)   # ROTATE: old credential invalidated immediately
                existing.rotated_at = now
                dev_id = existing.id
                action = "rotate"
            else:
                dev = schema.RelayDevice(name=device_name, credential_hash=_hash(secret), bruce_handle=bruce_handle)
                s.add(dev)
                await s.flush()
                dev_id = dev.id
                action = "register"
            await _audit_registration(s, action=action, environment=environment, result="ok", actor=actor,
                                      device_name=device_name, device_id=dev_id)
    if deny is not None:
        raise BootstrapError(deny[2])
    return dev_id, secret


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
