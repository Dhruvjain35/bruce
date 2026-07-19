"""Account linking + messaging identity persistence (Phase 5).

A phone number proves nothing on its own — anyone can text a number. A channel identity binds to a
Bruce user ONLY via a short-lived, single-use code the AUTHENTICATED app user generated and texted
in. The code is hashed at rest (never the plaintext); redemption is cross-user (the webhook doesn't
know who is texting), so it runs in a worker session, then binds the identity to the code's owner.

Rules encoded here:
  * one channel identity binds to at most one user; a code from a DIFFERENT user does not silently
    rebind it — that returns ``conflict`` and needs an explicit relink flow.
  * codes are single-use and expire; a bounded per-code attempt cap guards against reuse fuzzing
    (broad inbound rate-limiting is Phase 9).
  * account deletion cascades (FK ON DELETE CASCADE) — verified by test.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import os
from uuid import UUID

from sqlalchemy import select

from . import schema
from .db import user_session, worker_session
from .messaging import LINK_CODE_TTL, ChannelKind, generate_link_code

MAX_REDEEM_ATTEMPTS = 5                                    # per-CODE reuse cap
LINK_ATTEMPT_MAX = 5                                       # failed attempts per HANDLE before lockout
LINK_ATTEMPT_WINDOW = datetime.timedelta(minutes=15)      # rolling window for the handle counter
LINK_ATTEMPT_LOCKOUT = datetime.timedelta(minutes=15)     # how long a handle stays locked out


def _pepper() -> bytes:
    """The server-side link-code pepper — held ONLY in Secret Manager / env, NEVER in the database.
    Required: we fail closed rather than fall back to an unpeppered digest.

    .strip() is essential: the same secret is delivered with DIFFERENT trailing bytes depending on the
    path — Cloud Run injects the secret payload verbatim (a trailing newline survives), while a local
    `export X="$(gcloud secrets access …)"` drops it. Without normalization, a CLI-minted code hashes
    under a different key than the API redeems with, and every such code reads as invalid."""
    p = os.environ.get("BRUCE_LINK_CODE_PEPPER")
    if not p or not p.strip():
        raise RuntimeError("BRUCE_LINK_CODE_PEPPER is not set — required to hash link codes")
    return p.strip().encode()


def _hash(code: str) -> str:
    # HMAC-SHA256 with a server-side pepper. A 6-char code has too little entropy for plain SHA-256 to
    # resist offline brute-force if the DB leaks; the pepper (not stored in the DB) makes the digest
    # uninvertible unless the attacker ALSO steals the secret from Secret Manager. Normalize first so a
    # texted code matches regardless of casing/whitespace.
    return hmac.new(_pepper(), code.strip().upper().encode(), hashlib.sha256).hexdigest()


@dataclasses.dataclass
class RedeemResult:
    status: str  # "linked" | "invalid" | "locked" | "conflict" | "rate_limited"
    user_id: UUID | None = None
    identity_id: UUID | None = None


async def _attempt_row(s, channel: ChannelKind, identity: str):
    return (await s.execute(
        select(schema.MessagingLinkAttempt).where(
            schema.MessagingLinkAttempt.channel == channel.value,
            schema.MessagingLinkAttempt.channel_identity == identity)
    )).scalar_one_or_none()


async def _record_failure(s, att, channel: ChannelKind, identity: str, now: datetime.datetime) -> None:
    """Increment the per-handle failure counter; lock the handle out once it crosses the threshold
    inside the rolling window. A brute-forcer texting many wrong codes is stopped regardless of the
    per-code cap."""
    if att is None:
        s.add(schema.MessagingLinkAttempt(
            channel=channel.value, channel_identity=identity, failed_count=1, window_start=now))
        return
    if now - att.window_start > LINK_ATTEMPT_WINDOW:
        att.failed_count, att.window_start, att.locked_until = 1, now, None   # window rolled over
    else:
        att.failed_count += 1
        if att.failed_count >= LINK_ATTEMPT_MAX:
            att.locked_until = now + LINK_ATTEMPT_LOCKOUT


async def create_link_code(user_id: UUID, channel: ChannelKind = ChannelKind.self_hosted_imessage,
                           now: datetime.datetime | None = None) -> tuple[str, datetime.datetime]:
    """Generate a one-time code for an authenticated user. Returns the PLAINTEXT once (never stored)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    code = generate_link_code()
    expires_at = now + LINK_CODE_TTL
    async with user_session(user_id) as s:
        s.add(schema.AccountLinkCode(
            user_id=user_id, channel=channel.value, code_hash=_hash(code), expires_at=expires_at,
        ))
        await s.flush()
    return code, expires_at


async def redeem_link_code(code: str, channel: ChannelKind, channel_identity: str,
                           now: datetime.datetime | None = None) -> RedeemResult:
    """Bind a channel identity to the code's owner. Cross-user (worker session). Idempotent-safe:
    a re-redeem of an already-consumed code returns ``invalid`` (single-use)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    h = _hash(code)
    async with worker_session() as s:
        att = await _attempt_row(s, channel, channel_identity)
        if att is not None and att.locked_until is not None and att.locked_until > now:
            return RedeemResult(status="rate_limited")   # handle is locked out — don't even look it up

        row = (await s.execute(
            select(schema.AccountLinkCode)
            .where(schema.AccountLinkCode.code_hash == h, schema.AccountLinkCode.channel == channel.value,
                   schema.AccountLinkCode.consumed_at.is_(None))
            .order_by(schema.AccountLinkCode.created_at.desc())
        )).scalars().first()
        # Constant-time re-verification of the digest (defense in depth; matches relay_auth).
        if row is None or not hmac.compare_digest(row.code_hash, h) or row.expires_at <= now:
            await _record_failure(s, att, channel, channel_identity, now)
            return RedeemResult(status="invalid")
        row.attempts = (row.attempts or 0) + 1
        if row.attempts > MAX_REDEEM_ATTEMPTS:
            await _record_failure(s, att, channel, channel_identity, now)
            return RedeemResult(status="locked")

        ident = (await s.execute(
            select(schema.MessagingIdentity).where(
                schema.MessagingIdentity.channel == channel.value,
                schema.MessagingIdentity.channel_identity == channel_identity)
        )).scalar_one_or_none()
        if ident is None:
            ident = schema.MessagingIdentity(
                user_id=row.user_id, channel=channel.value, channel_identity=channel_identity)
            s.add(ident)
            await s.flush()
        elif ident.user_id is not None and ident.user_id != row.user_id:
            # Already bound to a DIFFERENT user — never silently rebind (prevents number hijacking).
            await _record_failure(s, att, channel, channel_identity, now)
            return RedeemResult(status="conflict")
        else:
            ident.user_id = row.user_id
            ident.disconnected_at = None  # re-link reactivates
        row.consumed_at = now
        row.bound_identity_id = ident.id
        if att is not None:
            await s.delete(att)          # success clears the handle's failure counter
        return RedeemResult(status="linked", user_id=row.user_id, identity_id=ident.id)


async def list_identities(user_id: UUID) -> list[schema.MessagingIdentity]:
    async with user_session(user_id) as s:
        return list((await s.execute(
            select(schema.MessagingIdentity).where(schema.MessagingIdentity.user_id == user_id)
        )).scalars().all())


async def disconnect_identity(user_id: UUID, identity_id: UUID) -> bool:
    """User disconnects messaging from the app. RLS ensures they can only touch their own identity."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with user_session(user_id) as s:
        ident = (await s.execute(
            select(schema.MessagingIdentity).where(
                schema.MessagingIdentity.id == identity_id, schema.MessagingIdentity.user_id == user_id)
        )).scalar_one_or_none()
        if ident is None:
            return False
        ident.disconnected_at = now
        return True
