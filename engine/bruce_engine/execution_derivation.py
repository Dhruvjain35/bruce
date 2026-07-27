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

from .semantic_contracts import (Actionability, DecisionPolarity, Derivation, Family, GoalCount,
                                 OperationFamily, SemanticTurn, TurnContext, TurnRole)

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

# Which operations each family NATIVELY performs — derived from the table above so the two can never
# disagree. Used to break a tie the model itself flagged: "write to my teacher about the deadline" comes
# back as {communication, calendar} because a deadline is date-shaped, but only one of those families can
# `send`. Capability truth resolves the ambiguity for free, with no phrase rule and no second model call.
_NATIVE_OPS: dict[Family, frozenset[OperationFamily]] = {}
for _fam, _op in _CAPABILITY:
    _NATIVE_OPS[_fam] = _NATIVE_OPS.get(_fam, frozenset()) | {_op}

# Within-family operation synonyms. "draft an email" is `create` in ordinary language, and Bruce has no
# standalone draft capability — composing, showing, and sending on approval is ONE route. So `create` in
# the communication family IS the send route, which already requires a human approval before the write.
# Deliberately narrow: a synonym only exists where the runtime genuinely has one road.
_SYNONYM: dict[tuple[Family, OperationFamily], OperationFamily] = {
    (Family.communication, OperationFamily.create): OperationFamily.send,
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


def resolve_family(turn: SemanticTurn) -> tuple[Family | None, str]:
    """Pick the one family this turn is about, using the operation to break a tie.

    A contested read is not automatically a question. When the model offers two families but only one of
    them can perform the operation it also named, the runtime knows the answer and asking the student
    would be theatre. Only a genuine tie — two families that both support the operation — is escalated.
    """
    seen: list[Family] = []
    for f in turn.domain_candidates:
        if f is not Family.unknown and f not in seen:
            seen.append(f)
    if not seen:
        return None, "no_domain"
    if len(seen) == 1:
        return seen[0], ""
    supporting = [f for f in seen if turn.operation_family in _NATIVE_OPS.get(f, frozenset())]
    if len(supporting) == 1:
        return supporting[0], ""
    return None, "multiple_domains"


def _clarify(reason: str, turn: SemanticTurn, rule: str) -> Derivation:
    """An honest question beats both a wrong action and a wrong silence.

    Asking is `fast_conversation` because execution class describes MACHINERY, and a question needs no
    tool, no durable run, and no lease. Calling it `foreground_agent` would spin up a bounded task graph
    to say "which one?". What makes this different from idle chat is carried by `needs_clarification` and
    `clarification_reason`, which is where a consumer should read it rather than inferring it from a class
    that is pretending a question is a task.
    """
    return Derivation(execution_class=_CHAT, action="answer", needs_clarification=True,
                      clarification_reason=reason, confidence=turn.confidence, rule=rule,
                      ambiguity=turn.uncertainty or (reason,))


def _chat(turn: SemanticTurn, rule: str) -> Derivation:
    return Derivation(execution_class=_CHAT, action="answer", confidence=turn.confidence, rule=rule)


def derive(turn: SemanticTurn, ctx: TurnContext) -> Derivation:
    """SemanticTurn + runtime facts -> execution class. Pure, total, and ordered by precedence."""

    # -1. THE DETERMINISTIC VETO. Ahead of the model's reading, ahead of pending-decision continuation,
    #     ahead of domain and operation derivation. The model drove false actions from 0.281 to 0.061 and
    #     will never reach zero, because it is a probabilistic reader; this is the boundary it cannot
    #     cross. Negative, withdrawal, cancellation and ambiguous are TERMINAL for the turn — nothing
    #     downstream may upgrade them, and only a later trusted turn can create authorization.
    boundary = ctx.action_boundary
    if boundary is not None and getattr(boundary, "blocks_execution", None) and boundary.blocks_execution():
        from . import user_action_boundary as uab
        verdict = uab.reconcile(boundary, turn.decision_polarity.value)
        if boundary.target is uab.Target.provider_entity:
            # "cancel it and delete the event" names something that already exists on the provider.
            # Stopping Bruce's work is not the same operation as deleting a provider entity, and that
            # one needs its own resolution and its own authorization.
            return _clarify("provider_entity_needs_its_own_authorization", turn, "veto_provider_entity")
        return Derivation(execution_class=_CHAT, action="answer", decision_id=ctx.pending_decision_id,
                          continuation_run_id=ctx.active_run_id, confidence=turn.confidence,
                          needs_clarification=(verdict == uab.CONTRADICTION_CLARIFY),
                          clarification_reason=("boundary_" + boundary.polarity.value),
                          rule="deterministic_veto_" + boundary.polarity.value,
                          ambiguity=(verdict,) if verdict != uab.AGREE else ())

    # 0. A REFUSAL STOPS EVERYTHING. Highest precedence, ahead of every other reading, and deliberately
    #    not conditioned on there being a pending Decision — an exhaustive sweep found that a decline with
    #    nothing pending fell straight through to the executable path and derived a write. "Don't" is the
    #    one input where being wrong is destructive, so it is answered before anything else is considered.
    if turn.decision_polarity is DecisionPolarity.decline:
        return Derivation(execution_class=_CHAT, action="answer", decision_id=ctx.pending_decision_id,
                          confidence=turn.confidence, rule="decision_declined")

    # 1. Answering a question Bruce asked outranks everything the sentence looks like — but ONLY an
    #    explicit approval authorizes the work. Measured on the open set, every refusal under a pending
    #    Decision executed the proposal, because this branch fired on the ROLE and never looked at which
    #    way the answer pointed. That is the same shape as the refusal P0, rebuilt one layer up.
    if turn.turn_role is TurnRole.decision_response and ctx.pending_decision_id:
        if turn.decision_polarity is DecisionPolarity.approve:
            return Derivation(execution_class=_DIRECT, action="create",
                              domain=ctx.active_run_domain or "calendar",
                              decision_id=ctx.pending_decision_id, confidence=turn.confidence,
                              rule="decision_approved")
        return _clarify("decision_unclear", turn, "decision_polarity_unclear")

    # 2. Calling something off stops BRUCE'S OWN WORK. It never reaches a provider. Measured, this branch
    #    used to derive action="delete", so "please dont schedule anything" produced a delete against the
    #    calendar — an unrequested destructive write out of a refusal.
    if turn.turn_role is TurnRole.cancellation:
        if ctx.pending_decision_id:
            return Derivation(execution_class=_CHAT, action="answer", decision_id=ctx.pending_decision_id,
                              confidence=turn.confidence, rule="decision_declined")
        if ctx.active_run_id:
            return Derivation(execution_class=_DIRECT, action=None, domain=None, capabilities=(),
                              continuation_run_id=ctx.active_run_id, confidence=turn.confidence,
                              rule="stop_inflight_work")
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

    # 5b. Several goals in one message. The semantic layer resolves ONE operation, so executing on it
    #     would silently do half of what was asked — measured on "add practice friday and email coach",
    #     which ran the calendar half and dropped the email. Half is not a partial success; it is a wrong
    #     answer the student has no way to see. Sequencing several goals is a planner's job.
    if turn.goal_count is GoalCount.several:
        return _clarify("several_goals", turn, "multiple_goals")

    # 6. Executable / durable. Resolve the family before anything else — a goal aimed at a family that is
    #    not live must be answered honestly, not routed at a neighbouring tool.
    family, why = resolve_family(turn)
    if family is None:
        return _clarify(why, turn, "family_unresolved")

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

    op = _SYNONYM.get((family, turn.operation_family), turn.operation_family)
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
        # Already watching this domain? Then "any update", "still waiting?", "did they reply" is asking
        # ABOUT the mission, not requesting a second one. Measured: every status question during an
        # active Gmail run spawned a duplicate monitor of a thread already being monitored.
        #
        # Gating this on `goal_count` was tried and measured WORSE: it let status questions through as
        # new missions and took the false-action rate from 0.012 to 0.117. No semantic field separates
        # "watch for a reply from coach" from "did they reply" — the model marks both as a continuation
        # wanting monitoring — so the guard stays strict and errs toward not starting a second mission.
        # The cost is real and accepted: an explicit second watch request during an active run in the
        # same domain is answered conversationally instead of spawning a run. A missed request is a
        # recall miss; a spurious durable mission is an unrequested action.
        if ctx.active_run_id and ctx.active_run_domain == domain:
            return Derivation(execution_class=_CHAT, action="answer", domain=domain,
                              continuation_run_id=ctx.active_run_id, confidence=turn.confidence,
                              rule="monitoring_already_running")
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


def derives_a_write(d: Derivation) -> bool:
    """Does this derivation propose touching the world? Used by the refusal invariant below and by the
    evaluation, so both ask the question the same way."""
    return d.execution_class in (_DIRECT, _BACKGROUND) and bool(d.capabilities)


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
