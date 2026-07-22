"""Mission kernel — durable mission CREATION from a conversation handoff (A1).

The moment Bruce crosses from conversation software into durable agency: an authorized handoff creates ONE
durable Mission + its FIRST phase event, atomically, and STOPS. No external action is taken here — A1 only
captures and tracks. Execution, approval, verification, and recovery are later phases.

Deliberately built on the LIVE substrate proven reachable by the reachability audit — ``schema.Mission`` +
``schema.MissionPhaseEvent`` + ``user_session`` RLS. It does NOT touch the dead ``approvals`` / ``receipts``
tables or ``contract.py``'s unused state machine; those get real contracts later, behind real use cases.

Guarantees (integration + A1 merge bar):
  * mission row + first phase event commit in ONE transaction (a failure orphans neither).
  * idempotent on (owner, source message, capability) — a relay redelivery or a repeated handoff REFERENCES
    the existing mission, never creates a second (owner via user_session + uq(user_id, idempotency_key)).
  * source message + attachment refs + evidence + autonomy + risk + proposed goal are linked in goal JSONB.
  * created under the caller's user_session, so Postgres RLS enforces tenant isolation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import schema
from .db import user_session
from .models import MissionPhase

log = logging.getLogger("bruce.mission")   # content-free: ids/phases only, never user text

HANDOFF_KIND = "handoff"
_MAX_KEY = 128


def handoff_idempotency_key(source_message_id: str, capability: str) -> str:
    """Idempotency is tied to owner + source message + intended capability. Owner is supplied by the
    user_session + the uq(user_id, idempotency_key) constraint, so it isn't in the string."""
    return f"handoff:{capability}:{source_message_id}"[:_MAX_KEY]


@dataclass
class MissionCreation:
    mission_id: UUID
    created: bool          # False = an existing matching mission was referenced (redelivery / re-handoff)
    phase: str


async def create_handoff_mission(
    user_id: UUID, *, capability: str, source_message_id: str, proposed_goal: str,
    short_status: str, autonomy: str = "A0", risk: str = "low",
    attachment_refs: list[dict] | None = None, evidence: dict | None = None,
) -> MissionCreation:
    """Create (or reference, if it already exists) the durable handoff mission + its first phase event in
    ONE transaction. Returns which happened. Performs NO external action."""
    key = handoff_idempotency_key(source_message_id, capability)
    goal = {
        "capability": capability,
        "proposed_goal": proposed_goal,
        "source_message_ids": [source_message_id],
        "source_attachment_refs": attachment_refs or [],   # metadata refs only, never bytes
        "evidence": evidence or {},
        "autonomy": autonomy,
        "risk": risk,
    }
    phase = MissionPhase.understanding.value                # live phase: Bruce captured it, is understanding

    async with user_session(user_id) as s:
        existing = (await s.execute(select(schema.Mission).where(
            schema.Mission.user_id == user_id,
            schema.Mission.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            log.info("mission_handoff_referenced mission_id=%s cap=%s", existing.id, capability)
            return MissionCreation(mission_id=existing.id, created=False, phase=existing.phase)

        mission = schema.Mission(
            user_id=user_id, kind=HANDOFF_KIND, status="running", phase=phase,
            short_status=short_status[:200], goal=goal, idempotency_key=key)
        s.add(mission)
        try:
            await s.flush()                                # assign id; uq rejects a concurrent duplicate
        except IntegrityError:
            # a concurrent identical handoff won the race -> reference it, don't duplicate
            async with user_session(user_id) as s2:
                ex = (await s2.execute(select(schema.Mission).where(
                    schema.Mission.user_id == user_id,
                    schema.Mission.idempotency_key == key))).scalar_one_or_none()
                if ex is not None:
                    return MissionCreation(mission_id=ex.id, created=False, phase=ex.phase)
            raise

        # first phase event in the SAME transaction -> mission + event are atomic (no orphan on failure)
        s.add(schema.MissionPhaseEvent(
            user_id=user_id, mission_id=mission.id, phase=phase, short_status=short_status[:200]))
        await s.flush()
        log.info("mission_handoff_created mission_id=%s cap=%s phase=%s", mission.id, capability, phase)
        return MissionCreation(mission_id=mission.id, created=True, phase=phase)


async def latest_active_handoff_mission(user_id: UUID) -> dict | None:
    """The most recent OPEN handoff mission for this user (backs a status question like 'what are u doing
    with that?'). Active = status 'running'. Owner-scoped; None if there is no open handoff mission."""
    async with user_session(user_id) as s:
        m = (await s.execute(select(schema.Mission).where(
            schema.Mission.user_id == user_id,
            schema.Mission.kind == HANDOFF_KIND,
            schema.Mission.status == "running").order_by(
            schema.Mission.created_at.desc()).limit(1))).scalar_one_or_none()
        if m is None:
            return None
        return {"mission_id": str(m.id), "kind": m.kind, "status": m.status, "phase": m.phase,
                "short_status": m.short_status, "goal": m.goal}


async def get_mission_state(user_id: UUID, mission_id: UUID) -> dict | None:
    """Owner-scoped read of a mission's persisted state — backs 'what are u doing with that?'. Content-safe
    (returns the durable goal/phase/status, never chain-of-thought). None if not found / not the owner."""
    async with user_session(user_id) as s:
        m = (await s.execute(select(schema.Mission).where(
            schema.Mission.id == mission_id, schema.Mission.user_id == user_id))).scalar_one_or_none()
        if m is None:
            return None
        events = (await s.execute(select(schema.MissionPhaseEvent).where(
            schema.MissionPhaseEvent.mission_id == mission_id,
            schema.MissionPhaseEvent.user_id == user_id).order_by(
            schema.MissionPhaseEvent.created_at))).scalars().all()
        return {"mission_id": str(m.id), "kind": m.kind, "status": m.status, "phase": m.phase,
                "short_status": m.short_status, "goal": m.goal,
                "phase_events": [{"phase": e.phase, "short_status": e.short_status} for e in events]}
