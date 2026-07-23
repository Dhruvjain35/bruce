"""Outcome-dispatch seam (D-INT-1, hardened by D-INT-3 to integration invariant 1).

``conversation_runtime.handle()`` turns ONE conversation decision into ONE reply by running a two-phase
handler pipeline — NOT an implicit first-match if/else tree:

  Phase 1 — EVALUATE (pure): every handler returns a HandlerVerdict {claim | decline | blocked, priority,
    reason, telemetry}. Evaluation MUST NOT mutate durable state and MUST NOT enqueue anything.
  Selection (deterministic, in the runtime): claims take precedence over blocked; among the owners the
    single highest PRIORITY wins; a tie at the top priority FAILS LOUDLY (raises + privacy-safe telemetry);
    ZERO owners route through one explicit fallback handler.
  Phase 2 — EXECUTE (only the selected handler): may mutate + returns a HandlerOutput (raw factual text +
    whether it should be voice-styled + side effects to record).

Presentation is RUNTIME-owned and runs AFTER selection: the runtime finalizes the factual output, applies
safety gates, and applies style — never a handler. So safety gates run for EVERY outcome uniformly and no
handler can skip them.

A workstream adds a capability by adding a handler with a stable priority; it never edits handle().
``ConversationDecision`` stays frozen at 13 fields; a handoff is decided by the deterministic
``handoff`` policy, not by a model flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from . import handoff, mission_kernel

if TYPE_CHECKING:
    from .conversation_context import ContextCapsule
    from .conversation_contract import ConversationDecision
    from .conversation_style import ConversationStyleEngine
    from .messaging import InboundMessage

log = logging.getLogger("bruce.conversation")   # content-free: ids/dispositions/reasons, never user text


# --------------------------------------------------------------------------------------------------
# Event helpers (relocated from the runtime in D-INT-1; event-OUTCOME logic).
# --------------------------------------------------------------------------------------------------
def _event_fields(decision: "ConversationDecision") -> tuple[str, str, str, dict]:
    """Build (title, when, where, provenance) from grounded entities. Provenance keeps verbatim spans."""
    title = when = where = ""
    spans = []
    for e in decision.extracted_entities:
        et = (e.type or "").lower()
        if not title and ("title" in et or "event" in et or "name" in et):
            title = e.value
        elif "date" in et or "time" in et or "day" in et:
            when = (when + " " + (e.normalized or e.value)).strip()
        elif "location" in et or "place" in et or "venue" in et or "address" in et:
            where = e.value
        if e.source_span:
            spans.append({"type": e.type, "span": e.source_span})
    title = title or (decision.proposed_goal or "your event")
    when = (when + "\n") if when else ""
    where = where or ""
    return title, when, where, {"entities": spans}


def _is_event(decision: "ConversationDecision") -> bool:
    """An event = a title-like AND a date-like entity. Robust to model phrasing."""
    types = [(e.type or "").lower() for e in decision.extracted_entities]
    has_title = any(("title" in t) or ("event" in t) or ("name" in t) for t in types)
    has_date = any(("date" in t) or ("time" in t) or ("day" in t) for t in types)
    return has_title and has_date


def _wants_calendar(decision: "ConversationDecision", msg_text: str | None) -> bool:
    from .conversation_contract import IntentKind
    if decision.intent is IntentKind.actionable:
        return True
    if any("calendar" in c.lower() for c in decision.required_capabilities):
        return True
    return "calendar" in (msg_text or "").lower()


# --------------------------------------------------------------------------------------------------
# Seam types
# --------------------------------------------------------------------------------------------------
class Disposition(Enum):
    claim = "claim"                    # I own this outcome and will produce the reply
    decline = "decline"               # not my concern — pass
    blocked = "blocked"               # this IS my concern but I cannot complete it (honest block)


class OutcomeCollision(Exception):
    """Two+ handlers claimed the SAME top priority — a configuration bug. Fails loudly; the runtime
    degrades the user to a safe fallback while this is logged at error level (privacy-safe)."""


@dataclass
class HandlerVerdict:
    disposition: Disposition
    priority: int = 0                  # stable, explicit; higher wins among owners
    reason: str = ""                  # privacy-safe category, never user content
    telemetry: dict = field(default_factory=dict)   # privacy-safe; the runtime logs it


@dataclass
class HandlerOutput:
    """What the SELECTED handler produces on execute(): the RAW factual reply text + presentation intent
    + side effects to record. The runtime (not the handler) renders/styles/safety-gates this."""
    text: str
    styled: bool = True                # False = fact-locked copy -> runtime skips voice styling
    event_candidate_id: UUID | None = None
    mission_id: UUID | None = None


@dataclass
class ResolvedReply:
    """Final result the runtime enqueues: the finished reply + which handler produced it + side effects."""
    reply: str
    handler: str
    event_candidate_id: UUID | None = None
    mission_id: UUID | None = None


@dataclass
class OutcomeContext:
    """Inputs a handler may read + the collaborators execute() needs. APPEND-ONLY. Handlers must not
    mutate durable state during evaluate()."""
    user_id: UUID
    decision: "ConversationDecision"
    capsule: "ContextCapsule"
    msg: "InboundMessage"
    profile: object
    channel: str
    pmid: str
    style: "ConversationStyleEngine"
    store: object                      # conversation_store module


@runtime_checkable
class OutcomeHandler(Protocol):
    name: str
    priority: int
    async def evaluate(self, octx: OutcomeContext) -> HandlerVerdict: ...   # PURE — no mutation, no enqueue
    async def execute(self, octx: OutcomeContext) -> HandlerOutput: ...     # only if selected; may mutate


# --------------------------------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------------------------------
_FRIENDLY_PHASE = {
    "created": "just getting started", "understanding": "still figuring out exactly what's needed",
    "extracting": "pulling out the details", "awaiting_approval": "waiting on your ok",
    "executing": "working on it", "waiting_external": "waiting on an external service",
    "verifying": "double-checking the result", "succeeded": "done", "blocked": "stuck, i need you",
    "failed": "it didn't finish",
}


class StatusQueryHandler:
    """A1.1: a status question ('what are u doing with that?', 'did that go through?') about an active
    handoff mission -> report its PERSISTED state honestly. A1 truth: captured + tracked, NO external action
    yet, so it never claims something happened. evaluate() reads only and CLAIMS only when there's an active
    mission to report; otherwise DECLINES so a status-shaped question with nothing open gets a normal reply."""
    name = "mission_status"
    priority = 65                      # a status READ outranks a new handoff creation

    async def evaluate(self, octx: OutcomeContext) -> HandlerVerdict:
        if not handoff.has_status_query_language(octx.msg.text):
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="not_a_status_query")
        state = await mission_kernel.latest_active_handoff_mission(octx.user_id)   # READ only, no mutation
        if state is None:
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="no_active_mission")
        return HandlerVerdict(disposition=Disposition.claim, priority=self.priority,
                              reason="active_mission", telemetry={"mission_phase": state["phase"]})

    async def execute(self, octx: OutcomeContext) -> HandlerOutput:
        from . import mission_presentation
        state = await mission_kernel.latest_active_handoff_mission(octx.user_id)
        if state is None:              # raced to closed between evaluate and execute -> honest fallback
            return HandlerOutput(text=octx.style.template("mission_status_none"), styled=False)
        # useful status from PERSISTED state + grounded facts: what's checked vs pending, and the honest
        # 'nothing external yet' — never a raw db phase label.
        return HandlerOutput(text=mission_presentation.render_status(state), styled=False)


class MissionHandoffHandler:
    """A1: on an AUTHORIZED handoff, CLAIM the outcome and create ONE durable Mission (capture/track only —
    no external action), then acknowledge. Otherwise DECLINE (telemetry only), leaving the normal reply
    flow unchanged. evaluate() is PURE — it runs the deterministic policy but mutates nothing, so a
    hallucinated model needs_mission can never make it claim: only the user's explicit handoff to a
    supported capability sets authorizes_mutation, and only that CLAIMS.

    A1's supported capability is durable CAPTURE itself (tracking the handoff with its source + attachment
    + goal linked), which is inherently low-risk and takes no external action. EXECUTING a specific
    capability is a later, separately-gated phase."""
    name = "mission_handoff"
    priority = 60                      # above event-candidate: an authorized handoff outranks an event

    _CAPTURE_CAPABILITY = "student_task_capture"

    def _decide(self, octx: OutcomeContext) -> handoff.HandoffDecision:
        """PURE deterministic decision — called by both evaluate() and execute() so the two agree."""
        d = octx.decision
        inp = handoff.HandoffInputs(
            user_text=octx.msg.text,
            model_needs_mission=getattr(d, "needs_mission", False),
            model_proposed_goal=getattr(d, "proposed_goal", None),
            model_suggested_capability=(d.required_capabilities[0] if d.required_capabilities else None),
            capability_supported=True,           # A1: durable capture/tracking is always supported
            risk="low", reversible=True,         # capture takes no external action -> inherently low-risk
            confidence=d.confidence)
        return handoff.decide_handoff(inp)

    async def evaluate(self, octx: OutcomeContext) -> HandlerVerdict:
        decision = self._decide(octx)
        tel = {**decision.telemetry(),
               "model_needs_mission": getattr(octx.decision, "needs_mission", False)}
        if decision.authorizes_mutation:         # ONLY an authorized explicit handoff claims + mutates
            return HandlerVerdict(disposition=Disposition.claim, priority=self.priority,
                                  reason=decision.reason, telemetry=tel)
        # answer_only / remember_context / propose_mission / request_decision / unsupported -> no state
        return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                              reason=decision.reason, telemetry=tel)

    async def execute(self, octx: OutcomeContext) -> HandlerOutput:
        from . import mission_presentation
        decision = self._decide(octx)
        assert decision.authorizes_mutation, "execute() reached without authorization"   # defense in depth
        d = octx.decision
        goal_text = (d.proposed_goal or (octx.msg.text or "").strip() or "this")[:120]
        facts = mission_presentation.extract_flyer_facts(d)         # grounded flyer facts, never invented
        attachment_refs = [{"media_type": getattr(a, "media_type", None),
                            "filename": getattr(a, "filename", None)} for a in octx.msg.attachments]
        evidence = {"reply_to_message_id": getattr(octx.msg, "reply_to_message_id", None),
                    "has_referenced_context": bool(
                        getattr(octx.capsule, "referenced_text", None)
                        or getattr(octx.capsule, "prior_answer", None)
                        or getattr(octx.capsule, "referenced_images", None))}
        result = await mission_kernel.create_handoff_mission(
            octx.user_id, capability=self._CAPTURE_CAPABILITY, source_message_id=octx.pmid,
            proposed_goal=goal_text, short_status=f"tracking: {goal_text}",
            autonomy=str(getattr(d, "autonomy", "A0") or "A0"), risk="low",
            attachment_refs=attachment_refs, evidence=evidence, extracted_facts=facts)
        # CONTEXT-AWARE ack generated from the created mission + grounded facts — never a canned line, and
        # never claims registration/booking/completion (A1 takes no external action).
        pres = mission_presentation.MissionStartPresentation(
            mission_id=str(result.mission_id), user_goal=goal_text, capability=self._CAPTURE_CAPABILITY,
            source_summary=("flyer/attachment" if attachment_refs else "message"), extracted_facts=facts,
            current_phase=result.phase, evidence_count=len(attachment_refs), external_action_attempted=False)
        reply = mission_presentation.render_start(pres)
        return HandlerOutput(text=reply, styled=False, mission_id=result.mission_id)


class EventCandidateHandler:
    """An extracted event -> CLAIM. execute() persists a candidate (durable, provenance-kept) and returns
    the honest fact-locked 'can't add to calendar yet' template (never 'added'), or the styled model reply.
    evaluate() is pure (only reads the decision)."""
    name = "event_candidate"
    priority = 50

    async def evaluate(self, octx: OutcomeContext) -> HandlerVerdict:
        if _is_event(octx.decision):
            return HandlerVerdict(disposition=Disposition.claim, priority=self.priority, reason="event_entities")
        return HandlerVerdict(disposition=Disposition.decline, priority=self.priority, reason="not_an_event")

    async def execute(self, octx: OutcomeContext) -> HandlerOutput:
        d = octx.decision
        title, when, where, provenance = _event_fields(d)
        ec_id = await octx.store.persist_event_candidate(
            octx.user_id, title=title,
            idempotency_key=f"ec:{octx.channel}:{octx.pmid}",
            confidence=d.confidence,
            missing_fields=[f for f in ("date",) if not when] or None,
            provenance={**provenance, "inbound_provider_message_id": octx.pmid})
        if _wants_calendar(d, octx.msg.text):
            # fact-locked copy -> runtime must NOT voice-style it (styled=False)
            reply = octx.style.template("event_saved_calendar_unavailable", title=title, when=when, where=where)
            return HandlerOutput(text=reply, styled=False, event_candidate_id=ec_id)
        return HandlerOutput(text=d.user_visible_response, styled=True, event_candidate_id=ec_id)


class DefaultReplyHandler:
    """The explicit FALLBACK (never a claim): the model's honest reply, styled. Selected only when no
    handler claims or blocks, so the pipeline can never fall through."""
    name = "default_reply"
    priority = 0

    async def evaluate(self, octx: OutcomeContext) -> HandlerVerdict:
        return HandlerVerdict(disposition=Disposition.decline, priority=self.priority, reason="fallback")

    async def execute(self, octx: OutcomeContext) -> HandlerOutput:
        return HandlerOutput(text=octx.decision.user_visible_response, styled=True)


def default_handlers() -> list[OutcomeHandler]:
    """Claim-candidate handlers, evaluated every turn (pure). Ordering is by explicit priority, not list
    position. A workstream inserts its handler here with a stable priority."""
    return [StatusQueryHandler(), MissionHandoffHandler(), EventCandidateHandler()]


def default_fallback() -> OutcomeHandler:
    """The single explicit fallback used when zero handlers claim or block."""
    return DefaultReplyHandler()


def select_owner(verdicts: list[tuple[OutcomeHandler, HandlerVerdict]]) -> OutcomeHandler | None:
    """Deterministic selection: claims outrank blocked; the single highest-priority owner wins; a tie at
    the top priority raises OutcomeCollision (fail loudly). Returns None when there is no owner (-> the
    caller uses the explicit fallback)."""
    claims = [(h, v) for h, v in verdicts if v.disposition == Disposition.claim]
    blocked = [(h, v) for h, v in verdicts if v.disposition == Disposition.blocked]
    owners = claims or blocked
    if not owners:
        return None
    top = max(v.priority for _, v in owners)
    top_owners = [h for h, v in owners if v.priority == top]
    if len(top_owners) > 1:
        names = sorted(h.name for h in top_owners)
        log.error("outcome_multi_claim n=%d priority=%d handlers=%s", len(top_owners), top, names)
        raise OutcomeCollision(f"{len(top_owners)} handlers claimed at priority {top}: {names}")
    return top_owners[0]
