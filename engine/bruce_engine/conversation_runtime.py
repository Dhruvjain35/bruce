"""The conversation brain orchestrator for ONE linked inbound (Bite 1), flag-gated.

Flow: gate (1:1 only) -> idempotency -> read attachments -> reason (one vision pass) -> branch
(tutoring / event-candidate / unsupported / casual / unreadable) -> style -> persist turn + optional
event candidate -> enqueue EXACTLY ONE outbound. Never creates a durable mission from the model and
never promises an unsolicited follow-up (deferred). Never claims an action happened that didn't.
"""

from __future__ import annotations

import logging
from uuid import UUID

from . import (capability_snapshot, capability_truth, context_compiler, conversation_context, conversation_outcomes,
               conversation_store, messaging_outbound, response_composer, router_authority, technical_render)
from .attachment_pipeline import UnreadableAttachment, normalize_image
from .conversation_contract import ConversationDecision, RiskLevel
from .conversation_model import ConversationReasoner, VisionInput, production_reasoner
from .conversation_style import ConversationStyleEngine, VoiceProfile, enforce_no_dashes, strip_redundant_offer
from .messaging import ChannelKind, InboundMessage, MessagingChannel
from .messaging_inbound import InboundOutcome

log = logging.getLogger("bruce.conversation")   # content-free: ids/intents/statuses only, never text

_FALLBACK = "ngl something glitched on my end 😅 mind sending that again?"


async def enabled_for(user_id: UUID, capability: str = "conversation") -> bool:
    """Whether this LINKED user may use the conversation runtime — decided by the DB access gate, not an
    env allow-list. Fail-closed: any error inside the gate resolves to DENY. Per-user access comes from a
    ProductionAccountEntitlement (persistent) or a live StagingTestEnrollment (temporary); a global kill
    or an explicit BRUCE_CONVERSATION_RUNTIME hard-off overrides to DENY. See access_control."""
    from . import access_control
    return (await access_control.conversation_access(user_id, capability)).allow


def _prepare_images(msg: InboundMessage) -> tuple[list[VisionInput], int]:
    """Normalize each image into a web-safe raster the vision model accepts (HEIC/HEIF -> JPEG, EXIF
    orientation applied, oversized bounded) so a healthy photo is never rejected for its container.
    PDFs pass through untouched. Returns (vision_inputs, n_unreadable) where n_unreadable counts
    attachments whose bytes genuinely could not be decoded (corrupt/truncated) — distinct from a model
    outage, so Bruce only says "i can't open that" when it truly can't."""
    out: list[VisionInput] = []
    unreadable = 0
    for a in msg.attachments:
        data = getattr(a, "data", None)
        if not data:
            continue
        mt = (getattr(a, "media_type", None) or "").lower()
        if mt == "application/pdf":
            out.append(VisionInput(data=data, media_type="application/pdf"))
            continue
        try:
            norm = normalize_image(data, mt)
            out.append(VisionInput(data=norm.data, media_type=norm.media_type))
        except UnreadableAttachment:
            unreadable += 1
            log.info("attachment_unreadable")     # content-free: no bytes, name, or path
    return out, unreadable


def _context(recent: list) -> str:
    if not recent:
        return "No prior conversation."
    lines = [f"{t.role}: {t.text}" for t in recent if t.text]
    return "Recent conversation (oldest first):\n" + "\n".join(lines[-8:])


async def _open_goal_candidates(user_id: UUID, conversation_id: str) -> list[dict]:
    """EVERY open AgentRun that declares a TYPED goal kind, newest first — the runs a turn could land on.

    Runs with no declared kind are skipped rather than returned: a background audit run, a mission row and
    a real goal all share this table, and only the last of them has slots a turn can answer into. Handing
    `continuation` a run whose `_goal_kind` is None makes every amendment resolve to "I own no slot for
    that" — a change the student asked for, silently understood and silently dropped.

    This used to return the FIRST such run and call it the answer. It returns all of them now because
    "newest" is a reading of the clock, and with an email goal and a calendar goal both open the clock
    knows nothing about which one the student just typed at. `goal_selection.select` does the choosing.
    """
    from . import goal_runtime, goal_slots
    out: list[dict] = []
    for run in await goal_runtime.open_runs(user_id, conversation_id=conversation_id):
        goal = run.get("goal")
        kind, _slots = goal_slots.from_goal_jsonb(goal if isinstance(goal, dict) else None)
        if kind is not None:
            out.append(run)
    return out


async def _has_live_decision(user_id: UUID, conversation_id: str) -> bool:
    """Is there durable state that will MAKE a promise true — right now, in the database?

    The fact `response_composer.no_false_promise` needs, read from the row rather than taken from a
    handler's word for it. A handler asserting "I parked a Decision" is the same class of claim the whole
    spine exists to stop trusting.

    TWO lanes park a real decision and both count. The goal spine puts a pending Decision on the run; the
    flyer lane saves an EventCandidate as `proposed` and offers it ("want me to add it to ur google
    calendar? just say the word and i'll put it on there"). That second sentence is a conditional promise
    and it is TRUE — the candidate is sitting there waiting for the word. Missing that lane is not a
    theoretical gap: it downgraded the flyer offer to a limitation and broke the offer→"ya"→execute flow,
    which `test_offer_then_ya_executes_same_run_no_loop` catches.

    Reachability matters as much as existence: a Decision for a capability the registry does not declare
    live is not something Bruce will actually do, so a promise resting on it would still be false.
    """
    from sqlalchemy import select as _select

    from . import goal_handler, schema, tool_registry
    from .db import user_session as _user_session
    try:
        for run in await _open_goal_candidates(user_id, conversation_id):
            block = run.get("active_decision")
            if not isinstance(block, dict):
                continue
            if str(block.get("status") or "") != goal_handler.PENDING:
                continue
            capability = str(block.get("capability") or "")
            if capability and tool_registry.is_live(tool_registry.canonical(capability)):
                return True
        async with _user_session(user_id) as s:
            proposed = (await s.execute(
                _select(schema.EventCandidate.id).where(
                    schema.EventCandidate.user_id == user_id,
                    schema.EventCandidate.status == "proposed").limit(1))).first()
        return proposed is not None
    except Exception:
        # Unreadable state must not licence a promise. Failing to False costs one honest sentence;
        # failing to True is the lie this gate exists to prevent.
        log.info("live_decision_read_failed user=%s", user_id)
    return False


def _pending_operation(run: dict | None):
    """The EXACT operation this student is being asked to answer for, or None.

    Read off the canonical Decision parked on the run (`agent_runs.active_decision`) rather than inferred
    from the goal kind: only a decision that is still PENDING is a question the student can be answering,
    and only the capability recorded on it names the operation they were shown. A goal with no pending
    decision produces None, and None means the attachment reader is never consulted at all — which is the
    common case and the cheap one.
    """
    if not isinstance(run, dict):
        return None
    from . import directive_scope, goal_handler, tool_registry
    block = run.get("active_decision")
    if not isinstance(block, dict) or str(block.get("status") or "") != goal_handler.PENDING:
        return None
    capability = str(block.get("capability") or "")
    spec = tool_registry.get(capability) if capability else None
    if spec is None:
        return None
    return directive_scope.pending_operation(provider=spec.provider, operation=spec.operation,
                                             summary=str(block.get("question") or ""))


def _continuation_state(run: dict | None) -> tuple[dict | None, dict | None, dict | None]:
    """(pending decision, recent draft, recent tool result) — all three read off the ONE run.

    `continuation.resolve` is pure and takes each of these separately so the caller cannot skip one by
    accident. They come from the same row on purpose: a decision belonging to one goal and a draft
    belonging to another is how "send it" sends the wrong thing.

    The DRAFT is the goal itself once any slot is filled. That is not a stand-in — the collected slots ARE
    what Bruce would send, so "this" pointing at them resolves to the run rather than to nothing, which is
    precisely what the transcript's inline reply of "this" failed to do.
    """
    if not isinstance(run, dict):
        return None, None, None
    from . import goal_slots
    block = run.get("active_decision")
    last = run.get("last_tool_result")
    goal = run.get("goal")
    _kind, slots = goal_slots.from_goal_jsonb(goal if isinstance(goal, dict) else None)
    return (block if isinstance(block, dict) else None,
            {"id": str(run.get("id"))} if slots else None,
            last if isinstance(last, dict) else None)


class _Runtime:
    def __init__(self, reasoner: ConversationReasoner | None = None,
                 style: ConversationStyleEngine | None = None,
                 handlers: list | None = None, fallback=None) -> None:
        self.reasoner = reasoner or production_reasoner()
        self.style = style or ConversationStyleEngine()
        # the outcome-dispatch pipeline (D-INT-1/D-INT-3). Claim-candidate handlers are EVALUATED (pure)
        # every turn and selected by explicit priority; a workstream adds a handler here, never edits
        # handle(). `fallback` is the single explicit handler used when nobody claims or blocks.
        self.handlers = handlers if handlers is not None else conversation_outcomes.default_handlers()
        self.fallback = fallback if fallback is not None else conversation_outcomes.default_fallback()

    async def _resolve_outcome(self, *, decision, capsule, msg, profile, channel, user_id, pmid,
                               turn_context=None, continuation=None, open_goal=None,
                               conversation_id: str = "", turn_index: int = 0,
                               mission_lane_ran: bool = False, scope=None, selection=None):
        """Two-phase dispatch (invariant 1): PURE evaluate() over every handler, deterministic priority
        selection (claims outrank blocked; single top-priority owner; a tie fails loudly; zero owners ->
        explicit fallback), then execute() ONLY the selected handler (mutation happens here, post-
        selection). Presentation (render/style/safety) is applied AFTER, by the runtime, not the handler.

        The snapshot arguments are passed THROUGH rather than looked up: they were assembled once for this
        turn, and a handler that fetched its own copy would put back the exact defect the spine removes."""
        octx = conversation_outcomes.OutcomeContext(
            user_id=user_id, decision=decision, capsule=capsule, msg=msg, profile=profile,
            channel=channel, pmid=pmid, style=self.style, store=conversation_store,
            turn_context=turn_context, continuation=continuation, open_goal=open_goal,
            conversation_id=conversation_id, turn_index=turn_index, mission_lane_ran=mission_lane_ran,
            scope=scope, goal_selection=selection)
        verdicts = []
        for h in self.handlers:                               # PURE evaluation — no mutation, no enqueue
            v = await h.evaluate(octx)
            verdicts.append((h, v))
            if v.telemetry:
                log.info("outcome_eval handler=%s disp=%s prio=%s tel=%s",
                         h.name, v.disposition.value, v.priority, v.telemetry)
        selected = conversation_outcomes.select_owner(verdicts) or self.fallback
        output = await selected.execute(octx)                 # mutation happens HERE, post-selection
        reply = self._finalize_presentation(output, decision=decision, profile=profile, channel=channel)
        return conversation_outcomes.ResolvedReply(
            reply=reply, handler=selected.name, event_candidate_id=output.event_candidate_id,
            mission_id=output.mission_id)

    def _finalize_presentation(self, output, *, decision, profile, channel) -> str:
        """Runtime-owned presentation applied to a handler's RAW factual output (invariant 1: safety runs
        for every outcome; style runs on finalized factual content). Model freeform is rendered + voice-
        styled + safety-gated; fact-locked copy is authoritative — only the hard no-em-dash guarantee."""
        if output.styled:
            styled = self._style(output.text, decision=decision, profile=profile, channel=channel)
            return self._apply_safety_gates(styled, decision=decision, channel=channel)
        return enforce_no_dashes(output.text)

    async def handle(self, channel: MessagingChannel, msg: InboundMessage, *,
                     user_id: UUID, reply_target: str) -> InboundOutcome:
        ch, ident, pmid = msg.channel.value, msg.channel_identity, msg.provider_message_id
        # ONE ROOT TRACE PER INBOUND MESSAGE. Opened before any work, so `received` is when the turn
        # actually arrived rather than when instrumentation got to it. Every exit finishes it —
        # including the early returns, which are the turns whose timing matters most.
        from . import turn_trace
        trace = turn_trace.start(user_id=user_id, conversation_id=ident, message_id=pmid)

        if msg.is_group:                                        # 1:1 only in Bite 1 (privacy)
            turn_trace.record(trace.finish())
            return InboundOutcome(status="skipped_group", user_id=user_id)

        if await self._already_answered(user_id, ch, pmid):     # webhook redelivery -> no 2nd reply
            turn_trace.record(trace.finish())
            return InboundOutcome(status="duplicate", user_id=user_id)
        recent = await conversation_store.load_recent_turns(user_id, channel=ch, channel_identity=ident)

        await conversation_store.persist_user_turn(
            user_id, channel=ch, channel_identity=ident, provider_message_id=pmid, text=msg.text)
        profile = self.style.derive_profile([t.text for t in recent if t.role == "user" and t.text])
        turn_trace.guard(trace, "trusted_input_ready")

        # THE OPEN GOAL IS READ FIRST, once, and everything below is handed it.
        #
        # It moved above the refusal block deliberately. The boundary that decides whether this turn
        # closes outstanding consent has to be able to know WHAT IS PENDING: "YES WRITE IT AND SEND IT NO
        # MORE QUESTIONS" contains a negation, and whether that negation is about the pending send or
        # about being asked things is not answerable from the sentence alone. Reading the row afterwards
        # would mean two boundaries per turn with two different answers, which is the defect this spine
        # exists to remove.
        from . import continuation as continuation_mod, goal_handler, goal_selection
        goal_candidates: list[dict] = []
        selection = goal_selection.Selection(None, goal_selection.NOTHING_OPEN)
        turn_continuation = None
        if goal_handler.enabled():
            try:
                goal_candidates = await _open_goal_candidates(user_id, ident)
                # WHICH open goal is this turn about. Ordered evidence, never recency: an inline reply
                # names a message that belongs to one goal, and a yes answers the one goal holding a
                # question. Both are known before anything reasons, which is exactly why they are the
                # rules that run here — `goal_selection.refine` adds the model's reading further down.
                selection = goal_selection.select(
                    goal_candidates, text=msg.text,
                    reply_to_message_id=(msg.reply_to_message_id or msg.thread_root_message_id))
                log.info("goal_selection pmid=%s %s", pmid, selection.telemetry)
            except Exception:
                log.info("open_goal_read_failed pmid=%s", pmid)   # never costs the turn
        open_goal_row = selection.run

        # INVALIDATE CONFLICTING AUTHORIZATION — step 3 of the authoritative execution order, and it runs
        # here rather than inside the router because it must hold whether or not semantic routing is on.
        # A refusal has to close outstanding consent even when this turn goes nowhere near a tool: the
        # student who says "actually never mind" while a background mission is asleep gets no reply that
        # mentions it, and the mission must still be dead when it wakes an hour later.
        #
        # Best-effort, like the rest of intake — but note the direction it fails in. A store fault leaves
        # an authorization ALIVE, which is the unsafe direction, so it is logged at warning rather than
        # swallowed silently. The durable recheck at execution is the second line for exactly this case.
        envelope = None            # hoisted: semantic rescue below needs the SAME separation, not a copy
        scope_proposal = None      # hoisted: the goal handler spends the SAME reading, not a second one

        # WHAT DOES THE NEGATION IN THIS TURN GOVERN. Resolved once, from the student's own words and the
        # exact operation on screen, and only for a turn whose clauses genuinely disagree — every plain
        # yes and every plain no returns from here without a reader being consulted at all.
        #
        # IN ITS OWN TRY, deliberately. A fault while reading scope must not also skip the refusal record
        # below: that would fail in the unsafe direction (an authorization left alive) for a reason that
        # has nothing to do with authorization. No proposal is the safe degradation and the common case.
        try:
            from . import directive_scope, input_envelope as _env
            # TRUSTED TEXT ONLY. The envelope keeps OCR, attachment, quoted, forwarded and provider
            # content beside the student's own words rather than joined to them — a screenshot in which
            # someone else says "yes add it" has approved a pending proposal in this codebase before.
            envelope = _env.from_message(msg)
            scope_proposal = await directive_scope.resolve_attachment(
                envelope.authorizing_text(), pending=_pending_operation(open_goal_row))
        except Exception:
            log.info("scope_attachment_failed pmid=%s", pmid)   # degrade to the deterministic reading

        try:
            from . import authorization_store, input_envelope as _env2
            from . import user_action_boundary as _uab
            envelope = envelope or _env2.from_message(msg)
            _boundary = _uab.evaluate(envelope.authorizing_text(), scope=scope_proposal)
            if _boundary.blocks_execution():
                await authorization_store.record_refusal(
                    user_id, _boundary, message_id=pmid, conversation_id=ident)
        except Exception:
            log.warning("authz_refusal_record_failed pmid=%s", pmid)

        # CONTINUATION BEFORE ROUTING — the ordering the transcript proved was backwards.
        #
        # Routing asks "what kind of request is this text". Three turns of that conversation were answers
        # to work Bruce was already supposed to be doing, and text alone cannot answer them: "send it" does
        # not match the send pattern (it is looking for the word "email"), "make it professional" re-entered
        # the pipeline as a brand-new subjectless request, and an inline reply of "this" resolved to
        # nothing at all. The answerable question is "what does this turn DO to what is already open", and
        # only STATE can answer it. So state is read first, `continuation` resolves against it, and a turn
        # that lands on work in flight never reaches the classifier.
        #
        # Every piece of state here is fetched ONCE and handed onward: `continuation.resolve` is pure by
        # design, and a resolver that fetched its own would grow a second, drifting copy of the truth.
        if goal_handler.enabled():
            try:
                pending_decision, recent_draft, recent_tool_result = _continuation_state(open_goal_row)
                if open_goal_row is not None or pending_decision is not None:
                    # NOTHING IN FLIGHT MEANS NOTHING TO CONTINUE. Resolving against an empty world would
                    # turn an ordinary "nah" into an untargeted rejection and pull it out of routing for
                    # no reason; `continuation` reports that honestly, and this is the caller acting on it.
                    turn_continuation = continuation_mod.resolve(
                        user_id, text=msg.text,
                        reply_to_message_id=(msg.reply_to_message_id or msg.thread_root_message_id),
                        open_goal=open_goal_row, pending_decision=pending_decision,
                        recent_draft=recent_draft, recent_tool_result=recent_tool_result)
                    log.info("continuation pmid=%s kind=%s evidence=%s conf=%.2f", pmid,
                             turn_continuation.kind.value, turn_continuation.evidence,
                             turn_continuation.confidence)
            except Exception:
                log.info("continuation_error pmid=%s", pmid)   # never costs the turn; the router still runs
                turn_continuation = None
        # A turn that speaks to work in flight but names none of it is NOT a new request, so it is held
        # back from the classifier for the same reason a resolved continuation is: routing it would
        # re-enqueue whatever lane the original ask landed in, and the answer this turn is owed is one
        # question rather than a second copy of the work.
        resolved_continuation = ((turn_continuation is not None and turn_continuation.resolved)
                                 or selection.ambiguous)

        # G0.1 FastRouter: classify the cheapest-correct execution path (fast chat / single verified action /
        # foreground agent / durable mission) BEFORE the heavy reasoner, and instrument its latency. This turn
        # the decision is SHADOWED — recorded for the router-quality harness, latency telemetry, and the later
        # execution lanes that will act on it; the proven conversation pipeline below still runs. A router
        # glitch must never drop a turn, so classification is best-effort and content-free.
        from . import fast_router, mission_planner, tool_broker, tool_registry
        from .runtime_contracts import ExecutionClass
        router_ec: str | None = None
        router_ms: float | None = None
        # Bound before the try, not inside it: a router fault must leave these READABLE rather than
        # unbound, because everything below this block asks "what did the router say" and a NameError
        # raised while answering that would cost the student their reply.
        rd = None
        rt = None
        shortlisted: tuple[str, ...] | None = None
        mission_plan = None                # C1: set when a routed background mission was durably enqueued
        authoritative_decision = None      # G0 Activation Phase A: set -> skip the reasoner, dispatch on it
        try:
            turn_trace.guard(trace, "state_reads_started")
            # A MEASUREMENT GAP, recorded rather than faked. The pending-Decision read happens inside
            # `fast_router.route` -> `execution_derivation.build_context`, which has no trace to mark, so
            # its cost is folded into `router_finished` and cannot be attributed on its own. Marking it
            # here would attribute a read to the wrong place; leaving it silently null would make the gap
            # invisible.
            trace.absent("decisions_ready", turn_trace.NOT_APPLICABLE)
            if resolved_continuation:
                # Skipped, not merely ignored. Classifying a continuation would ALSO re-enqueue whatever
                # lane the original request landed in — a background send mission included — so a student
                # saying "yeah send it" could get the same email twice.
                trace.absent("router_started", turn_trace.NOT_APPLICABLE)
                trace.absent("router_finished", turn_trace.NOT_APPLICABLE)
                log.info("router_skipped pmid=%s reason=%s kind=%s evidence=%s", pmid,
                         "continuation" if turn_continuation is not None else selection.rule,
                         turn_continuation.kind.value if turn_continuation is not None else None,
                         turn_continuation.evidence if turn_continuation is not None else None)
            else:
                turn_trace.guard(trace, "router_started")
                rd, rt = await fast_router.route(
                    user_id, msg.text or "", has_attachments=bool(msg.attachments),
                    has_reply_ref=bool(msg.reply_to_message_id or msg.thread_root_message_id))
                turn_trace.guard(trace, "router_finished")
                router_ec, router_ms = rd.execution_class.value, rt.total_ms
                turn_trace.note(trace, execution_path=router_ec)
                log.info("router pmid=%s ec=%s action=%s domain=%s conf=%.2f src=%s stage0_ms=%.1f "
                         "stage1_ms=%.1f total_ms=%.1f", pmid, router_ec,
                         rd.action.value if rd.action else None, rd.domain, rd.confidence, rd.source,
                         rt.stage0_ms, rt.stage1_ms, rt.total_ms)
                # G0.3 ToolBroker (SHADOW): for a tool-bearing path, shortlist the FEW relevant, live,
                # connected tools the router→broker seam would hand a planner — never the whole registry.
                # Recorded for telemetry + the G0.4 planner that will consume it; execution is unchanged this
                # turn. Skipped for chat/plan paths (no provider tool), so pure conversation pays nothing.
                if rd.domain and rd.action and rd.execution_class in (
                        ExecutionClass.direct_action, ExecutionClass.foreground_agent):
                    sl = await tool_broker.shortlist(user_id, domain=rd.domain, action=rd.action,
                                                     candidate_capabilities=rd.candidate_capabilities)
                    shortlisted = tuple(c.capability for c in sl.candidates)
                    log.info("broker pmid=%s caps=%s actionable=%s dead=%s unavailable=%s", pmid,
                             list(shortlisted), sl.has_actionable, list(sl.excluded_dead),
                             list(sl.unavailable))
                # Phase A: AUTHORITATIVE router. On a deterministic text-action lane whose DB precondition the
                # router already verified, the winning handler ignores the reasoner's decision — so for a
                # canary user, skip the expensive vision pass and dispatch on a synthetic decision (identical
                # outcome).
                skip = router_authority.is_authoritative(user_id) and router_authority.reasoner_skippable(
                    rd, has_attachments=bool(msg.attachments),
                    has_reply_ref=bool(msg.reply_to_message_id or msg.thread_root_message_id))
                if skip and rd.domain == "calendar":
                    # the calendar handlers CLAIM only when connected; if not, defer to the reasoner so the
                    # honest "not connected" reply is still generated instead of an empty synthetic one
                    # (keeps parity). Phase B: the ToolBroker is the single capability-truth authority
                    # (live+connected+scoped); a known-live cap is the shared-connection proxy. Kill switch
                    # falls back to the registry.
                    if tool_broker.authority_enabled():
                        skip = (await tool_broker.availability(user_id, "calendar.update_event")).ok
                    else:
                        skip = await tool_registry.is_available("calendar.update_event", user_id)
                if skip:
                    authoritative_decision = router_authority.synthetic_decision(rd)
                # C1: a routed BACKGROUND mission becomes a DURABLE run right here. This is the seam that was
                # missing — until now `enqueue_background` had no live caller, so a mission only existed if a
                # job inserted one by hand. The key is derived from the inbound message, so a webhook
                # redelivery resolves to the SAME run instead of sending a second email. Best-effort like the
                # rest of this block: a planner fault must never cost the student their reply.
                if mission_planner.is_enqueueable(rd):
                    plan = await mission_planner.plan_mission(
                        user_id, rd, text=msg.text or "", source_message_id=pmid,
                        idempotency_key=mission_planner.mission_idempotency_key(ch, pmid))
                    mission_plan = plan
                    # surface the broker's shortlist for the MISSION lane too: the block above only computes
                    # one for direct/foreground, so without this a mission's capability truth is invisible.
                    if plan.shortlisted:
                        shortlisted = plan.shortlisted
                    log.info("mission_plan pmid=%s enqueued=%s status=%s run=%s existed=%s caps=%s",
                             pmid, plan.enqueued, plan.status, plan.run_id, plan.already_existed,
                             list(plan.shortlisted))
        except Exception:
            log.info("router_error pmid=%s", pmid)     # classification never blocks a reply
            authoritative_decision = None

        # SEMANTIC RESCUE (founder alpha; default off, allowlisted, kill-switchable). Stage 0 returned
        # UNKNOWN, which today silently becomes generic chat — so a real request phrased in a way nobody
        # anticipated gets a friendly reply and no work, and the student cannot tell "Bruce decided not
        # to" from "Bruce didn't understand". Rescue reads exactly those turns and either OWNS the reply
        # (a proposal, one question, a named block, a resolved confirmation) or hands the turn straight
        # back untouched. It never executes anything on the turn that proposes.
        #
        # Placed here, ahead of the vision pass, for two reasons: a rescued turn should not pay for a
        # reasoner whose answer is going to be discarded, and the confirmation branch must settle consent
        # from the founder's own trusted words BEFORE any model has had a chance to describe them.
        rescue_outcome: str | None = None
        from . import semantic_rescue_runtime
        if semantic_rescue_runtime.applies(user_id, rt):
            try:
                from . import input_envelope as _env2
                rescued = await semantic_rescue_runtime.rescue(
                    user_id, envelope=envelope or _env2.from_message(msg),
                    conversation_id=ident, source_message_id=pmid)
            except Exception:
                # Fail back to today's path, never to a wrong answer. Logged at warning because a rescue
                # that silently stops working would look exactly like a rescue that decided not to fire.
                log.warning("rescue_error pmid=%s", pmid)
                rescued = None
            if rescued is not None:
                rescue_outcome = rescued.outcome
                log.info("rescue pmid=%s outcome=%s owns=%s decision=%s", pmid, rescued.outcome,
                         rescued.owns_turn, rescued.decision_id)
                if rescued.owns_turn and rescued.reply:
                    # The response authority still runs. `handler` is what decides whether a completion
                    # claim is trusted, and only the executed path is named as an action handler.
                    reply_out = response_composer.no_false_completion(
                        enforce_no_dashes(rescued.reply), handler=rescued.handler)
                    await self._finalize(user_id, ch, ident, pmid, reply_out, reply_target, trace=trace,
                                         decision=None, intent="semantic_rescue", trusted_text=msg.text)
                    turn_trace.record(trace.finish())
                    return InboundOutcome(status="processed", user_id=user_id, execution_class=router_ec,
                                          router_ms=router_ms, shortlisted_capabilities=shortlisted,
                                          rescue_outcome=rescue_outcome, mission_id=rescued.mission_id)

        # A3.2: resolve the EXPLICITLY-referenced message/attachment/prior-answer into a bounded capsule the
        # GENERIC reasoner consumes as evidence (no reply-specific branch). Hoisted so both the authoritative
        # and the legacy path share it (a skippable turn has no reference -> a cheap, empty capsule).
        capsule = await conversation_context.resolve(user_id, msg)

        # THE ONE SNAPSHOT. Assembled once, here, before anything reasons, and handed onward rather than
        # rebuilt. The transcript's four symptoms were one cause: the reply path read one window of chat,
        # the routing path asked the MODEL whether a mission was needed, and capability truth stayed in
        # the broker where the replying model never saw it — three views of one turn, and the student got
        # the least informed one. `assemble` never raises (a store that is down costs its own field and
        # says so in `report_for(ctx)`), so the guard here is for a defect in this call, not in that one.
        #
        # `trace=None` on purpose. The assembler marks `decisions_ready` / `capabilities_ready` /
        # `agent_runs_ready` / `conversation_state_ready`, and `turn_trace.STAGES` orders those AROUND the
        # ContextCompiler's own `memory_ready` / `entities_ready`, which are marked further down. Taking
        # both sets would make every trace read as out of order and the latency baseline unusable; the
        # assembly's own cost is logged by `turn_context_assembled` instead.
        turn_ctx = None
        try:
            from . import turn_context_assembler
            turn_ctx = await turn_context_assembler.assemble(
                user_id,
                # The ENVELOPE, not the raw message: `turn_context._message_text` takes
                # `authorizing_text()` off it, so OCR'd and forwarded content stays out of the one field
                # every path treats as the student's instruction.
                latest_message=(envelope if envelope is not None else msg.text),
                reply_reference=(msg.reply_to_message_id or msg.thread_root_message_id or ""),
                recent_turns=recent, trace=None)
        except Exception:
            log.info("turn_context_error pmid=%s", pmid)

        if authoritative_decision is not None:
            # Phase A: the FastRouter OWNS this deterministic text-action lane and already verified its DB
            # precondition; the winning handler re-derives from msg.text + DB and ignores the reasoner. So
            # skip the vision pass entirely and dispatch on the synthetic decision — identical outcome, no
            # model on the hot path. (Skippable is gated to no-attachment/no-reply, so no perception is lost.)
            decision = authoritative_decision
            log.info("router_authoritative pmid=%s ec=%s action=%s reasoner=skipped", pmid, router_ec,
                     rd.action.value if rd.action else None)
        else:
            images, unreadable = _prepare_images(msg)

            # P0.1 AUTHORITATIVE TARGET. An explicit reply target is authoritative: Bruce must answer about the
            # EXACT message the student pointed at — never a newer/most-recent image, never unrelated recent
            # context. So when the referenced content can't be loaded AND this message carries no standalone
            # image of its own, fail closed with an honest, specific ask. This fires even when the student
            # typed a question alongside the reply (the question is ABOUT the thing we could not load) — the
            # earlier bug answered such replies from the newest image instead of admitting the miss.
            explicit_ref = (bool(msg.reply_to_message_id or msg.thread_root_message_id)
                            or capsule.resolution_source == conversation_context.RELAY_EXACT)
            if not capsule.referenced_images and not images:
                if capsule.attachment_pending:                      # replied to an image/file, not downloaded
                    reply = self.style.template("reply_attachment_pending")
                    await self._finalize(user_id, ch, ident, pmid, reply, reply_target, trace=trace,
                                         decision=None, intent="image_understanding", trusted_text=msg.text)
                    turn_trace.record(trace.finish())
                    return InboundOutcome(status="processed", user_id=user_id)
                if capsule.attachment_load_failed:                  # the exact file EXISTS but we couldn't load
                    # P0.2: fail closed honestly — we KNOW which image, we just couldn't load its bytes. Never
                    # tell the user to resend a file that is genuinely there, and never fall through to answer
                    # from the newest/nearest image.
                    reply = self.style.template("reply_image_unavailable")
                    await self._finalize(user_id, ch, ident, pmid, reply, reply_target, trace=trace,
                                         decision=None, intent="image_understanding", trusted_text=msg.text)
                    turn_trace.record(trace.finish())
                    return InboundOutcome(status="processed", user_id=user_id)
                if explicit_ref and not (capsule.referenced_text or capsule.prior_answer):
                    reply = self.style.template("reply_target_unavailable")        # target lost / nothing to show
                    await self._finalize(user_id, ch, ident, pmid, reply, reply_target, trace=trace,
                                         decision=None, intent="image_understanding", trusted_text=msg.text)
                    turn_trace.record(trace.finish())
                    return InboundOutcome(status="processed", user_id=user_id)

            images = capsule.referenced_images + images     # the referenced attachment is authoritative -> first

            # attachment the relay couldn't fetch OR bytes we genuinely can't open, and nothing else to go
            # on -> honest resend ask. (A healthy HEIC no longer lands here — it's normalized to JPEG above.)
            if (msg.attachment_unavailable or unreadable) and not (msg.text and msg.text.strip()) and not images:
                reply = self.style.template("could_not_read_attachment")
                await self._finalize(user_id, ch, ident, pmid, reply, reply_target, trace=trace,
                                     decision=None, intent="image_understanding", trusted_text=msg.text)
                turn_trace.record(trace.finish())
                return InboundOutcome(status="processed", user_id=user_id)

            # G0.2 ContextCompiler: assemble a BOUNDED, prioritized context from layered memory (world tz +
            # active calendar entities + any open agent run + a bounded conversation window) instead of dumping
            # the raw recent turns. P0.1 still holds: with an explicit, RESOLVED reply target THAT owns the
            # context, so episodic is WITHHELD — that window is exactly how a newer image B leaked in and got
            # answered instead of the replied-to image A. World/entity/operational still ground the reply. The
            # referenced content (fenced as DATA below) stands on its own. Compilation never drops a turn.
            # NARROW, don't delete. Withholding the window entirely was the P0.1 fix for a real leak, but
            # it also meant the one turn most likely to say "send it" or "this" was the one turn with no
            # memory of what "it" referred to — the recipient given two turns earlier was simply gone. The
            # reply target still dominates: it is fenced as DATA at a higher priority than episodic. What
            # changes is that the window shrinks instead of vanishing.
            # The hazard P0.1 fixed is ATTACHMENT-specific: a newer image B in the window got answered
            # instead of the image A the user pointed at. Text has no such competitor — pointing at a
            # draft and saying "this" needs the window, because that is where the recipient given two
            # turns ago still lives. So withhold only when the reference carries its own images, and
            # narrow otherwise. Blanket withholding is what made "send it" amnesiac.
            _owns_context = bool(explicit_ref and capsule.has_reference)
            include_episodic = not (_owns_context and bool(capsule.referenced_images))
            episodic_limit = context_compiler._REF_TURNS if _owns_context else None
            # M1: the runtime already knows what is live. Hand that to the model instead of letting it
            # guess — a guessed "i can't add it to your calendar" on a fully-scoped Google connection is
            # what blocked a P0 verification. Fault-isolated: a snapshot error degrades to no block, never
            # to a wrong one.
            try:
                cap_snapshot = await capability_snapshot.snapshot(user_id)
                turn_trace.guard(trace, "capabilities_ready")
            except Exception:
                cap_snapshot = None
                log.info("capability_snapshot_error pmid=%s", pmid)
            # THE MEMORY CUE — built from state the runtime already has, never from a model asking what
            # might be relevant. `rd` is the router's decision for this turn, so the domain and the
            # execution class it already derived are what scope the shortlist; entity resolution supplies
            # the subjects. A cue that had to be inferred would put a second inference on the hot path to
            # decide what to feed the first one.
            memory_cue = None
            try:
                from . import memory_retrieval
                _domains = tuple(d for d in ((rd.domain if rd is not None else None),) if d)
                _cls = (rd.execution_class.value if rd is not None else "")
                memory_cue = memory_retrieval.TurnCue(
                    text=msg.text or "",
                    domains=_domains,
                    entities=tuple(capsule.referenced_entities) if hasattr(capsule, "referenced_entities") else (),
                    active_run_domain=(rd.domain if rd is not None and rd.continuation_run_id else None),
                    has_pending_decision=bool(rd is not None and rd.decision_id),
                    turn_class=("action" if _cls in ("direct_action", "foreground_agent")
                                else "complex" if _cls == "background_mission" else "conversation"))
            except Exception:
                memory_cue = None       # a cue fault costs context, never the turn

            try:
                compiled = await context_compiler.compile(
                    user_id, recent, include_episodic=include_episodic, episodic_limit=episodic_limit, profile=profile,
                    capabilities=cap_snapshot, memory_cue=memory_cue, trace=trace)
                ctx = compiled.text
                if compiled.dropped:
                    log.info("ctx_compiled pmid=%s tokens=%s blocks=%s dropped=%s", pmid, compiled.est_tokens,
                             [b.layer for b in compiled.blocks], list(compiled.dropped))
            except Exception:
                log.info("ctx_compile_error pmid=%s", pmid)         # fall back to the honest minimal context
                ctx = _context(recent)   # the window is narrowed, never withheld — see episodic_limit above
            _ev = conversation_context.evidence_text(capsule)       # referenced content, fenced as DATA
            if _ev:
                ctx = ctx + "\n\n" + _ev
            try:
                turn_trace.note(trace, model_role="reasoner")
                turn_trace.guard(trace, "model_started")
                rr = await self.reasoner.decide(text=msg.text, images=images, context=ctx)
                turn_trace.guard(trace, "model_finished")
            except Exception:
                # A model/backend glitch is OUR fault, not the image's. Never say "couldn't read that" for
                # a healthy photo (the exact false-negative we're fixing) — own it and ask for a retry.
                # If a MISSION already ran this turn, its outcome outranks the glitch: a verified send
                # followed by a reasoner error must not tell the student "something glitched" while a
                # real email is already in their inbox and monitoring is running.
                reply = mission_planner.reply_for(mission_plan) or _FALLBACK
                await self._finalize(user_id, ch, ident, pmid, reply, reply_target, trace=trace,
                                     decision=None, intent="unsupported", trusted_text=msg.text)
                log.info("conv_model_error pmid=%s mission=%s", pmid,
                         mission_plan.status if mission_plan else None)
                turn_trace.record(trace.finish())
                return InboundOutcome(
                    status="model_error", user_id=user_id,
                    mission_run_id=(mission_plan.run_id if mission_plan and mission_plan.enqueued else None),
                    mission_status=(mission_plan.status if mission_plan else None),
                    shortlisted_capabilities=shortlisted)

            decision = rr.decision

        # THE SECOND INSTALMENT OF EVIDENCE. A drafted subject and a named operation are the reasoner's
        # output and cannot exist before it runs, so the selector gets them here — and may only break a
        # tie it could not break above. A goal it ALREADY named is final: `continuation.resolve` has by
        # now bound this turn's yes, amendment or pointer to that run, and re-selecting underneath it
        # would leave a confirmation of one goal's Decision sitting on another's.
        if goal_candidates:
            try:
                selection = goal_selection.refine(
                    selection, goal_candidates, text=msg.text,
                    reply_to_message_id=(msg.reply_to_message_id or msg.thread_root_message_id),
                    decision=decision)
                open_goal_row = selection.run
                log.info("goal_selection_refined pmid=%s %s", pmid, selection.telemetry)
            except Exception:
                log.info("goal_selection_refine_failed pmid=%s", pmid)

        # Outcome dispatch (D-INT-1/D-INT-3): two-phase evaluate -> priority select -> execute, presentation
        # runtime-owned. A handler-collision (two claims at the same top priority) is a config bug: it fails
        # loudly (error telemetry inside select_owner) but degrades the USER to a safe honest reply.
        try:
            turn_trace.guard(trace, "response_generation_started")
            outcome = await self._resolve_outcome(
                decision=decision, capsule=capsule, msg=msg, profile=profile, channel=ch,
                user_id=user_id, pmid=pmid, turn_context=turn_ctx, continuation=turn_continuation,
                # Position in the conversation, used to order slot values deterministically so a merge
                # cannot depend on which turn happened to be replayed last. `recent` is a BOUNDED window,
                # so this number saturates on a long thread — `goal_handler.turn_index_for` floors it
                # against the goal's own highest index, which is what keeps a late correction winning.
                open_goal=open_goal_row, conversation_id=ident, turn_index=len(recent) + 1,
                # The SAME attachment reading the refusal boundary above was judged with. Handing it down
                # rather than letting the handler resolve its own is the difference between one reading of
                # this turn and two that can disagree about consent — and, if the model reader is ever
                # turned on, between one provider call and two.
                scope=scope_proposal,
                # WHICH goal, and by which rule. The handler reads `ambiguous` off it to ask one question
                # instead of guessing between two goals — the guess is how a bare "no" cancels the one the
                # student was not talking about.
                selection=selection,
                # A background mission may ALREADY have sent something on this turn. Nothing else may
                # act on it, or the student gets the same email twice.
                mission_lane_ran=bool(mission_plan is not None and mission_plan.enqueued))
        except conversation_outcomes.OutcomeCollision:
            log.error("conv_outcome_collision pmid=%s", pmid)      # loud; details already logged, no content
            await self._finalize(user_id, ch, ident, pmid,
                                 mission_planner.reply_for(mission_plan) or _FALLBACK, reply_target,
                                 decision=None, intent="unsupported", trace=trace, trusted_text=msg.text)
            turn_trace.record(trace.finish())
            return InboundOutcome(
                status="outcome_collision", user_id=user_id,
                mission_run_id=(mission_plan.run_id if mission_plan and mission_plan.enqueued else None),
                mission_status=(mission_plan.status if mission_plan else None))

        # P0.5 capability-truth guard: never let a model reply DENY a capability that is actually live.
        # Cheap regex first; only pay for the connection lookup when the reply reads like a calendar denial.
        reply_out = outcome.reply

        # M1 contradiction validator: a reply may NEVER deny a capability the snapshot says is live.
        # Structural, not a denial-phrase regex — the previous guard was exactly that and missed
        # "i can't" written with a curly apostrophe, so a false denial shipped to a student.
        _snap = locals().get("cap_snapshot")
        if _snap is not None:
            _wrong = capability_snapshot.contradicts(reply_out, _snap)
            if _wrong:
                log.info("capability_contradiction pmid=%s family=%s", pmid, _wrong)
                # Both families now have a correction. Previously only calendar did, so an email denial
                # was detected, logged, and then shipped verbatim — the detector was right and the reply
                # lied anyway. A family with no correction still falls through unchanged rather than
                # being silently rewritten by the wrong one.
                _corrections = {"calendar": capability_truth.grounded_calendar_correction,
                                "email": capability_truth.grounded_email_correction}
                _fix = _corrections.get(_wrong)
                reply_out = _fix(msg.text) if _fix is not None else reply_out

        # C1/D: when a MISSION lane ran, the runtime's own outcome decides the words, not the reasoner.
        # The reasoner writes without knowing what was executed, and that produced BOTH live failures we
        # have seen: a cheerful "i've got it" for work never started, and "i can't send emails from here"
        # for a mission that had just sent a verified email and enqueued monitoring. A state-grounded
        # reply cannot contradict the run, in either direction.
        if mission_plan is not None:
            grounded = mission_planner.reply_for(mission_plan)
            if grounded:
                if grounded != reply_out:
                    log.info("mission_reply_grounded pmid=%s status=%s enqueued=%s verified=%s",
                             pmid, mission_plan.status, mission_plan.enqueued,
                             mission_plan.verified_send)
                reply_out = grounded
        if capability_truth.mentions_calendar_denial(reply_out):
            from . import oauth_google
            try:
                integ = await oauth_google.get_integration(user_id)
            except Exception:
                integ = None
            if integ is not None and integ.status == "connected" and integ.revoked_at is None:
                log.info("capability_claim_override pmid=%s cap=calendar_connected", pmid)
                reply_out = capability_truth.grounded_calendar_correction(msg.text)

        # G0.6 response-experience guard: the dual of the capability-truth check. Never let a reply that no
        # verified action handler produced fabricate that a calendar change LANDED ("added it ✅"). A real
        # action handler's success copy (only emitted after a read-back) passes through untouched.
        pre_guard = reply_out
        reply_out = response_composer.no_false_completion(reply_out, handler=outcome.handler)
        if reply_out is not pre_guard:
            log.info("false_completion_downgraded pmid=%s handler=%s", pmid, outcome.handler)

        # The FUTURE-tense dual. "i'll send it" is only true if something will actually send it, so the
        # database is asked whether a Goal with a pending Decision exists for a reachable operation.
        # Checked ONLY when the reply actually promises something, so an ordinary turn pays nothing.
        if response_composer.promises_action(reply_out):
            live_decision = await _has_live_decision(user_id, ident)
            pre_promise = reply_out
            reply_out = response_composer.no_false_promise(
                reply_out, handler=outcome.handler, has_live_decision=live_decision)
            if reply_out is not pre_promise:
                log.warning("false_promise_downgraded pmid=%s handler=%s", pmid, outcome.handler)

        await self._finalize(user_id, ch, ident, pmid, reply_out, reply_target,
                             decision=decision, event_candidate_id=outcome.event_candidate_id, trusted_text=msg.text)
        log.info("conv_ok pmid=%s intent=%s rt=%s ec=%s mission=%s handler=%s route=%s route_ms=%s",
                 pmid, decision.intent.value, decision.response_type.value,
                 outcome.event_candidate_id is not None, outcome.mission_id is not None, outcome.handler,
                 router_ec, None if router_ms is None else round(router_ms, 1))
        turn_trace.record(trace.finish())
        return InboundOutcome(status="processed", user_id=user_id,
                              execution_class=router_ec, router_ms=router_ms,
                              shortlisted_capabilities=shortlisted, rescue_outcome=rescue_outcome,
                              mission_run_id=(mission_plan.run_id if mission_plan and mission_plan.enqueued
                                              else None),
                              mission_status=(mission_plan.status if mission_plan else None))

    def _present(self, text: str, *, decision: ConversationDecision, profile, channel: str) -> str:
        """Channel-aware presentation = humanity styling THEN trust safety gates (D-INT-2 seam).

        Split so conversation-humanity (voice/rendering) and trust-evals (channel/fact safety) edit
        DISJOINT methods instead of colliding on one function. Presentation only — every value/sign/
        entry is preserved. Order is load-bearing: style first (may emit an em dash / a redundant offer),
        gates second (guarantee they never ship)."""
        styled = self._style(text, decision=decision, profile=profile, channel=channel)
        return self._apply_safety_gates(styled, decision=decision, channel=channel)

    def _style(self, text: str, *, decision: ConversationDecision, profile, channel: str) -> str:
        """conversation-humanity owned. LaTeX/Markdown -> readable plain text/Unicode (fact-equivalence-
        guarded), then voice styling that leaves technical lines verbatim. Non-plain channels pass
        through unstyled math. Adds no safety guarantee — that is _apply_safety_gates' job."""
        readable = technical_render.render_for_channel(text, channel=channel)
        return self.style.render(readable, risk_level=decision.risk_level, profile=profile,
                                 protect_technical=True)

    def _apply_safety_gates(self, styled: str, *, decision: ConversationDecision, channel: str) -> str:
        """trust-evals owned. Deterministic guarantees applied AFTER styling, never trusting the model or
        the voice pass to comply: no raw TeX/Markdown reaches a plain-text channel (last-resort re-clean),
        no redundant trailing 'want me to…' offer on a non-serious reply, and no em dash EVER. Fact-
        preserving (matrices contain no em dash)."""
        if channel in technical_render.PLAIN_TEXT_CHANNELS and technical_render.forbidden_tokens(styled):
            styled = technical_render.render_for_channel(styled, channel=channel)   # last-resort re-clean
        if decision.risk_level not in (RiskLevel.sensitive, RiskLevel.high):
            styled = strip_redundant_offer(styled)
        styled = enforce_no_dashes(styled)
        assert "—" not in styled, "em dash must never ship to a plain-text channel"
        return styled

    async def _already_answered(self, user_id, channel, pmid) -> bool:
        from .db import user_session
        from sqlalchemy import select
        from . import schema
        async with user_session(user_id) as s:
            return (await s.execute(select(schema.ConversationTurn.id).where(
                schema.ConversationTurn.user_id == user_id, schema.ConversationTurn.channel == channel,
                schema.ConversationTurn.provider_message_id == pmid,
                schema.ConversationTurn.role == "assistant"))).scalar_one_or_none() is not None

    async def _finalize(self, user_id, ch, ident, pmid, reply, reply_target, *,
                        decision: ConversationDecision | None, event_candidate_id=None, intent=None,
                        trace=None, trusted_text: str | None = None):
        # persist the assistant turn, then enqueue EXACTLY ONE outbound (idempotent on conv:{pmid}).
        if decision is not None:
            await conversation_store.persist_assistant_turn(
                user_id, channel=ch, channel_identity=ident, provider_message_id=pmid,
                decision=decision, styled_text=reply, event_candidate_id=event_candidate_id)
            kind = "acknowledged"
        else:
            # fallback/no-decision turn: store a minimal record without a decision blob
            from . import schema
            from .db import user_session
            async with user_session(user_id) as s:
                if not await conversation_store._turn_exists(s, user_id, ch, pmid, "assistant"):
                    s.add(schema.ConversationTurn(
                        user_id=user_id, channel=ch, channel_identity=ident, provider_message_id=pmid,
                        role="assistant", intent=intent, text=reply))
            kind = "acknowledged"
        from . import turn_trace
        # `relay_send_started` is the ENQUEUE, not the send. The relay hands back a GUID asynchronously,
        # so `relay_guid_received` belongs to the delivery path and is marked there — leaving it null
        # here rather than pretending this turn observed it.
        turn_trace.guard(trace, "response_ready")
        turn_trace.guard(trace, "relay_send_started")
        if trace is not None:
            trace.absent("relay_guid_received", turn_trace.NOT_APPLICABLE)
        await messaging_outbound.enqueue(
            user_id=user_id, to_handle=reply_target, channel=ChannelKind.self_hosted_imessage,
            kind=kind, text=reply, idempotency_key=f"conv:{pmid}")

        # The memory stack finally has a caller. This runs AFTER the reply is enqueued, deliberately: the
        # student's answer is already safe, so a memory failure costs a remembered preference rather than
        # a turn. `memory_finalize` writes aggregate style only — never task slots, which stay in the
        # goal, and never guesses.
        try:
            from . import memory_finalize
            await memory_finalize.after_turn(user_id, trusted_text=trusted_text,
                                             source_message_id=pmid)
        except Exception:
            log.info("memory_finalize_failed pmid=%s", pmid)


async def handle(channel: MessagingChannel, msg: InboundMessage, *, user_id: UUID, reply_target: str,
                 reasoner: ConversationReasoner | None = None,
                 style: ConversationStyleEngine | None = None,
                 handlers: list | None = None, fallback=None) -> InboundOutcome:
    return await _Runtime(reasoner=reasoner, style=style, handlers=handlers, fallback=fallback).handle(
        channel, msg, user_id=user_id, reply_target=reply_target)
