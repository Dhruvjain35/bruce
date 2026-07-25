"""C2 — the production notifier: how a finished background mission reaches the student.

A mission that has done its work still has to TELL the student, and the only channel that counts is the
one they already use. This module is the engine half of that: it hands the notification to the DURABLE
outbound queue, and the relay drains it to iMessage.

WHY THE ENGINE DOES NOT SEND. The engine runs on Cloud Run and cannot talk to iMessage; only the relay
Mac can. So "notify" here means "durably queue, exactly once" — the relay claims the row under a lease,
sends via imsg, and reports back. That split is what makes the guarantee possible at all: the engine
owns exactly-once ENQUEUE, the relay owns exactly-once HANDOFF.

THE EXACTLY-ONCE CHAIN, end to end:

  1. runner  -> `idempotency_key = f"{run_id}:notify"` (run id + purpose). Stable across lease reclaims
     and worker restarts, because it is derived from the run, never from wall-clock or a retry counter.
  2. engine  -> `messaging_outbound.enqueue` is idempotent on that key: a second call with the same key
     writes NOTHING. A retried advance therefore cannot queue a second text.
  3. relay   -> claims under a lease, and `relay/imsg.py::_send` REFUSES to report success without a
     confirmation guid: an explicit decline raises ImsgSendRejected (retryable, no bytes handed over),
     and a missing guid raises rather than returning None, because a missing guid is AMBIGUOUS — the
     message may already be on its way. The relay's outbound ledger records HANDED_TO_IMSG with the guid
     BEFORE the server is told, so a crash between sending and reporting is recoverable without a
     duplicate: on restart the ledger already knows this oid reached imsg.

FAILURE IS LOUD AND RECOVERABLE. If the handle cannot be resolved or the queue write fails, this raises.
`PlanMissionAdvancer` only stamps `recovery_state["notified"] = True` AFTER the notifier returns, so a
failed notify leaves the mission un-notified and un-completed; the lease expires, the run is reclaimed,
and the attempt repeats against the same idempotency key. Nothing is lost and nothing is duplicated.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol
from uuid import UUID

from sqlalchemy import select

from . import messaging_outbound, schema
from .db import user_session
from .messaging import ChannelKind

log = logging.getLogger(__name__)

NOTIFY_PURPOSE = "notify"
NOTIFY_KIND = "mission_notify"
_CHANNEL = ChannelKind.self_hosted_imessage


class NotifierUnavailable(RuntimeError):
    """The notification could not be handed off. Raised, never swallowed: the mission must stay
    un-notified so the runner retries rather than completing on a delivery that never happened."""


class Notifier(Protocol):
    """Delivers exactly one message about a finished mission.

    `idempotency_key` is stable across retries and lease reclaims — a transport MUST dedupe on it rather
    than assuming it is called once, because a worker can crash between sending and checkpointing.
    """

    async def __call__(self, user_id: UUID, run: dict, *, idempotency_key: str) -> None: ...


class RecordingNotifier:
    """Test double. Captures deliveries in memory so a test can assert EXACTLY one, keyed by idempotency
    key. Not for production use: recording a delivery is not making one."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def __call__(self, user_id: UUID, run: dict, *, idempotency_key: str) -> None:
        self.keys.append(idempotency_key)

    @property
    def unique_keys(self) -> set[str]:
        return set(self.keys)


async def resolve_handle(user_id: UUID) -> str | None:
    """The student's iMessage handle for this channel, or None. Owner-scoped; the newest bound identity
    wins if a student ever linked more than one handle.

    NOTE ON HANDLE FORMAT — do not "fix" this to the `any;-;+1...` form. Conversation replies use
    `msg.thread_id or msg.channel_identity` (messaging_inbound.py:102), so they carry imsg's CHAT
    identifier, which is why sent rows show `any;-;+1...`. A mission notification has no inbound message
    and therefore no thread, so it addresses the student directly by number. That is a supported imsg
    target: the live relay check sent to a bare `+1...` and it arrived with a confirmation guid.
    """
    async with user_session(user_id) as s:
        return (await s.execute(
            select(schema.MessagingIdentity.channel_identity)
            .where(schema.MessagingIdentity.user_id == user_id,
                   schema.MessagingIdentity.channel == _CHANNEL.value,
                   schema.MessagingIdentity.blocked_at.is_(None))
            .order_by(schema.MessagingIdentity.id.desc()).limit(1))).scalar_one_or_none()


def compose_notification(run: dict) -> str:
    """What the student actually reads. Grounded in the run: it says a reply landed, and nothing it
    cannot support. No em dash (the outbound gate asserts on one anyway)."""
    goal = run.get("goal") or {}
    to = goal.get("to")
    who = f"{to} replied" if to else "you got a reply"
    return f"hey, {who} to the email i sent for you. want me to pull it up?"


class RelayNotifier:
    """Production Notifier. Hands the notification to the durable outbound queue the relay drains.

    Returns only on a durable, deduplicated enqueue. Everything else raises, so the runner retries.
    """

    async def __call__(self, user_id: UUID, run: dict, *, idempotency_key: str) -> None:
        handle = await resolve_handle(user_id)
        if not handle:
            # Honest and recoverable: the student has no linked handle, so there is nowhere to send.
            # Raising keeps the mission un-notified instead of completing on a phantom delivery.
            log.error("notify_no_handle user=%s run=%s key=%s", user_id, run.get("id"), idempotency_key)
            raise NotifierUnavailable(f"no {_CHANNEL.value} handle linked for user {user_id}")

        mission_id = run.get("mission_id")
        try:
            await messaging_outbound.enqueue(
                user_id=user_id, to_handle=handle, channel=_CHANNEL, kind=NOTIFY_KIND,
                text=compose_notification(run), idempotency_key=idempotency_key,
                mission_id=UUID(str(mission_id)) if mission_id else None)
        except Exception as exc:
            log.exception("notify_enqueue_failed user=%s run=%s key=%s", user_id, run.get("id"),
                          idempotency_key)
            raise NotifierUnavailable("outbound enqueue failed") from exc
        log.info("notify_queued user=%s run=%s key=%s handle_known=%s", user_id, run.get("id"),
                 idempotency_key, bool(handle))


def transport_configured() -> bool:
    """Whether the production notifier is switched on. Default ON: the transport is built and verified,
    so the flag exists as a kill switch, not as a feature gate."""
    return os.environ.get("BRUCE_MISSION_NOTIFIER_OFF", "").strip().lower() not in {"1", "true", "yes", "on"}


def build_notifier() -> Notifier | None:
    """The production notifier, or None when the kill switch is thrown.

    None is load-bearing: `PlanMissionAdvancer` only records a notification when a notifier is actually
    invoked, so with None a mission completes WITHOUT claiming a delivery it never made.
    """
    if not transport_configured():
        log.warning("mission_notifier_disabled — missions will complete without notifying")
        return None
    return RelayNotifier()
