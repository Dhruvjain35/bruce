"""AuthorizationStore — consent that survives the turn that created it.

#120 made authorization mandatory and turn-scoped. That combination is safe and incomplete: a background
mission executes minutes or days after the turn that planned it, and an in-memory grant does not exist by
then, so `MissionExecutor` correctly refused every write and background work could not proceed at all.

This is the storage that unblocks it, under one rule: **a mission may carry only an authorization_id.**
Never a copied verdict, never `approved=True`, never a snapshot of the check's result. A boolean that was
true once is not evidence that it is true now, and the entire gap between planning and waking is the
window in which it stops being true — the student changes their mind, edits the time, says never mind,
disconnects the account, or the approval simply gets old.

So the executor reloads the row and rechecks it from scratch. `recheck()` is the ten conditions:

    same user                     same operation                not invalidated
    same conversation or scope    same arguments fingerprint    not consumed
    not expired                   no later refusal              capability still live
                                                                policy still permits

The first eight are the record and the refusal timeline. The last two are deliberately NOT properties of
the record: a Google account revoked after the approval, or an entitlement withdrawn, must stop a mission
that was perfectly authorized when it was planned. Consent is necessary and never sufficient.

WHY REFUSALS GET THEIR OWN TABLE. "No later refusal" cannot be answered from the evidence rows. A student
who says "actually never mind" at a moment when nothing is outstanding marks no authorization — and must
still stop the mission that was authorized ten minutes earlier and wakes an hour later. Refusals are
therefore recorded on their own timeline, and an authorization is compared against it by timestamp.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update

from . import authorization_evidence as ae
from . import schema
from . import user_action_boundary as uab
from .db import user_session

log = logging.getLogger("bruce.authz.store")   # CONTENT-FREE: ids, operations, verdicts

# Extra denial reasons that only a durable check can produce. Kept alongside the pure ones in
# `authorization_evidence` so a caller has ONE vocabulary of refusal to report and count.
REFUSED_SINCE = "refused_after_authorization"
CAPABILITY_NOT_LIVE = "capability_not_live"
POLICY_DENIED = "policy_denied"
UNKNOWN_AUTHORIZATION = "unknown_authorization"

# provider.operation -> the registry capability, so liveness can be rechecked from a stored row that
# knows nothing about capability names. Derived from the registry rather than hand-listed, so a renamed
# capability cannot leave a stale mapping behind.
def _capability_for(provider: str, operation: str) -> str | None:
    from . import tool_registry
    for spec in tool_registry.specs(None):
        if spec.provider == provider and spec.operation == operation:
            return spec.capability
    return None


# --- persistence ---------------------------------------------------------------------------------------

def _to_row(ev: ae.AuthorizationEvidence, *, mission_id: UUID | None,
            agent_run_id: UUID | None) -> schema.AuthorizationEvidenceRow:
    return schema.AuthorizationEvidenceRow(
        authorization_id=UUID(ev.authorization_id), user_id=ev.user_id,
        conversation_id=ev.conversation_id, trusted_message_id=ev.trusted_message_id,
        source_message_timestamp=ev.source_message_timestamp, decision_id=ev.decision_id,
        mission_id=mission_id, agent_run_id=agent_run_id, provider=ev.provider, operation=ev.operation,
        normalized_arguments=ev.normalized_arguments, arguments_fingerprint=ev.arguments_fingerprint,
        polarity=ev.polarity.value, authorization_type=ev.authorization_type.value,
        explicit_operation_request=ev.explicit_operation_request, confidence=ev.confidence,
        created_at=ev.created_at, expires_at=ev.expires_at, invalidated_at=ev.invalidated_at,
        invalidated_by_message_id=ev.invalidated_by_message_id,
        superseded_by_authorization_id=(UUID(ev.superseded_by_authorization_id)
                                        if ev.superseded_by_authorization_id else None),
        consumed_at=ev.consumed_at, consumed_by_attempt=ev.consumed_by_attempt,
        operation_receipt_id=ev.operation_receipt_id)


def _from_row(row: schema.AuthorizationEvidenceRow) -> ae.AuthorizationEvidence:
    return ae.AuthorizationEvidence(
        authorization_id=str(row.authorization_id), user_id=row.user_id,
        conversation_id=row.conversation_id, trusted_message_id=row.trusted_message_id,
        source_message_timestamp=row.source_message_timestamp, decision_id=row.decision_id,
        provider=row.provider, operation=row.operation,
        normalized_arguments=dict(row.normalized_arguments or {}),
        arguments_fingerprint=row.arguments_fingerprint, polarity=uab.Polarity(row.polarity),
        authorization_type=ae.AuthorizationType(row.authorization_type), confidence=row.confidence,
        created_at=row.created_at, expires_at=row.expires_at, invalidated_at=row.invalidated_at,
        invalidated_by_message_id=row.invalidated_by_message_id,
        superseded_by_authorization_id=(str(row.superseded_by_authorization_id)
                                        if row.superseded_by_authorization_id else None),
        consumed_at=row.consumed_at, consumed_by_attempt=row.consumed_by_attempt,
        operation_receipt_id=row.operation_receipt_id,
        explicit_operation_request=bool(row.explicit_operation_request))


async def record(ev: ae.AuthorizationEvidence, *, mission_id: UUID | None = None,
                 agent_run_id: UUID | None = None) -> str:
    """Persist a freshly minted authorization and return its id — the ONLY thing a mission may carry."""
    async with user_session(ev.user_id) as s:
        s.add(_to_row(ev, mission_id=mission_id, agent_run_id=agent_run_id))
    log.info("authz_recorded id=%s op=%s.%s mission=%s", ev.authorization_id, ev.provider, ev.operation,
             mission_id)
    return ev.authorization_id


async def load(user_id: UUID, authorization_id: str) -> ae.AuthorizationEvidence | None:
    """Read one authorization back. RLS scopes the query to the owner, so a mission that somehow carried
    another student's id gets None here rather than a row it could be talked into trusting."""
    try:
        aid = UUID(str(authorization_id))
    except (ValueError, AttributeError, TypeError):
        return None
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.AuthorizationEvidenceRow).where(
            schema.AuthorizationEvidenceRow.authorization_id == aid))).scalar_one_or_none()
    return _from_row(row) if row is not None else None


# --- the invalidation chain ----------------------------------------------------------------------------

async def record_refusal(user_id: UUID, boundary: uab.UserActionBoundary, *,
                         message_id: str | None = None, conversation_id: str | None = None,
                         at: datetime | None = None) -> None:
    """Write one refusal to the timeline AND close every outstanding authorization for this user.

    Both halves matter. Closing the outstanding rows is what makes the next reload report INVALIDATED
    with the message that caused it; the timeline entry is what still stops an authorization whose row
    was written before this refusal but which nobody had closed — a mission planned at 4:00, refused at
    4:05 while nothing was outstanding, waking at 5:00.

    Deliberately unconditional about scope: a refusal closes work in EVERY conversation, not only the one
    it was typed in. A student saying "stop" means stop, and asking them which thread they meant is not a
    conversation anyone wants to have while something irreversible is queued.
    """
    if not boundary.blocks_execution():
        return
    now = at or datetime.now(timezone.utc)
    async with user_session(user_id) as s:
        s.add(schema.AuthorizationRefusal(
            user_id=user_id, refused_at=now, message_id=message_id,
            polarity=boundary.polarity.value, conversation_id=conversation_id))
        await s.execute(
            update(schema.AuthorizationEvidenceRow)
            .where(schema.AuthorizationEvidenceRow.user_id == user_id,
                   schema.AuthorizationEvidenceRow.invalidated_at.is_(None),
                   schema.AuthorizationEvidenceRow.consumed_at.is_(None))
            .values(invalidated_at=now, invalidated_by_message_id=message_id))
    log.info("authz_refusal_recorded polarity=%s", boundary.polarity.value)


async def refused_since(user_id: UUID, since: datetime) -> bool:
    """Did the student refuse anything after this moment? The second half of "no later refusal"."""
    async with user_session(user_id) as s:
        found = (await s.execute(select(schema.AuthorizationRefusal.id).where(
            schema.AuthorizationRefusal.user_id == user_id,
            schema.AuthorizationRefusal.refused_at > since).limit(1))).first()
    return found is not None


async def supersede(user_id: UUID, old_id: str, new: ae.AuthorizationEvidence) -> None:
    """Close an authorization out and point it at its replacement, so an audit can follow what the
    student actually settled on in one direction."""
    async with user_session(user_id) as s:
        await s.execute(
            update(schema.AuthorizationEvidenceRow)
            .where(schema.AuthorizationEvidenceRow.authorization_id == UUID(old_id),
                   schema.AuthorizationEvidenceRow.user_id == user_id)
            .values(superseded_by_authorization_id=UUID(new.authorization_id),
                    invalidated_at=new.created_at))


async def mark_consumed(user_id: UUID, authorization_id: str, *, attempt_key: str,
                        receipt_id: str | None = None, at: datetime | None = None) -> None:
    """Spend it. Scoped to the attempt so an exactly-once RETRY of the same attempt still resolves, while
    a second, different operation reaching for the same consent does not.

    Written only after the provider call returns. Marking it first would turn a transport failure into a
    permanently unusable authorization and strand work the student did agree to.
    """
    async with user_session(user_id) as s:
        await s.execute(
            update(schema.AuthorizationEvidenceRow)
            .where(schema.AuthorizationEvidenceRow.authorization_id == UUID(authorization_id),
                   schema.AuthorizationEvidenceRow.user_id == user_id)
            .values(consumed_at=at or datetime.now(timezone.utc), consumed_by_attempt=attempt_key,
                    operation_receipt_id=receipt_id))


async def history(user_id: UUID, *, limit: int = 50) -> list[dict]:
    """The audit trail: what was authorized, by which message, when it died and why.

    Content-free by construction — ids, operations, timestamps and verdicts, never message text or
    argument values. It answers "what did Bruce believe it was allowed to do, and on whose say-so",
    which is the question anyone actually has after something unexpected happens.
    """
    async with user_session(user_id) as s:
        rows = (await s.execute(
            select(schema.AuthorizationEvidenceRow)
            .where(schema.AuthorizationEvidenceRow.user_id == user_id)
            .order_by(schema.AuthorizationEvidenceRow.created_at.desc()).limit(limit))).scalars().all()
    return [{"authorization_id": str(r.authorization_id), "operation": f"{r.provider}.{r.operation}",
             "type": r.authorization_type, "trusted_message_id": r.trusted_message_id,
             "decision_id": r.decision_id, "mission_id": str(r.mission_id) if r.mission_id else None,
             "fingerprint": r.arguments_fingerprint[:12], "created_at": r.created_at,
             "expires_at": r.expires_at, "invalidated_at": r.invalidated_at,
             "invalidated_by_message_id": r.invalidated_by_message_id,
             "superseded_by": (str(r.superseded_by_authorization_id)
                               if r.superseded_by_authorization_id else None),
             "consumed_at": r.consumed_at, "receipt": r.operation_receipt_id} for r in rows]


# --- the ten-condition recheck --------------------------------------------------------------------------

async def recheck(user_id: UUID, authorization_id: str | None, *, provider: str, operation: str,
                  arguments: dict | None, conversation_id: str | None = None,
                  attempt_key: str | None = None, revalidate_grant: bool = True,
                  now: datetime | None = None) -> tuple[ae.AuthorizationEvidence | None, str]:
    """Reload an authorization by id and judge it against the world as it is NOW.

    Returns (evidence, reason). `reason` is `ae.ALLOW` or exactly one typed denial. This is what a waking
    background executor calls, and it is deliberately the same shape as the foreground check — a mission
    is not held to a weaker standard because it is asleep. If anything, the opposite: everything below the
    pure check exists because time has passed.
    """
    now = now or datetime.now(timezone.utc)
    if not authorization_id:
        return None, ae.NO_AUTHORIZATION

    ev = await load(user_id, authorization_id)
    if ev is None:
        # Either it never existed or it belongs to another student and RLS hid it. Both are the same
        # answer here, and neither is worth telling the caller apart — a mission holding an id it cannot
        # load has nothing.
        log.warning("authz_unknown id=%s", authorization_id)
        return None, UNKNOWN_AUTHORIZATION

    reason = ae.check(ev, user_id=user_id, provider=provider, operation=operation, arguments=arguments,
                      conversation_id=conversation_id, attempt_key=attempt_key, now=now)
    if reason != ae.ALLOW:
        return ev, reason

    if await refused_since(user_id, ev.created_at):
        # The row itself is clean, and the student has since said no to something. The safer reading of an
        # ambiguous scope is that it covered this.
        log.warning("authz_refused_since id=%s", ev.authorization_id)
        return ev, REFUSED_SINCE

    capability = _capability_for(provider, operation)
    if capability is None:
        return ev, CAPABILITY_NOT_LIVE
    from . import tool_broker
    try:
        available = (await tool_broker.availability(user_id, capability)).ok
    except Exception:
        available = False           # unreadable capability truth narrows what Bruce attempts, never widens
    if not available:
        # Authorized, and the account was disconnected or the scope withdrawn while the mission slept.
        # Consent was never sufficient on its own.
        return ev, CAPABILITY_NOT_LIVE

    if not revalidate_grant:
        # A foreground turn passed the SAME coarse gate at intake, one call ago. Re-deciding it here
        # would let a turn be half-processed — accepted at the door and refused at the write — for a
        # grant that has not changed. The wake path passes True, because a mission has no intake gate and
        # its whole risk is the time that passed.
        return ev, ae.ALLOW

    from . import access_control
    try:
        # The COARSE gate — entitlement, staging enrollment, global kill switch. Deliberately not the tool
        # capability: `access_control` speaks in access grants ("conversation"), and handing it
        # "calendar.delete_event" would find no grant and deny every write. Capability-level truth is the
        # broker's answer above; this one asks whether Bruce may act for this student at all, which is the
        # thing that can be revoked between planning a mission and waking it.
        permitted = bool((await access_control.conversation_access(user_id)).allow)
    except Exception:
        permitted = False
    if not permitted:
        return ev, POLICY_DENIED

    return ev, ae.ALLOW


def invalidated_copy(ev: ae.AuthorizationEvidence, *, by_message_id: str | None) -> ae.AuthorizationEvidence:
    """The in-memory twin of `record_refusal`'s UPDATE, for callers holding an object rather than a row."""
    return replace(ae.invalidate(ev, by_message_id=by_message_id))
