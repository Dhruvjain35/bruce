"""Phase 8 — durable outbound queue. Cloud Run NEVER calls the Mac; it writes rows here and the
relay claims them.

State machine (attempts is incremented on each claim, so it bounds retries):

    pending ─claim─▶ sending ─sent─▶ sent
                        │
                        ├─ retryable (attempts < max) ─▶ retryable_failed ─(lease expires)─▶ (reclaim)
                        └─ terminal  (attempts ≥ max) ─▶ terminal_failed
    (a crashed relay leaves status=sending with an expired lease → reclaimable)

Idempotent enqueue on idempotency_key: a redelivered inbound never queues the same reply twice.
Claiming uses FOR UPDATE SKIP LOCKED so multiple relay pollers never grab the same message.
"""

from __future__ import annotations

import dataclasses
import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import text as sa_text

from . import schema
from .db import user_session, worker_session
from .messaging import ChannelKind, MessagingChannel, OutboundMessage

DEFAULT_LEASE_SECONDS = 120
DEFAULT_RETRY_BACKOFF_SECONDS = 30


@dataclasses.dataclass
class ClaimedOutbound:
    id: UUID
    to_handle: str | None
    channel: str
    kind: str
    text: str
    deep_link: str | None
    attempts: int


_PLAIN_TEXT_CHANNELS = frozenset({"self_hosted_imessage", "imessage", "sms"})


def gate_outbound_text(text: str, channel_value: str) -> str:
    """FINAL channel-safety floor for EVERY outbound (integration invariant 7 — no bypasses). Applied
    inside enqueue(), so no caller — the conversation runtime, a legacy ACK, an error, a status update —
    can ship an em dash or a corporate filler phrase to a plain-text channel. Rich voice styling happens
    upstream; this is the last-line HARD guarantee, not a substitute for it. Idempotent."""
    from .conversation_style import PROHIBITED_PHRASES, enforce_no_dashes
    if not text or channel_value not in _PLAIN_TEXT_CHANNELS:
        return text
    import re
    out = text
    for p in PROHIBITED_PHRASES:
        out = re.sub(re.escape(p), "", out, flags=re.IGNORECASE)
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    out = enforce_no_dashes(out)
    assert "—" not in out, "em dash must never ship to a plain-text channel (outbound gate)"
    return out


async def enqueue(*, user_id: UUID | None, to_handle: str, channel: ChannelKind, kind: str, text: str,
                  idempotency_key: str, mission_id: UUID | None = None, deep_link: str | None = None) -> None:
    """Durably queue an outbound reply. Idempotent on idempotency_key. Runs in the recipient's context
    when known (user_id) so RLS scopes it; a pre-link prompt (user_id None) is queued in a worker
    session. EVERY outbound passes through gate_outbound_text first — no bypasses."""
    text = gate_outbound_text(text, channel.value)      # HARD channel-safety floor, applied to all callers

    async def _write(s):
        existing = (await s.execute(
            select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.idempotency_key == idempotency_key)
        )).scalar_one_or_none()
        if existing is not None:
            return
        s.add(schema.OutboundMessageRow(
            user_id=user_id, channel=channel.value, kind=kind, text=text, to_handle=to_handle,
            deep_link=deep_link, mission_id=mission_id, idempotency_key=idempotency_key, status="pending"))

    if user_id is not None:
        async with user_session(user_id) as s:
            await _write(s)
    else:
        async with worker_session() as s:
            await _write(s)


async def claim(relay_device_id: UUID, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> ClaimedOutbound | None:
    """Claim the next sendable outbound message for a relay device (cross-user; worker session)."""
    sql = sa_text(
        """
        UPDATE outbound_messages SET
            status = 'sending', lease_owner = :owner, relay_device_id = :dev,
            lease_expires_at = now() + make_interval(secs => :lease),
            attempts = attempts + 1, version = version + 1, updated_at = now()
        WHERE id = (
            SELECT id FROM outbound_messages
            WHERE (status = 'pending')
               OR (status IN ('sending', 'retryable_failed')
                   AND lease_expires_at IS NOT NULL AND lease_expires_at < now())
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, to_handle, channel, kind, text, deep_link, attempts
        """
    )
    async with worker_session() as s:
        row = (await s.execute(sql, {"owner": str(relay_device_id)[:64], "dev": str(relay_device_id), "lease": lease_seconds})).mappings().first()
    if row is None:
        return None
    return ClaimedOutbound(id=row["id"], to_handle=row["to_handle"], channel=row["channel"],
                           kind=row["kind"], text=row["text"], deep_link=row["deep_link"], attempts=row["attempts"])


async def mark_sent(outbound_id: UUID, *, provider_message_id: str | None, relay_device_id: UUID) -> None:
    graph = None
    async with worker_session() as s:
        row = (await s.execute(select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.id == outbound_id))).scalar_one_or_none()
        if row is None:
            return
        row.status = "sent"
        row.provider_message_id = provider_message_id
        row.lease_owner = None
        row.lease_expires_at = None
        s.add(schema.DeliveryAttempt(user_id=row.user_id, outbound_message_id=outbound_id,
                                     relay_device_id=relay_device_id, attempt_no=row.attempts,
                                     status="sent", provider_message_id=provider_message_id))
        if provider_message_id and row.user_id is not None:
            graph = (row.user_id, row.channel, provider_message_id, row.to_handle)
    if graph is not None:  # Bite 2 A2: canonical outbound node (separate txn; provider guid is now known)
        from . import conversation_graph
        uid, channel, pmid, to_handle = graph
        await conversation_graph.ingest_outbound_message(
            uid, provider=channel, provider_message_id=pmid, provider_chat_id=to_handle,
            outbound_message_id=outbound_id)


async def mark_failed(outbound_id: UUID, *, reason: str, relay_device_id: UUID,
                      force_terminal: bool = False,
                      backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS) -> None:
    """Record a failed send. force_terminal=True (e.g. recipient not on iMessage) skips retries."""
    async with worker_session() as s:
        row = (await s.execute(select(schema.OutboundMessageRow).where(schema.OutboundMessageRow.id == outbound_id))).scalar_one_or_none()
        if row is None:
            return
        if force_terminal or row.attempts >= row.max_attempts:
            row.status = "terminal_failed"
            row.lease_owner = None
            row.lease_expires_at = None
        else:
            row.status = "retryable_failed"
            row.lease_expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=backoff_seconds)
        s.add(schema.DeliveryAttempt(user_id=row.user_id, outbound_message_id=outbound_id,
                                     relay_device_id=relay_device_id, attempt_no=row.attempts,
                                     status="failed", error=(reason or "")[:200]))


class QueueChannel:
    """Production MessagingChannel: 'sending' means ENQUEUING a durable row for the relay to claim —
    Cloud Run never talks to the Mac. Inbound is received via the relay endpoint, not parsed here."""

    kind = ChannelKind.self_hosted_imessage

    def verify_signature(self, *, body: bytes, headers: dict[str, str]) -> None:  # pragma: no cover
        raise NotImplementedError("inbound is authenticated at the relay endpoint, not here")

    async def parse_inbound(self, *, body: bytes, headers: dict[str, str]):  # pragma: no cover
        raise NotImplementedError

    async def send_message(self, *, to: str, message: OutboundMessage) -> str:
        # No-op: the durable outbound_messages row (written by the handoff) IS the queue entry.
        return "queued"

    async def send_decision(self, *, to: str, summary: str, deep_link: str) -> str:
        return "queued"

    async def send_receipt(self, *, to: str, summary: str, deep_link: str | None = None) -> str:
        return "queued"
