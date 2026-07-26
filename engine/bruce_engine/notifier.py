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
from .notification_brief import (NotificationBrief, Relationship,
                                 deterministic_notification, validate_notification)

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


def brief_for(run: dict, *, own_account: str | None = None) -> NotificationBrief:
    """Turn a finished run into the small set of VERIFIED facts a notification may use.

    Everything here comes from the run's own checkpoint. No provider payload, no email body, and no
    invented actor: if we do not know a name or a relationship we say "they", never an address.
    """
    cp = run.get("recovery_state") or {}
    results = cp.get("results") or {}
    replied = any(isinstance(v, dict) and v.get("reply") for v in results.values())
    goal = run.get("goal") or {}
    to = (goal.get("to") or "").strip() or None
    is_self = bool(to and own_account and to.lower() == own_account.lower())

    return NotificationBrief(
        event_type="reply_received" if replied else ("no_reply" if cp.get("no_reply") else "reply_received"),
        verified=replied,
        agent_run_id=str(run.get("id")),
        receipt_id=f"{run.get('id')}:{NOTIFY_PURPOSE}",
        actor_relationship=Relationship.self_ if is_self else Relationship.unknown,
        # A contact name / relationship / grounded summary needs entity resolution and a summariser that
        # can be held to grounding. Neither exists yet, so they stay None and the floor says less rather
        # than guessing — "they replied" is true; "mr. smith said friday" would not be.
        actor_name=None, reply_summary=None,
        source_entity_id=(next((v.get("reply_id") for v in results.values()
                                if isinstance(v, dict) and v.get("reply_id")), None)),
    )


def compose_notification(run: dict, *, own_account: str | None = None) -> str:
    """What the student actually reads: the deterministic floor over the verified brief, validated.

    The floor is used directly today. A phrasing model plugs in here later, and it can only ever REPHRASE
    what the brief already asserts — `validate_notification` is the gate, and a failed rewrite falls back
    to this string rather than shipping something ungrounded.
    """
    brief = brief_for(run, own_account=own_account)
    text = deterministic_notification(brief)
    verdict = validate_notification(text, brief)
    if not verdict.ok:
        log.error("notification_floor_failed_validation run=%s issues=%s", run.get("id"), verdict.issues)
        return "u got a reply"           # the smallest true thing we can say
    return text


class RelayNotifier:
    """Production Notifier. Hands the notification to the durable outbound queue the relay drains.

    Returns only on a durable, deduplicated enqueue. Everything else raises, so the runner retries.
    """

    @staticmethod
    async def _own_account(user_id: UUID) -> str | None:
        """The student's connected Google address, so a self-thread reads as "ur reply came in" instead
        of the impersonal "they replied"."""
        try:
            from . import oauth_google
            integ = await oauth_google.get_integration(user_id)
            return getattr(integ, "provider_account_id", None) if integ else None
        except Exception:
            return None

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
                text=compose_notification(run, own_account=await self._own_account(user_id)),
                idempotency_key=idempotency_key,
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
