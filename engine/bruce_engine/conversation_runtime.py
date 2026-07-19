"""The conversation brain orchestrator for ONE linked inbound (Bite 1), flag-gated.

Flow: gate (1:1 only) -> idempotency -> read attachments -> reason (one vision pass) -> branch
(tutoring / event-candidate / unsupported / casual / unreadable) -> style -> persist turn + optional
event candidate -> enqueue EXACTLY ONE outbound. Never creates a durable mission from the model and
never promises an unsolicited follow-up (deferred). Never claims an action happened that didn't.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from . import conversation_store, messaging_outbound
from .conversation_contract import ConversationDecision, IntentKind, RiskLevel
from .conversation_model import ConversationReasoner, VisionInput, production_reasoner
from .conversation_style import ConversationStyleEngine, VoiceProfile
from .messaging import ChannelKind, InboundMessage, MessagingChannel
from .messaging_inbound import InboundOutcome

log = logging.getLogger("bruce.conversation")   # content-free: ids/intents/statuses only, never text

_FALLBACK = "ngl something glitched on my end 😅 mind sending that again?"


def enabled_for(handle: str) -> bool:
    """Master flag ON *and* the handle explicitly allow-listed. Empty list => no one (fail closed)."""
    if os.environ.get("BRUCE_CONVERSATION_RUNTIME", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    allow = {h.strip() for h in os.environ.get("BRUCE_CONVERSATION_TEST_HANDLES", "").split(",") if h.strip()}
    return handle in allow


def _images(msg: InboundMessage) -> list[VisionInput]:
    out = []
    for a in msg.attachments:
        data = getattr(a, "data", None)
        if data:
            out.append(VisionInput(data=data, media_type=getattr(a, "media_type", None) or "image/png"))
    return out


def _context(recent: list) -> str:
    if not recent:
        return "No prior conversation."
    lines = [f"{t.role}: {t.text}" for t in recent if t.text]
    return "Recent conversation (oldest first):\n" + "\n".join(lines[-8:])


def _event_fields(decision: ConversationDecision) -> tuple[str, str, str, dict]:
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


def _wants_deferred_calendar(decision: ConversationDecision) -> bool:
    return "calendar_write" in [c.lower() for c in decision.required_capabilities]


class _Runtime:
    def __init__(self, reasoner: ConversationReasoner | None = None,
                 style: ConversationStyleEngine | None = None) -> None:
        self.reasoner = reasoner or production_reasoner()
        self.style = style or ConversationStyleEngine()

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
        images = _images(msg)

        # attachment the relay couldn't fetch, and nothing else to go on -> honest resend ask
        if msg.attachment_unavailable and not (msg.text and msg.text.strip()) and not images:
            reply = self.style.template("could_not_read_attachment")
            await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                                 decision=None, intent="image_understanding")
            return InboundOutcome(status="processed", user_id=user_id)

        try:
            rr = await self.reasoner.decide(text=msg.text, images=images, context=_context(recent))
        except Exception:
            reply = self.style.template("could_not_read_attachment") if images else _FALLBACK
            await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                                 decision=None, intent="unsupported")
            log.info("conv_model_error pmid=%s", pmid)
            return InboundOutcome(status="model_error", user_id=user_id)

        decision = rr.decision
        event_candidate_id: UUID | None = None

        if _wants_deferred_calendar(decision) and decision.extracted_entities:
            # event ask, calendar not wired: PERSIST the candidate + honest template. NEVER "added".
            title, when, where, provenance = _event_fields(decision)
            event_candidate_id = await conversation_store.persist_event_candidate(
                user_id, title=title,
                idempotency_key=f"ec:{ch}:{pmid}",
                confidence=decision.confidence,
                missing_fields=[e for e in ("date",) if not when] or None,
                provenance={**provenance, "inbound_provider_message_id": pmid})
            reply = self.style.template("event_saved_calendar_unavailable", title=title,
                                        when=when, where=where)
        elif decision.intent is IntentKind.unsupported or (
                decision.needs_mission and not _wants_deferred_calendar(decision)):
            # unsupported / would-need-a-mission: honest, no false "on it", no autonomous mission.
            reply = self.style.render(decision.user_visible_response, risk_level=decision.risk_level,
                                      profile=profile)
        else:
            reply = self.style.render(decision.user_visible_response, risk_level=decision.risk_level,
                                      profile=profile)

        await self._finalize(user_id, ch, ident, pmid, reply, reply_target,
                             decision=decision, event_candidate_id=event_candidate_id)
        log.info("conv_ok pmid=%s intent=%s rt=%s ec=%s", pmid, decision.intent.value,
                 decision.response_type.value, event_candidate_id is not None)
        return InboundOutcome(status="processed", user_id=user_id)

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
                 style: ConversationStyleEngine | None = None) -> InboundOutcome:
    return await _Runtime(reasoner=reasoner, style=style).handle(
        channel, msg, user_id=user_id, reply_target=reply_target)
