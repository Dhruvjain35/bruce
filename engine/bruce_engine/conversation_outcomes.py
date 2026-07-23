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


def _calendar_reply(result, event) -> str:
    """The honest, fact-locked reply for a calendar outcome. NEVER says 'done' unless the read-back
    verified. All copy is lowercase Bruce voice; the only dash is the numeric date range the outbound
    gate preserves as a fact."""
    from .calendar_schedule import ScheduleState, human_when
    when = human_when(event)
    title = (event.title or "that").lower()
    if result.state is ScheduleState.verified:
        base = f"done, {title} is on ur calendar for {when} ✅"
        if result.all_day:
            base += "\ni saved it as an all-day event since there's no time on the flyer."
        return base
    if result.state is ScheduleState.not_connected:
        return (f"i've got {title} ready to add, but ur google calendar isn't connected yet. "
                f"connect it and i'll put it on there for {when}.")
    if result.state is ScheduleState.verification_inconclusive:
        return (f"i added {title} to ur calendar but couldn't confirm it stuck when i checked back, "
                f"so i'm not calling it done. want me to recheck?")
    return (f"i tried to add {title} to ur calendar for {when} but it didn't go through, so nothing's "
            f"on there. want me to try again?")


class CalendarScheduleHandler:
    """Real Google Calendar execution. On an AUTHORIZED handoff of an EVENT with a resolvable date to the
    calendar capability, CLAIM and actually create + read-back-verify the event on the student's connected
    Google Calendar — then reply only after that verification. Outranks generic capture (70 > 60) because
    it completes the action rather than merely tracking it; when the calendar isn't connected it still owns
    the outcome and replies honestly (never a fake 'added'). evaluate() is PURE: deterministic handoff
    policy + grounded entities + a read-only buildability check, no mutation."""
    name = "calendar_schedule"
    priority = 70

    def _decide(self, octx: OutcomeContext) -> handoff.HandoffDecision:
        d = octx.decision
        inp = handoff.HandoffInputs(
            user_text=octx.msg.text,
            model_needs_mission=getattr(d, "needs_mission", False),
            model_proposed_goal=getattr(d, "proposed_goal", None),
            model_suggested_capability=(d.required_capabilities[0] if d.required_capabilities else None),
            capability_supported=True, risk="low", reversible=True, confidence=d.confidence)
        return handoff.decide_handoff(inp)

    async def evaluate(self, octx: OutcomeContext) -> HandlerVerdict:
        from . import calendar_schedule, oauth_google
        d = octx.decision
        # DETERMINISTIC authorization to schedule: either explicit scheduling verbs ("schedule this",
        # "put this on my calendar", "add this to my cal", "block this off") OR a generic handoff. The
        # model proposes (entities/intent); this policy authorizes the write. This is the CASE-2 fix:
        # "schedule ts" was never authorized before because it isn't generic-handoff language.
        sched_intent = handoff.has_scheduling_execution_intent(octx.msg.text)
        decision = self._decide(octx)
        authorized = sched_intent or decision.authorizes_mutation
        if not authorized:
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="not_authorized_to_schedule")
        # a real dated event must be present; scheduling intent already implies the calendar is wanted
        if not _is_event(d):
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="not_an_event")
        if not (sched_intent or _wants_calendar(d, octx.msg.text)):
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="not_a_calendar_intent")
        if calendar_schedule.build_calendar_event(d) is None:
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="no_resolvable_date")
        # only NOW read the connection (read-only, so evaluate stays pure). If the calendar isn't
        # connected, DECLINE so the generic capture path (MissionHandoffHandler) owns it exactly as it
        # did before calendar execution existed — Bruce captures + tracks the handoff, no nag, no write.
        try:
            integ = await oauth_google.get_integration(octx.user_id)
        except Exception:
            integ = None
        connected = (integ is not None and integ.status == "connected"
                     and integ.revoked_at is None and bool(integ.refresh_token_encrypted))
        if not connected:
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="calendar_not_connected")
        return HandlerVerdict(disposition=Disposition.claim, priority=self.priority,
                              reason="calendar_event_handoff", telemetry=decision.telemetry())

    async def execute(self, octx: OutcomeContext) -> HandlerOutput:
        from . import calendar_schedule, mission_presentation
        d = octx.decision
        source = "flyer/attachment" if octx.msg.attachments else "message"
        event = calendar_schedule.build_calendar_event(d, source=source)
        if event is None:                              # defense in depth (evaluate already gated it)
            return HandlerOutput(text="i couldn't pin down the date for that, when is it?", styled=False)
        goal_text = (d.proposed_goal or event.title or "add this to your calendar")[:120]
        facts = mission_presentation.extract_flyer_facts(d)
        attachment_refs = [{"media_type": getattr(a, "media_type", None),
                            "filename": getattr(a, "filename", None),
                            "sha256": getattr(a, "sha256", None)} for a in octx.msg.attachments]
        creation = await mission_kernel.create_handoff_mission(
            octx.user_id, capability=calendar_schedule._CAPABILITY, source_message_id=octx.pmid,
            proposed_goal=goal_text, short_status=f"add to calendar: {event.title}"[:120],
            autonomy=str(getattr(d, "autonomy", "A0") or "A0"), risk="low",
            attachment_refs=attachment_refs,
            evidence={"reply_to_message_id": getattr(octx.msg, "reply_to_message_id", None)},
            extracted_facts=facts)
        digest = calendar_schedule.attachment_digest(attachment_refs)
        result = await calendar_schedule.schedule_event(
            octx.user_id, creation.mission_id, event,
            source_message_id=octx.pmid, attachment_digest=digest)
        log.info("calendar_schedule state=%s mission_id=%s", result.state.value, creation.mission_id)
        return HandlerOutput(text=_calendar_reply(result, event), styled=False,
                             mission_id=creation.mission_id)


class CalendarApprovalHandler:
    """Resolve a PENDING calendar decision — the fix for the live loop where "ya"/"add it"/"YES ADD IT"
    kept re-triggering the same offer because authorization never carried forward.

    When Bruce has an open awaiting_approval calendar mission and the student's reply resolves it, THIS
    handler (highest priority — an answer to an open question outranks starting anything new) continues
    the SAME run: approved -> execute the EXACT offered event on that mission + read-back verify; rejected
    -> close it honestly; ambiguous -> ask ONE precise question. It never re-asks the same offer, and it
    claims ONLY when a pending decision actually exists (so a stray "ya" with nothing open is ignored).
    evaluate() is pure: it reads the deterministic resolver + the pending mission, mutating nothing."""
    name = "calendar_approval"
    priority = 80

    async def evaluate(self, octx: OutcomeContext) -> HandlerVerdict:
        from . import decision_resolver
        res = decision_resolver.resolve_approval(octx.msg.text)
        if res is decision_resolver.Resolution.unrelated:   # cheap gate first -> no DB read for chatter
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="not_a_decision_reply")
        try:
            pending = await mission_kernel.latest_pending_calendar_mission(octx.user_id)
        except Exception:                                   # unreadable -> conservatively no pending
            pending = None
        if pending is None:
            return HandlerVerdict(disposition=Disposition.decline, priority=self.priority,
                                  reason="no_pending_decision")
        return HandlerVerdict(disposition=Disposition.claim, priority=self.priority,
                              reason=f"resolve_{res.value}")

    async def execute(self, octx: OutcomeContext) -> HandlerOutput:
        from . import calendar_schedule, decision_resolver
        from .models import CalendarEvent
        res = decision_resolver.resolve_approval(octx.msg.text)
        pending = await mission_kernel.latest_pending_calendar_mission(octx.user_id)
        if pending is None:                        # resolved by a concurrent turn between evaluate + execute
            return HandlerOutput(text="nothing pending on my end right now.", styled=False)
        mid = UUID(pending["mission_id"])
        goal = pending["goal"] or {}
        pe = goal.get("pending_event") or {}
        if not pe.get("start"):                    # malformed pending event -> don't claim success
            return HandlerOutput(text="i lost the details for that one, mind resending the flyer?", styled=False,
                                 mission_id=mid)
        event = CalendarEvent(title=pe.get("title") or "your event", start=pe["start"], end=pe.get("end"),
                              location=pe.get("location"), source=pe.get("source"))
        if res is decision_resolver.Resolution.rejected:
            await mission_kernel.record_phase(octx.user_id, mid, "blocked", "approval_rejected",
                                              status="cancelled")
            return HandlerOutput(text="ok, i'll leave it off ur calendar.", styled=False, mission_id=mid)
        if res is decision_resolver.Resolution.ambiguous:
            return HandlerOutput(
                text=f"just to confirm, do u want {(event.title or 'it').lower()} on ur google calendar? yes or no",
                styled=False, mission_id=mid)
        # approved -> execute the EXACT offered event on the SAME mission (stable id -> no duplicate)
        src = (goal.get("source_message_ids") or [octx.pmid])[0]
        digest = goal.get("attachment_digest") or ""
        result = await calendar_schedule.schedule_event(
            octx.user_id, mid, event, source_message_id=src, attachment_digest=digest)
        log.info("calendar_approval_executed state=%s mission_id=%s", result.state.value, mid)
        return HandlerOutput(text=_calendar_reply(result, event), styled=False, mission_id=mid)


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
            # fact-locked copy -> runtime must NOT voice-style it (styled=False). If the calendar IS
            # connected AND the event is resolvable, OFFER + create a durable PENDING decision so the
            # student's "ya" later actually executes it (carries authorization forward). If not connected,
            # be honest that it's unavailable. Bruce still doesn't WRITE here — this is a flyer with no
            # scheduling verb, so it waits for the explicit go-ahead.
            from . import calendar_schedule, mission_presentation, oauth_google
            try:
                integ = await oauth_google.get_integration(octx.user_id)
            except Exception:                          # unknowable -> conservatively 'not connected'
                integ = None
            connected = integ is not None and integ.status == "connected" and integ.revoked_at is None
            if connected:
                event = calendar_schedule.build_calendar_event(
                    d, source="flyer/attachment" if octx.msg.attachments else "message")
                if event is not None:
                    attachment_refs = [{"media_type": getattr(a, "media_type", None),
                                        "filename": getattr(a, "filename", None),
                                        "sha256": getattr(a, "sha256", None)} for a in octx.msg.attachments]
                    creation = await mission_kernel.create_pending_calendar_approval(
                        octx.user_id, source_message_id=octx.pmid, event=event,
                        attachment_digest=calendar_schedule.attachment_digest(attachment_refs),
                        facts=mission_presentation.extract_flyer_facts(d), attachment_refs=attachment_refs)
                    reply = octx.style.template("event_saved_offer_calendar", title=title, when=when, where=where)
                    return HandlerOutput(text=reply, styled=False, event_candidate_id=ec_id,
                                         mission_id=creation.mission_id)
                # connected but no resolvable date -> offer still, but nothing to execute yet
                reply = octx.style.template("event_saved_offer_calendar", title=title, when=when, where=where)
                return HandlerOutput(text=reply, styled=False, event_candidate_id=ec_id)
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
    return [CalendarApprovalHandler(), CalendarScheduleHandler(), StatusQueryHandler(),
            MissionHandoffHandler(), EventCandidateHandler()]


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
