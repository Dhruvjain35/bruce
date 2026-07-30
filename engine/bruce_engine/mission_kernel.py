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

from . import agent_run_store, execution_gate, schema, transitions
from .contract import MachineState
from .db import user_session
from .models import MissionPhase

log = logging.getLogger("bruce.mission")   # content-free: ids/phases only, never user text

HANDOFF_KIND = "handoff"
_MAX_KEY = 128


# --- the phase log is a state machine too --------------------------------------------------------------
# `record_phase` used to be `m.phase = phase`, which meant the phase log — the thing the Live Activity
# renders and the thing a student reads to decide whether Bruce actually did something — could be moved
# anywhere by anyone. A mission could go from "awaiting your ok" straight to "Added and verified" with no
# provider call in between, and nothing in the code would object. So phases are proposed now, through the
# SAME gate `agent_run_store` uses, and refused moves raise `IllegalStatusWrite`.
#
# `MissionPhase` is the render vocabulary and `MachineState` is the machine's; they overlap on eight of
# ten words. The two that differ are projections, not new states: `created` is the instant before
# `understanding`, and `extracting` is what `preparing` looks like on screen.

_PHASE_TO_MACHINE: dict[str, MachineState] = {
    MissionPhase.created.value: MachineState.understanding,
    MissionPhase.understanding.value: MachineState.understanding,
    MissionPhase.extracting.value: MachineState.preparing,
    MissionPhase.awaiting_approval.value: MachineState.awaiting_approval,
    MissionPhase.executing.value: MachineState.executing,
    MissionPhase.waiting_external.value: MachineState.waiting_external,
    MissionPhase.verifying.value: MachineState.verifying,
    MissionPhase.succeeded.value: MachineState.succeeded,
    MissionPhase.blocked.value: MachineState.blocked,
    MissionPhase.failed.value: MachineState.failed,
    # not a MissionPhase — the row never renders it — but a mission whose decision was declined is
    # cancelled, and the projection has to be total or the gate cannot judge that mission at all.
    MachineState.cancelled.value: MachineState.cancelled,
}

# The one edge the phase LOG walks that the machine table does not have. `transitions.py` requires a
# capability, a live connection and an open authorization before `executing`, and it is right to: that is
# the check that must happen before a provider is called. But it happens at `mutation_gateway` — the ONE
# DOOR, one statement later in `calendar_schedule` — and re-asking it here would mean either duplicating
# the broker + authorization lookups or, worse, answering them from stale columns. The phase log RECORDS
# what the door already permitted; it is not a second door. It still cannot record an unverified success:
# `check_operational_lane` forbids any supplement from naming `succeeded`.
_PHASE_LANE: dict[str, frozenset[str]] = {
    MachineState.preparing.value: frozenset({MachineState.executing.value}),
}
agent_run_store.check_operational_lane(_PHASE_LANE, "the mission phase-log lane")

# Mission ROW statuses. A coarse rollup of the phase for list views — not a third state machine, and it
# must never disagree with the phase, which is what `_authorize_phase` enforces for the only value that
# makes a claim about the world.
MISSION_STATUSES: frozenset[str] = frozenset({"running", "succeeded", "failed", "cancelled", "done"})


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
    extracted_facts: dict | None = None,
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
        "extracted_facts": extracted_facts or {},          # grounded flyer facts (name/date/location/…)
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


CALENDAR_CAPABILITY = "calendar.create_event"


async def create_pending_calendar_approval(
    user_id: UUID, *, source_message_id: str, event, attachment_digest: str = "",
    facts: dict | None = None, attachment_refs: list[dict] | None = None,
) -> "MissionCreation":
    """Create a durable mission holding a PENDING calendar-create decision + the exact event to run on
    approval. This is the state that carries authorization forward: when Bruce offers "add it to ur
    calendar?", the offer becomes a real awaiting_approval mission, so the student's later "ya" resolves
    THIS decision and continues the same run — instead of re-asking from scratch (the live loop bug).

    Idempotent on (owner, source message, capability): re-offering the same flyer references the existing
    pending decision rather than stacking duplicates."""
    key = handoff_idempotency_key(source_message_id, CALENDAR_CAPABILITY)
    goal = {
        "capability": CALENDAR_CAPABILITY,
        "proposed_goal": f"add {event.title} to calendar"[:200],
        "source_message_ids": [source_message_id],
        "source_attachment_refs": attachment_refs or [],
        "attachment_digest": attachment_digest,
        "extracted_facts": facts or {},
        # the EXACT event to create on approval — so approval executes what was offered, not a re-parse
        "pending_event": {"title": event.title, "start": event.start, "end": event.end,
                          "location": event.location, "source": event.source},
        # NO status here. The mission ROW (status + phase) is the single source of decision truth;
        # a second copy in JSON is a divergence waiting to happen and one already occurred: a mission
        # cancelled by a refusal still read `goal.decision.status == "pending"`. Nothing consumed it, so
        # it was a trap rather than a live bug — removed at the source instead of kept in sync.
        # Read `decision_status(mission)` instead.
        "decision": {"type": "approve_calendar_create"},
    }
    phase = MissionPhase.awaiting_approval.value
    async with user_session(user_id) as s:
        existing = (await s.execute(select(schema.Mission).where(
            schema.Mission.user_id == user_id,
            schema.Mission.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            return MissionCreation(mission_id=existing.id, created=False, phase=existing.phase)
        mission = schema.Mission(
            user_id=user_id, kind=HANDOFF_KIND, status="running", phase=phase,
            short_status=f"awaiting ok: add {event.title} to calendar"[:200], goal=goal, idempotency_key=key)
        s.add(mission)
        try:
            await s.flush()
        except IntegrityError:
            async with user_session(user_id) as s2:
                ex = (await s2.execute(select(schema.Mission).where(
                    schema.Mission.user_id == user_id,
                    schema.Mission.idempotency_key == key))).scalar_one_or_none()
                if ex is not None:
                    return MissionCreation(mission_id=ex.id, created=False, phase=ex.phase)
            raise
        s.add(schema.MissionPhaseEvent(
            user_id=user_id, mission_id=mission.id, phase=phase, short_status="awaiting_approval"))
        await s.flush()
        return MissionCreation(mission_id=mission.id, created=True, phase=phase)


# Phases/statuses that mean the decision is CLOSED. Derived in one place so callers never re-derive it.
_RESOLVED_PHASES = {"blocked", "succeeded", "failed", "cancelled"}
_RESOLVED_STATUSES = {"cancelled", "succeeded", "failed", "done"}


def decision_status(mission) -> str:
    """The ONE canonical read of a pending decision's state: pending | rejected | resolved.

    Derived from the mission row, never from the embedded `goal.decision` JSON — that field carried a
    stale "pending" on an already-cancelled mission. Accepts a row or a dict so legacy rows that still
    contain the old field resolve correctly regardless.
    """
    def _g(k):
        return mission.get(k) if isinstance(mission, dict) else getattr(mission, k, None)

    phase, status = (_g("phase") or ""), (_g("status") or "")
    short = (_g("short_status") or "").lower()
    if "reject" in short or status == "cancelled":
        return "rejected"
    if phase in _RESOLVED_PHASES or status in _RESOLVED_STATUSES:
        return "resolved"
    if phase == MissionPhase.awaiting_approval.value:
        return "pending"
    return "resolved"


async def latest_pending_calendar_mission(user_id: UUID) -> dict | None:
    """The most recent OPEN calendar-create decision awaiting the student's ok. Owner-scoped; None if
    there is nothing pending. Backs 'ya' / 'add it' continuing the exact offered event."""
    async with user_session(user_id) as s:
        m = (await s.execute(select(schema.Mission).where(
            schema.Mission.user_id == user_id,
            schema.Mission.kind == HANDOFF_KIND,
            schema.Mission.status == "running",
            schema.Mission.phase == MissionPhase.awaiting_approval.value,
            schema.Mission.goal["capability"].astext == CALENDAR_CAPABILITY).order_by(
            schema.Mission.created_at.desc()).limit(1))).scalar_one_or_none()
        if m is None:
            return None
        return {"mission_id": str(m.id), "goal": m.goal}


# --- the founder-alpha semantic-rescue proposal -------------------------------------------------------
# A rescued proposal is a pending Decision, and this file already knows how to represent one. Giving it a
# second home — a `rescue_proposals` table, a cache, an in-memory map — would mean two answers to "is
# something awaiting this student's ok", and the two would disagree on the day one of them is written and
# the other is not. So a rescue proposal IS a mission row in `awaiting_approval`, read back by the same
# owner-scoped query shape as `latest_pending_calendar_mission`, and closed by the same `record_phase`.
#
# The `decision` discriminator is what keeps the two kinds apart. `latest_pending_calendar_mission` filters
# on `goal.capability == "calendar.create_event"` and a rescue proposal stores the gate's own capability
# key ("google_calendar.create_event"), so neither query can return the other's row — which matters,
# because the calendar approval handler resolves a decision WITHOUT the arguments-fingerprint check the
# rescue path is built around, and answering a rescue proposal through it would skip that check.

RESCUE_DECISION_TYPE = "rescue_proposal"


def rescue_idempotency_key(source_message_id: str, capability: str) -> str:
    """Owner + source message + intended capability, like every other mission key. A relay redelivery of
    the turn that produced the offer references the SAME pending Decision instead of stacking a second."""
    return f"rescue:{capability}:{source_message_id}"[:_MAX_KEY]


async def create_pending_rescue_proposal(
    user_id: UUID, *, pending, presentation: str, proposed_goal: str, goal_spec: dict | None = None,
) -> MissionCreation:
    """Persist ONE canonical pending Decision for a semantic-rescue proposal. Takes no external action.

    `pending` is a `semantic_rescue.PendingProposal` — a pure value object. The whole point of the split is
    that the thing being written here was derived deterministically from the model's reading, so what lands
    on disk is a concrete operation with exact arguments and an exact fingerprint, not a paraphrase.

    Note which id survives. `build_pending` mints a `decision_id` before there is a row to point at; the
    row's own id is what every later turn resolves against, so the loader below reports the MISSION id as
    the decision id. Carrying both would give one Decision two names, and the authorization record binds to
    exactly one of them.
    """
    capability = f"{pending.provider}.{pending.operation}"
    key = rescue_idempotency_key(pending.source_message_id or "", capability)
    goal = {
        "capability": capability,
        "proposed_goal": proposed_goal[:200],
        "source_message_ids": [pending.source_message_id] if pending.source_message_id else [],
        "decision": {"type": RESCUE_DECISION_TYPE},
        # The proposal verbatim. `arguments_fingerprint` is the load-bearing field: the approval turn is
        # judged against it, and execution re-derives it from what is actually about to reach the provider.
        "rescue": {
            "goal_id": pending.goal_id,
            "provider": pending.provider,
            "operation": pending.operation,
            "normalized_arguments": pending.normalized_arguments,
            "arguments_fingerprint": pending.arguments_fingerprint,
            "trusted_authorization_required": pending.trusted_authorization_required,
            "conversation_id": pending.conversation_id,
            "source_message_id": pending.source_message_id,
            "created_at": pending.created_at.isoformat(),
            "expires_at": pending.expires_at.isoformat(),
            "presentation": presentation,
            "goal_spec": goal_spec or {},
        },
    }
    phase = MissionPhase.awaiting_approval.value
    async with user_session(user_id) as s:
        existing = (await s.execute(select(schema.Mission).where(
            schema.Mission.user_id == user_id,
            schema.Mission.idempotency_key == key))).scalar_one_or_none()
        if existing is not None:
            return MissionCreation(mission_id=existing.id, created=False, phase=existing.phase)
        mission = schema.Mission(
            user_id=user_id, kind=HANDOFF_KIND, status="running", phase=phase,
            short_status=f"awaiting ok: {proposed_goal}"[:200], goal=goal, idempotency_key=key)
        s.add(mission)
        try:
            await s.flush()
        except IntegrityError:
            async with user_session(user_id) as s2:
                ex = (await s2.execute(select(schema.Mission).where(
                    schema.Mission.user_id == user_id,
                    schema.Mission.idempotency_key == key))).scalar_one_or_none()
                if ex is not None:
                    return MissionCreation(mission_id=ex.id, created=False, phase=ex.phase)
            raise
        s.add(schema.MissionPhaseEvent(
            user_id=user_id, mission_id=mission.id, phase=phase, short_status="rescue_awaiting_approval"))
        await s.flush()
        log.info("rescue_proposal_pending mission_id=%s cap=%s", mission.id, capability)
        return MissionCreation(mission_id=mission.id, created=True, phase=phase)


async def latest_pending_rescue_proposal(user_id: UUID) -> dict | None:
    """The most recent OPEN rescue proposal awaiting the founder's ok. Owner-scoped; None if nothing is
    pending. Deliberately keyed on the `decision.type` discriminator rather than on the capability, so
    adding a second rescued operation does not need a second query."""
    async with user_session(user_id) as s:
        m = (await s.execute(select(schema.Mission).where(
            schema.Mission.user_id == user_id,
            schema.Mission.kind == HANDOFF_KIND,
            schema.Mission.status == "running",
            schema.Mission.phase == MissionPhase.awaiting_approval.value,
            schema.Mission.goal["decision"]["type"].astext == RESCUE_DECISION_TYPE).order_by(
            schema.Mission.created_at.desc()).limit(1))).scalar_one_or_none()
        if m is None:
            return None
        return {"mission_id": str(m.id), "goal": m.goal}


_SUSPENDED_RECEIPT = "provider-semantics-test-suspension"   # never persisted; guard evidence only


async def _operation_receipt(s, user_id: UUID, mission_id: UUID) -> str | None:
    """The provider entity id this mission's OWN consumed authorization recorded, or None.

    This is the read-back evidence, taken from the database rather than from an argument. When a mutation
    goes through `mutation_gateway`, the authorization is marked consumed with `operation_receipt_id` set
    to what the provider actually returned (`receipt_of`) — and that happens BEFORE the caller records
    `succeeded`. So the question "is there proof this write produced a real object" already has a durable
    answer, and a mission cannot be argued into `succeeded` by a caller that merely believes it worked.
    """
    return (await s.execute(select(schema.AuthorizationEvidenceRow.operation_receipt_id).where(
        schema.AuthorizationEvidenceRow.user_id == user_id,
        schema.AuthorizationEvidenceRow.mission_id == mission_id,
        schema.AuthorizationEvidenceRow.consumed_at.isnot(None),
        schema.AuthorizationEvidenceRow.operation_receipt_id.isnot(None)).order_by(
        schema.AuthorizationEvidenceRow.consumed_at.desc()).limit(1))).scalar_one_or_none()


async def record_phase(
    user_id: UUID, mission_id: UUID, phase: str, short_status: str, *, status: str | None = None,
    verified_read_back: bool | None = None, read_back_entity_id: str | None = None,
) -> bool:
    """Append ONE durable phase event to an existing mission and PROPOSE its live phase/short_status
    (optionally its status). Owner-scoped; a no-op returning False if the mission isn't the caller's.
    Raises ``agent_run_store.IllegalStatusWrite`` if the move is not one the machine allows.

    This is how an EXECUTING capability (e.g. the real calendar write) records the honest states the
    product must never skip — creation_attempted -> created -> fetched_back -> verified / failed /
    verification_inconclusive — each as a persisted event, never merely a log line.

    ``succeeded`` is the state this function exists to protect. It is reachable only from ``verifying``,
    and only with an independent read-back: pass ``verified_read_back`` / ``read_back_entity_id`` if you
    are holding the provider's answer, otherwise the mission's own consumed-authorization receipt is
    looked up and used. If neither exists, the move is REFUSED and the mission stays in ``verifying`` —
    which is the honest state for a write nobody has proven, and the one the student should be shown.
    """
    async with user_session(user_id) as s:
        m = (await s.execute(select(schema.Mission).where(
            schema.Mission.id == mission_id, schema.Mission.user_id == user_id))).scalar_one_or_none()
        if m is None:
            return False

        current, target = _PHASE_TO_MACHINE.get(m.phase or ""), _PHASE_TO_MACHINE.get(phase)
        if current is None or target is None:
            # An unrecognised phase word cannot be judged, and a phase nothing can judge is exactly the
            # free-text `required_capabilities=["sending messages"]` failure in another column.
            raise agent_run_store.IllegalStatusWrite(
                m.phase or "", phase, agent_run_store.UNKNOWN_STATUS)
        if current in (MachineState.succeeded, MachineState.cancelled):
            # A finished mission does not move, and a redelivery is not an error. At-least-once transports
            # re-hand the same flyer, and `calendar_schedule` answers a repeat by re-walking its graph —
            # the provider's 409 keeps that safe, but the PHASE log must not be dragged back to
            # "preparing" and then forward to a second "verified". Recording nothing is what "terminal"
            # means; raising would make a normal redelivery look like a defect.
            log.info("mission_phase_ignored_terminal mission_id=%s phase=%s", mission_id, m.phase)
            return False
        if status is not None and status not in MISSION_STATUSES:
            raise agent_run_store.IllegalStatusWrite(m.status or "", status, agent_run_store.UNKNOWN_STATUS)
        if status == MachineState.succeeded.value and target is not MachineState.succeeded:
            # Marking the ROW succeeded while the phase says otherwise would route around the read-back
            # guard through the one field the guard does not look at.
            raise agent_run_store.IllegalStatusWrite(
                m.status or "", status, transitions.NOT_FROM_VERIFYING, (transitions.VERIFIED_READ_BACK,))

        entity_id, verified = read_back_entity_id, verified_read_back
        if target is MachineState.succeeded and not (entity_id or "").strip():
            entity_id = await _operation_receipt(s, user_id, mission_id)
            if entity_id is None and execution_gate.unchecked():
                # The ONE declared suspension (`unchecked_provider_writes_for_test`, which raises outside
                # pytest). Those tests call the verified I/O directly, so the gateway that writes the
                # receipt never ran and there is nothing to find. Refusing here would fail them for the
                # single thing they declared out of scope — and would prove nothing about the guard, which
                # is exercised head-on in test_status_enforcement.py.
                entity_id = _SUSPENDED_RECEIPT
        if verified is None:
            # The provider id IS the read-back. A caller that has one has verified by definition; a caller
            # that explicitly says False is believed, because it knows something this row does not.
            verified = bool((entity_id or "").strip())

        agent_run_store.enforce_status_write(
            current.value, target.value, extra_edges=_PHASE_LANE,
            guard_ctx=transitions.GuardContext(
                capability=(m.goal or {}).get("capability"),
                decision_id=str(mission_id),        # the mission row IS the decision — see the rescue note
                verified_read_back=bool(verified),
                read_back_entity_id=entity_id))

        m.phase = phase
        m.short_status = short_status[:200]
        if status is not None:
            m.status = status
        s.add(schema.MissionPhaseEvent(
            user_id=user_id, mission_id=mission_id, phase=phase, short_status=short_status[:200]))
        await s.flush()
        log.info("mission_phase mission_id=%s phase=%s status=%s", mission_id, phase, status or m.status)
        return True


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
