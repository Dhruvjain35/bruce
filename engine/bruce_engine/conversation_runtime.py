"""The conversation brain orchestrator for ONE linked inbound (Bite 1), flag-gated.

Flow: gate (1:1 only) -> idempotency -> read attachments -> reason (one vision pass) -> branch
(tutoring / event-candidate / unsupported / casual / unreadable) -> style -> persist turn + optional
event candidate -> enqueue EXACTLY ONE outbound. Never creates a durable mission from the model and
never promises an unsolicited follow-up (deferred). Never claims an action happened that didn't.
"""

from __future__ import annotations

import logging
from uuid import UUID

from . import (capability_truth, conversation_context, conversation_outcomes, conversation_store,
               messaging_outbound, technical_render)
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

    async def _resolve_outcome(self, *, decision, capsule, msg, profile, channel, user_id, pmid):
        """Two-phase dispatch (invariant 1): PURE evaluate() over every handler, deterministic priority
        selection (claims outrank blocked; single top-priority owner; a tie fails loudly; zero owners ->
        explicit fallback), then execute() ONLY the selected handler (mutation happens here, post-
        selection). Presentation (render/style/safety) is applied AFTER, by the runtime, not the handler."""
        octx = conversation_outcomes.OutcomeContext(
            user_id=user_id, decision=decision, capsule=capsule, msg=msg, profile=profile,
            channel=channel, pmid=pmid, style=self.style, store=conversation_store)
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

        if msg.is_group:                                        # 1:1 only in Bite 1 (privacy)
            return InboundOutcome(status="skipped_group", user_id=user_id)

        if await self._already_answered(user_id, ch, pmid):     # webhook redelivery -> no 2nd reply
            return InboundOutcome(status="duplicate", user_id=user_id)
        recent = await conversation_store.load_recent_turns(user_id, channel=ch, channel_identity=ident)

        await conversation_store.persist_user_turn(
            user_id, channel=ch, channel_identity=ident, provider_message_id=pmid, text=msg.text)
        profile = self.style.derive_profile([t.text for t in recent if t.role == "user" and t.text])

        # G0.1 FastRouter: classify the cheapest-correct execution path (fast chat / single verified action /
        # foreground agent / durable mission) BEFORE the heavy reasoner, and instrument its latency. This turn
        # the decision is SHADOWED — recorded for the router-quality harness, latency telemetry, and the later
        # execution lanes that will act on it; the proven conversation pipeline below still runs. A router
        # glitch must never drop a turn, so classification is best-effort and content-free.
        from . import fast_router
        router_ec: str | None = None
        router_ms: float | None = None
        try:
            rd, rt = await fast_router.route(
                user_id, msg.text or "", has_attachments=bool(msg.attachments),
                has_reply_ref=bool(msg.reply_to_message_id or msg.thread_root_message_id))
            router_ec, router_ms = rd.execution_class.value, rt.total_ms
            log.info("router pmid=%s ec=%s action=%s domain=%s conf=%.2f src=%s stage0_ms=%.1f "
                     "stage1_ms=%.1f total_ms=%.1f", pmid, router_ec,
                     rd.action.value if rd.action else None, rd.domain, rd.confidence, rd.source,
                     rt.stage0_ms, rt.stage1_ms, rt.total_ms)
        except Exception:
            log.info("router_error pmid=%s", pmid)     # classification never blocks a reply
        # A3.2: resolve the EXPLICITLY-referenced message/attachment/prior-answer into a bounded capsule
        # the GENERIC reasoner consumes as evidence (no reply-specific branch).
        capsule = await conversation_context.resolve(user_id, msg)
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
                await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                                     decision=None, intent="image_understanding")
                return InboundOutcome(status="processed", user_id=user_id)
            if capsule.attachment_load_failed:                  # the exact file EXISTS but we couldn't load
                # P0.2: fail closed honestly — we KNOW which image, we just couldn't load its bytes. Never
                # tell the user to resend a file that is genuinely there, and never fall through to answer
                # from the newest/nearest image.
                reply = self.style.template("reply_image_unavailable")
                await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                                     decision=None, intent="image_understanding")
                return InboundOutcome(status="processed", user_id=user_id)
            if explicit_ref and not (capsule.referenced_text or capsule.prior_answer):
                reply = self.style.template("reply_target_unavailable")        # target lost / nothing to show
                await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                                     decision=None, intent="image_understanding")
                return InboundOutcome(status="processed", user_id=user_id)

        images = capsule.referenced_images + images     # the referenced attachment is authoritative -> first

        # attachment the relay couldn't fetch OR bytes we genuinely can't open, and nothing else to go
        # on -> honest resend ask. (A healthy HEIC no longer lands here — it's normalized to JPEG above.)
        if (msg.attachment_unavailable or unreadable) and not (msg.text and msg.text.strip()) and not images:
            reply = self.style.template("could_not_read_attachment")
            await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                                 decision=None, intent="image_understanding")
            return InboundOutcome(status="processed", user_id=user_id)

        # P0.1: with an explicit, RESOLVED reply target, THAT is the context. Do not also dump the recent
        # turns — that window is exactly how a newer image B leaked in and got answered instead of the
        # replied-to image A. The referenced content (fenced as DATA below) stands on its own.
        if explicit_ref and capsule.has_reference:
            ctx = "No prior conversation."
        else:
            ctx = _context(recent)
        _ev = conversation_context.evidence_text(capsule)       # referenced content, fenced as DATA
        if _ev:
            ctx = ctx + "\n\n" + _ev
        try:
            rr = await self.reasoner.decide(text=msg.text, images=images, context=ctx)
        except Exception:
            # A model/backend glitch is OUR fault, not the image's. Never say "couldn't read that" for
            # a healthy photo (the exact false-negative we're fixing) — own it and ask for a retry.
            reply = _FALLBACK
            await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                                 decision=None, intent="unsupported")
            log.info("conv_model_error pmid=%s", pmid)
            return InboundOutcome(status="model_error", user_id=user_id)

        decision = rr.decision

        # Outcome dispatch (D-INT-1/D-INT-3): two-phase evaluate -> priority select -> execute, presentation
        # runtime-owned. A handler-collision (two claims at the same top priority) is a config bug: it fails
        # loudly (error telemetry inside select_owner) but degrades the USER to a safe honest reply.
        try:
            outcome = await self._resolve_outcome(decision=decision, capsule=capsule, msg=msg,
                                                  profile=profile, channel=ch, user_id=user_id, pmid=pmid)
        except conversation_outcomes.OutcomeCollision:
            log.error("conv_outcome_collision pmid=%s", pmid)      # loud; details already logged, no content
            await self._finalize(user_id, ch, ident, pmid, _FALLBACK, reply_target,
                                 decision=None, intent="unsupported")
            return InboundOutcome(status="outcome_collision", user_id=user_id)

        # P0.5 capability-truth guard: never let a model reply DENY a capability that is actually live.
        # Cheap regex first; only pay for the connection lookup when the reply reads like a calendar denial.
        reply_out = outcome.reply
        if capability_truth.mentions_calendar_denial(reply_out):
            from . import oauth_google
            try:
                integ = await oauth_google.get_integration(user_id)
            except Exception:
                integ = None
            if integ is not None and integ.status == "connected" and integ.revoked_at is None:
                log.info("capability_claim_override pmid=%s cap=calendar_connected", pmid)
                reply_out = capability_truth.grounded_calendar_correction(msg.text)

        await self._finalize(user_id, ch, ident, pmid, reply_out, reply_target,
                             decision=decision, event_candidate_id=outcome.event_candidate_id)
        log.info("conv_ok pmid=%s intent=%s rt=%s ec=%s mission=%s handler=%s route=%s route_ms=%s",
                 pmid, decision.intent.value, decision.response_type.value,
                 outcome.event_candidate_id is not None, outcome.mission_id is not None, outcome.handler,
                 router_ec, None if router_ms is None else round(router_ms, 1))
        return InboundOutcome(status="processed", user_id=user_id,
                              execution_class=router_ec, router_ms=router_ms)

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
                        decision: ConversationDecision | None, event_candidate_id=None, intent=None):
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
        await messaging_outbound.enqueue(
            user_id=user_id, to_handle=reply_target, channel=ChannelKind.self_hosted_imessage,
            kind=kind, text=reply, idempotency_key=f"conv:{pmid}")


async def handle(channel: MessagingChannel, msg: InboundMessage, *, user_id: UUID, reply_target: str,
                 reasoner: ConversationReasoner | None = None,
                 style: ConversationStyleEngine | None = None,
                 handlers: list | None = None, fallback=None) -> InboundOutcome:
    return await _Runtime(reasoner=reasoner, style=style, handlers=handlers, fallback=fallback).handle(
        channel, msg, user_id=user_id, reply_target=reply_target)
