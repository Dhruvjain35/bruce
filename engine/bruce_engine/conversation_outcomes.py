"""Outcome-dispatch seam (D-INT-1) — the ONE place a conversation decision becomes a reply.

Before this module, ``conversation_runtime.handle()`` resolved a decision with a hardcoded
``if _is_event / else`` branch. Every future capability (a mission handoff, a school "what's due"
answer, an approval prompt) would have had to edit those same ~30 lines, so four parallel workstreams
would collide on ``handle()`` every merge.

Instead, ``handle()`` now runs an ORDERED pipeline of ``OutcomeHandler``s over an ``OutcomeContext``.
The first handler that returns a ``ResolvedReply`` wins; a terminal default handler always resolves, so
the pipeline never falls through. A workstream adds a capability by adding a handler module and inserting
it into the list — it never touches ``handle()`` control flow.

Contract (integration-owned):
  * ``OutcomeContext`` fields are APPEND-ONLY. A handler reads new fields; nothing is removed/renamed.
  * A handler is PURE w.r.t. control flow: it returns a ``ResolvedReply`` or ``None`` (pass to the next).
    Side effects (persisting an event candidate, later creating a mission) happen inside ``resolve`` and
    are recorded on the returned reply so ``_finalize`` can persist them.
  * Behavior parity: ``EventCandidateHandler`` + ``DefaultReplyHandler`` reproduce the pre-seam branch
    EXACTLY, in that order. This module changes no user-visible behavior on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol, runtime_checkable
from uuid import UUID

if TYPE_CHECKING:                                      # type-only: no runtime import cycle with the runtime
    from .conversation_context import ContextCapsule
    from .conversation_contract import ConversationDecision
    from .conversation_style import ConversationStyleEngine
    from .messaging import InboundMessage


# --------------------------------------------------------------------------------------------------
# Event helpers — relocated verbatim from conversation_runtime (they are event-OUTCOME logic, and the
# runtime importing them back would be a cycle). Nothing outside the runtime referenced them.
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
    """An event = a title-like AND a date-like entity. Robust to model phrasing (do NOT rely on an
    exact capability id — the real model says 'calendar creation', 'add to calendar', etc.)."""
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
@dataclass
class OutcomeContext:
    """Everything a handler needs to resolve ONE decision into a reply. Read-only inputs plus the
    runtime's presentation + persistence helpers, so a handler never re-derives them. APPEND-ONLY."""
    user_id: UUID
    decision: "ConversationDecision"
    capsule: "ContextCapsule"
    msg: "InboundMessage"
    profile: object                                    # VoiceProfile (opaque to handlers)
    channel: str
    pmid: str
    present: Callable[..., str]                         # bound _Runtime._present (channel render + style)
    style: "ConversationStyleEngine"
    store: object                                      # conversation_store module (persist_event_candidate…)


@dataclass
class ResolvedReply:
    """A handler's result: the reply text + any side effect the runtime must persist on finalize."""
    reply: str
    handler: str                                       # which handler produced it (content-free telemetry)
    event_candidate_id: UUID | None = None


@runtime_checkable
class OutcomeHandler(Protocol):
    name: str
    async def resolve(self, octx: OutcomeContext) -> ResolvedReply | None: ...


# --------------------------------------------------------------------------------------------------
# Default handlers (behavior parity with the pre-seam branch)
# --------------------------------------------------------------------------------------------------
class EventCandidateHandler:
    """An extracted event -> persist a candidate (durable, provenance-kept) then reply with the honest
    fact-locked 'can't add to calendar yet' template (never 'added'), or the styled model reply. Ported
    verbatim from the pre-seam handle() event branch."""
    name = "event_candidate"

    async def resolve(self, octx: OutcomeContext) -> ResolvedReply | None:
        d = octx.decision
        if not _is_event(d):
            return None
        title, when, where, provenance = _event_fields(d)
        ec_id = await octx.store.persist_event_candidate(
            octx.user_id, title=title,
            idempotency_key=f"ec:{octx.channel}:{octx.pmid}",
            confidence=d.confidence,
            missing_fields=[f for f in ("date",) if not when] or None,
            provenance={**provenance, "inbound_provider_message_id": octx.pmid})
        if _wants_calendar(d, octx.msg.text):
            reply = octx.style.template("event_saved_calendar_unavailable", title=title,
                                        when=when, where=where)
        else:
            reply = octx.present(d.user_visible_response, decision=d, profile=octx.profile,
                                 channel=octx.channel)
        return ResolvedReply(reply=reply, handler=self.name, event_candidate_id=ec_id)


class DefaultReplyHandler:
    """Terminal handler: the model's honest reply, styled. Always resolves, so the pipeline can never
    fall through. No autonomous mission is ever created here (that is a dedicated handler)."""
    name = "default_reply"

    async def resolve(self, octx: OutcomeContext) -> ResolvedReply | None:
        d = octx.decision
        reply = octx.present(d.user_visible_response, decision=d, profile=octx.profile,
                             channel=octx.channel)
        return ResolvedReply(reply=reply, handler=self.name)


def default_handlers() -> list[OutcomeHandler]:
    """The ordered outcome pipeline. Workstreams INSERT capability handlers before DefaultReplyHandler
    (which must stay last — it always resolves). Order = event-candidate, then the styled default."""
    return [EventCandidateHandler(), DefaultReplyHandler()]
