"""Stage 3 — the deterministic orchestrator (M2). Turns a SemanticTurn into an execution class.

THE POINT OF THE SPLIT. Execution class is not a language fact. It is a function of: what operation was
resolved, which tools are actually live for THIS student, whether anything must be waited on, whether a
human approval is required, and whether the work outlives the turn. A language model has no access to
four of those five. Asking it to answer anyway is how a correct reading of "email them and let me know if
they respond" turned into direct_action — the model understood the monitoring perfectly and simply had no
way to know that monitoring means a durable run in this runtime.

So: no model call in this module, and no phrase matching either. Every rule below reads structured
semantic fields and structured runtime facts. If a rule ever needs to look at raw message text, the split
has been violated.

FAIL SHUT, NOT QUIET. When the semantics do not resolve, the answer is a QUESTION (foreground_agent), not
a fabricated chat reply. The old path's silent conversion of every uncertainty into fast_conversation is
exactly what made a missed executable goal indistinguishable from correct chat.
"""

from __future__ import annotations

from .semantic_contracts import (Actionability, Derivation, Family, OperationFamily, SemanticTurn,
                                 TurnContext, TurnRole)

# Below this, a semantic read is not trusted to route work. It becomes a clarifying question rather than
# a guess in either direction — a wrong action and a wrongly-silent chat reply are both failures.
MIN_CONFIDENCE = 0.55

# family + operation -> the capability that would perform it. The ONLY place a provider name appears in
# the semantic path, and it is deterministic code, not the model. Absence here means "no such operation in
# this family", which is honestly reported rather than approximated with a neighbouring capability.
_CAPABILITY: dict[tuple[Family, OperationFamily], str] = {
    (Family.calendar, OperationFamily.create): "calendar.create_event",
    (Family.calendar, OperationFamily.update): "calendar.update_event",
    (Family.calendar, OperationFamily.cancel): "calendar.delete_event",
    (Family.calendar, OperationFamily.find): "calendar.search_events",
    (Family.communication, OperationFamily.send): "gmail.send_message",
    (Family.communication, OperationFamily.monitor): "gmail.find_reply",
    (Family.communication, OperationFamily.find): "gmail.search_messages",
}

# The provider-facing domain string the rest of the pipeline already speaks.
_DOMAIN = {Family.calendar: "calendar", Family.communication: "gmail", Family.memory: "world"}

# Operation family -> GoalAction value. Provider-neutral on both sides.
_ACTION = {
    OperationFamily.create: "create", OperationFamily.update: "update",
    OperationFamily.cancel: "delete", OperationFamily.send: "send",
    OperationFamily.find: "search", OperationFamily.monitor: "monitor",
    OperationFamily.remember: "remember", OperationFamily.answer: "answer",
}

_CHAT = "fast_conversation"
_DIRECT = "direct_action"
_FOREGROUND = "foreground_agent"
_BACKGROUND = "background_mission"


def _clarify(reason: str, turn: SemanticTurn, rule: str) -> Derivation:
    """An honest question beats both a wrong action and a wrong silence."""
    return Derivation(execution_class=_FOREGROUND, action="answer", needs_clarification=True,
                      clarification_reason=reason, confidence=turn.confidence, rule=rule,
                      ambiguity=turn.uncertainty or (reason,))


def _chat(turn: SemanticTurn, rule: str) -> Derivation:
    return Derivation(execution_class=_CHAT, action="answer", confidence=turn.confidence, rule=rule)


def derive(turn: SemanticTurn, ctx: TurnContext) -> Derivation:
    """SemanticTurn + runtime facts -> execution class. Pure, total, and ordered by precedence."""

    # 1. Answering a question Bruce asked outranks everything the sentence looks like. "yeah do it" is a
    #    decision response, not a new goal, and it belongs to the run that asked.
    if turn.turn_role is TurnRole.decision_response and ctx.pending_decision_id:
        return Derivation(execution_class=_DIRECT, action="create", domain=ctx.active_run_domain or "calendar",
                          decision_id=ctx.pending_decision_id, confidence=turn.confidence,
                          rule="decision_response")

    # 2. Calling something off. Only meaningful against real in-flight work; otherwise it is just talk.
    if turn.turn_role is TurnRole.cancellation:
        if ctx.pending_decision_id or ctx.active_run_id:
            return Derivation(execution_class=_DIRECT, action="delete", domain=ctx.active_run_domain,
                              decision_id=ctx.pending_decision_id, continuation_run_id=ctx.active_run_id,
                              confidence=turn.confidence, rule="cancellation")
        return _chat(turn, "cancellation_nothing_pending")

    # 3. Deixis with no content of its own resolves against in-flight work, or it is a question.
    if turn.turn_role is TurnRole.reference_only:
        if ctx.active_run_id or ctx.pending_decision_id:
            return Derivation(execution_class=_CHAT, action="answer", domain=ctx.active_run_domain,
                              continuation_run_id=ctx.active_run_id, decision_id=ctx.pending_decision_id,
                              confidence=turn.confidence, rule="reference_against_open_work")
        return _clarify("referent_unknown", turn, "reference_no_referent")

    # 4. Nothing to do. This is the ONLY road to fast_conversation for a confident read, and it requires
    #    the model to have positively said so — never a fallback, never a shrug.
    if turn.actionability in (Actionability.no_action, Actionability.information_only):
        return _chat(turn, "not_actionable")

    # 5. An uncertain read asks. Below the confidence floor the semantic layer has not earned a routing
    #    decision, and defaulting to chat would silently drop real work (the measured failure).
    if turn.confidence < MIN_CONFIDENCE:
        return _clarify("low_confidence", turn, "below_confidence_floor")

    if turn.actionability is Actionability.ambiguous:
        return _clarify("underspecified", turn, "ambiguous_actionability")

    # 6. Executable / durable. Resolve the family before anything else — a goal aimed at a family that is
    #    not live must be answered honestly, not routed at a neighbouring tool.
    family = turn.single_family()
    if family is None:
        real = tuple(f for f in turn.domain_candidates if f is not Family.unknown)
        return _clarify("multiple_domains" if len(real) > 1 else "no_domain", turn,
                        "family_unresolved")

    if family is Family.knowledge:
        return _chat(turn, "knowledge_only")

    # A durable fact about the student needs no provider — it is a write to Bruce's own world state.
    if family is Family.memory:
        return Derivation(execution_class=_DIRECT, action="remember", domain="world",
                          confidence=turn.confidence, rule="memory_write")

    if family not in ctx.live_families:
        # Honest, and NOT a clarifying question: there is nothing the student could say to make an
        # unconnected tool work. The reply must say so, which is what CapabilitySnapshot is for.
        return Derivation(execution_class=_CHAT, action="answer", domain=_DOMAIN.get(family),
                          confidence=turn.confidence, rule="family_not_live",
                          ambiguity=("family_not_live",))

    op = turn.operation_family
    if op in (OperationFamily.none, OperationFamily.answer):
        return _clarify("operation_unknown", turn, "operation_unresolved")

    capability = _CAPABILITY.get((family, op))
    if capability is None:
        return _clarify("operation_not_in_family", turn, "operation_family_mismatch")
    if capability not in ctx.live_capabilities:
        return Derivation(execution_class=_CHAT, action="answer", domain=_DOMAIN.get(family),
                          capabilities=(capability,), confidence=turn.confidence,
                          rule="operation_not_live", ambiguity=("operation_not_live",))

    domain = _DOMAIN.get(family)
    action = _ACTION.get(op)

    # 7. Durable monitoring is a RUNTIME property, not a language one: it outlives the turn, so it needs a
    #    durable run, a lease, and exactly-once notification. This is precisely the judgement the model was
    #    being asked for and could not make.
    if turn.actionability is Actionability.durable_monitoring or op is OperationFamily.monitor:
        caps = (capability,)
        if op is not OperationFamily.monitor and family is Family.communication:
            # "send it and watch for a reply" is one send plus a monitor; the send is verified inside the
            # turn and the mission that follows is pure monitoring.
            caps = (capability, "gmail.find_reply")
        return Derivation(execution_class=_BACKGROUND, action=action, domain=domain, capabilities=caps,
                          confidence=turn.confidence, rule="durable_monitoring")

    # 8. A correction targets existing work; it still executes directly, but it carries the link so the
    #    executor repairs rather than duplicates.
    if turn.turn_role is TurnRole.correction and ctx.active_run_id:
        return Derivation(execution_class=_DIRECT, action=action, domain=domain, capabilities=(capability,),
                          correction_of_run_id=ctx.active_run_id, confidence=turn.confidence,
                          rule="correction_of_active_run")

    return Derivation(execution_class=_DIRECT, action=action, domain=domain, capabilities=(capability,),
                      continuation_run_id=ctx.active_run_id if turn.turn_role is TurnRole.continuation else None,
                      confidence=turn.confidence, rule="executable_single_operation")


# --- runtime facts -------------------------------------------------------------------------------------

async def build_context(user_id, *, has_reply_ref: bool = False, has_attachments: bool = False) -> TurnContext:
    """Gather the deterministic side of the decision. Every read is best-effort: a store hiccup narrows
    what Bruce will attempt, which is the safe direction, and never blocks the turn."""
    from . import agent_run_store, capability_snapshot, mission_kernel

    live_caps: set[str] = set()
    live_fams: set[Family] = set()
    try:
        snap = await capability_snapshot.snapshot(user_id)
        for state in snap.families:
            if not state.usable:
                continue
            live_caps.update(state.capabilities)
            # CapabilitySnapshot speaks "email"; the semantic layer speaks "communication". One mapping,
            # in one place, rather than two vocabularies drifting apart.
            live_fams.add(Family.communication if state.family == "email" else Family.calendar)
    except Exception:
        live_caps, live_fams = set(), set()

    pending = None
    try:
        p = await mission_kernel.latest_pending_calendar_mission(user_id)
        pending = str(p["mission_id"]) if p else None
    except Exception:
        pending = None

    run_id = run_domain = None
    try:
        # domain=None: defaulting this to "calendar" is exactly how Gmail missions became invisible to the
        # runtime once already.
        run = await agent_run_store.latest_active(user_id, domain=None)
        if run:
            run_id, run_domain = str(run.get("id")), (run.get("domain") or None)
    except Exception:
        run_id = run_domain = None

    return TurnContext(live_families=frozenset(live_fams), live_capabilities=frozenset(live_caps),
                       pending_decision_id=pending, active_run_id=run_id, active_run_domain=run_domain,
                       has_reply_ref=has_reply_ref, has_attachments=has_attachments)
