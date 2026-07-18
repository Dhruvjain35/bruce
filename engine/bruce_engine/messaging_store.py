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
from uuid import UUID

from sqlalchemy import select

from . import schema
from .db import user_session, worker_session
from .messaging import LINK_CODE_TTL, ChannelKind, generate_link_code

MAX_REDEEM_ATTEMPTS = 5


def _hash(code: str) -> str:
    # Normalize (upper, strip) so a texted code matches regardless of casing/whitespace, then hash.
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


@dataclasses.dataclass
class RedeemResult:
    status: str  # "linked" | "invalid" | "locked" | "conflict"
    user_id: UUID | None = None
    identity_id: UUID | None = None


async def create_link_code(user_id: UUID, channel: ChannelKind = ChannelKind.linq,
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
        row = (await s.execute(
            select(schema.AccountLinkCode)
            .where(schema.AccountLinkCode.code_hash == h, schema.AccountLinkCode.channel == channel.value,
                   schema.AccountLinkCode.consumed_at.is_(None))
            .order_by(schema.AccountLinkCode.created_at.desc())
        )).scalars().first()
        if row is None or row.expires_at <= now:
            return RedeemResult(status="invalid")
        row.attempts = (row.attempts or 0) + 1
        if row.attempts > MAX_REDEEM_ATTEMPTS:
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
            # Already bound to a DIFFERENT user — never silently rebind.
            return RedeemResult(status="conflict")
        else:
            ident.user_id = row.user_id
            ident.disconnected_at = None  # re-link reactivates
        row.consumed_at = now
        row.bound_identity_id = ident.id
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
