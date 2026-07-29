"""FastRouter (G0.1) — pick the cheapest correct execution path BEFORE any planning.

Bruce must not run a giant planning loop for every text. Most requests are fast chat or a single verified
tool call; only genuinely ambiguous, multi-step, or long-running work needs a planner or a durable mission.

Two stages, cheapest first:
  * Stage 0 — DETERMINISTIC structural signals (no model): a pending Decision being resolved, an explicit
    correction, a mutation verb on a concrete entity, scheduling intent, a world-state statement, a handoff.
    These carry high confidence and cover the live (calendar) surface with zero model latency.
  * Stage 1 — a COMPACT router model, only when Stage 0 is inconclusive. Classifies meaning, not phrases.
    (Pluggable; until non-calendar tools are live, the default is fast_conversation — with only the calendar
    hands connected, anything Stage 0 didn't catch is Bruce chatting.)

The heavy planner is Stage 2 elsewhere, reached only when the router marks needs_deeper_planning.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from .runtime_contracts import ExecutionClass, GoalAction, RouterDecision


@dataclass
class RouterTiming:
    stage0_ms: float = 0.0
    stage1_ms: float = 0.0
    total_ms: float = 0.0
    # Did the DETERMINISTIC stage answer, or did it fall through? Recorded rather than inferred from
    # `source == "router_default"`, which is only equivalent while Stage 1 is off: the day the compact
    # router is enabled, that inference silently starts reporting every Stage-0 miss as a hit, and
    # anything gated on "Stage 0 returned UNKNOWN" stops firing without failing.
    stage0_resolved: bool = True


class RouterModel(Protocol):
    async def route(self, text: str) -> RouterDecision: ...


# A COMMAND to put something on the calendar (so "can u add X thurs" is still a create, despite "can u").
_COMMAND_VERB = re.compile(
    r"\b(add|put|schedule|block\s+off|save|create|set\s+up|pencil|throw|slot|book|remind\s+me|make\s+an?\s+event)\b",
    re.IGNORECASE)
# A generic "send someone a message" intent — the SHARED router's send verb, NOT a Gmail-specific route. It
# resolves to the provider-neutral send action on the send-capable hand (Gmail today); the broker + loop +
# response handle it exactly like calendar. Deliberately tight so "send me the plan"/"message me" don't fire:
# an email/message/note verb aimed outward.
_SEND_INTENT = re.compile(
    r"\b(e-?mail|send\s+(?:an?\s+|them\s+|him\s+|her\s+|over\s+)?(?:email|message|msg|note))\b", re.IGNORECASE)
# A send that also asks to be TOLD about the reply is a durable monitor -> background mission (not a bare send).
_FOLLOWUP = re.compile(
    r"\b(let\s+me\s+know|tell\s+me|notify\s+me|ping\s+me|follow(?:\s|-)?up|"
    r"when\s+they\s+(?:reply|respond|get\s+back|answer)|if\s+they\s+(?:don'?t|dont|haven'?t|havent))\b",
    re.IGNORECASE)
# An interrogative / non-command framing ("is it gonna rain tmr", "hru", "gimme a plan", "should i…").
_INTERROGATIVE = re.compile(
    r"^\s*(?:is|are|am|do|does|did|can|could|would|should|will|what|whats|when|whens|where|why|who|how|hru|"
    r"gimme|give\s+me|lmk|tell\s+me)\b|\?\s*$", re.IGNORECASE)


def _is_noncommand_question(text: str) -> bool:
    """Interrogative/chat framing with NO scheduling command verb -> chat, not a create ("is it gonna rain
    tmr" has a time but isn't a request to schedule; "can u add X thurs" has 'add' so it stays a create)."""
    t = text or ""
    return bool(_INTERROGATIVE.search(t)) and not _COMMAND_VERB.search(t)


def _has_time_expression(text: str) -> bool:
    """A concrete date/time is present ("tmr at 3", "friday 6pm", "aug 20") — a strong create signal even
    when the deterministic verbs miss the named event. Chit-chat with no concrete time returns False."""
    import datetime as _dt

    from . import temporal
    try:
        # DEFAULT_TZ is America/Los_Angeles with a TODO beside it. This only asks "is a concrete time
        # PRESENT", which is zone-insensitive for every expression except one straddling local midnight
        # — where a Central student's "tonight at 11" could resolve against a Pacific clock two hours
        # behind. UTC is the honest neutral reference for a presence check; the ACTUAL resolution always
        # happens later against the user's real timezone.
        return temporal.resolve(text or "", now=_dt.datetime.now(_dt.timezone.utc)) is not None
    except Exception:
        return False


async def _stage0(user_id: UUID, text: str, *, has_attachments: bool, has_reply_ref: bool) -> RouterDecision | None:
    """High-confidence structural routing with no model call. None -> escalate to Stage 1."""
    from . import (calendar_mutation, decision_resolver, entity_resolution, handoff, mission_kernel,
                   world_state)
    t = text or ""

    # 1. resolving an OPEN decision -> continue that run
    if decision_resolver.resolve_approval(t) is not decision_resolver.Resolution.unrelated:
        try:
            pending = await mission_kernel.latest_pending_calendar_mission(user_id)
        except Exception:
            pending = None
        if pending is not None:
            return RouterDecision(
                ExecutionClass.direct_action, action=GoalAction.create, domain="calendar",
                decision_id=pending["mission_id"], candidate_capabilities=("calendar.create_event",))

    # 2. a mutation/correction on a CONCRETE entity -> direct action
    kind = calendar_mutation.classify(t)
    if kind is not None:
        try:
            res = await entity_resolution.resolve(user_id, t)
            referent = res.status in ("resolved", "ambiguous")
            if not referent and kind == "repair":
                referent = (await entity_resolution.resolve_most_recent(user_id)).status == "resolved"
        except Exception:
            referent = False
        if referent:
            action = {"delete": GoalAction.delete, "update": GoalAction.update, "repair": GoalAction.repair}[kind]
            corr = None
            return RouterDecision(
                ExecutionClass.direct_action, action=action, domain="calendar", target_reference=t,
                correction_of_run_id=corr, candidate_capabilities=(f"calendar.{kind}_event",))

    # 3. a world-state statement ("i'm in cst") -> remember
    if world_state.detect_user_timezone_statement(t):
        return RouterDecision(ExecutionClass.direct_action, action=GoalAction.remember, domain="world")

    # 3b. GENERIC send intent -> the send hand (Gmail). A plain send is a direct action; a send that also asks
    # to be told about the reply is a durable monitor -> background mission (boundary-7 shape). Provider-neutral
    # action; the broker resolves the concrete tool + capability truth. Placed before the handoff check so a
    # "email X and let me know when they reply" is a Gmail mission, not a bare unrouted plan.
    if _SEND_INTENT.search(t):
        ec = ExecutionClass.background_mission if _FOLLOWUP.search(t) else ExecutionClass.direct_action
        return RouterDecision(ec, action=GoalAction.send, domain="gmail", target_reference=t,
                              candidate_capabilities=("gmail.send_message",))

    # 5. explicit handoff / long-running -> durable background mission (before the temporal check so
    # "handle this by friday" is a mission, not a bare create)
    if handoff.has_explicit_handoff_language(t):
        return RouterDecision(ExecutionClass.background_mission, action=GoalAction.plan)

    # 4. scheduling a NEW event: explicit verbs, OR a concrete time expression (so "add dentist tmr at 3pm"
    # — a named event the deterministic verbs miss — still routes to the create path, not to chat).
    if handoff.has_scheduling_execution_intent(t) or (_has_time_expression(t) and not _is_noncommand_question(t)):
        return RouterDecision(ExecutionClass.direct_action, action=GoalAction.create, domain="calendar",
                              candidate_capabilities=("calendar.create_event",))

    # 6. a status question about an existing run -> fast answer from state
    if handoff.has_status_query_language(t):
        return RouterDecision(ExecutionClass.fast_conversation, action=GoalAction.answer)

    # 7. an attachment / explicit reply with no clear text intent -> needs perception (foreground)
    if has_attachments or has_reply_ref:
        return RouterDecision(ExecutionClass.foreground_agent, action=GoalAction.create, domain="calendar",
                              needs_deeper_planning=False)

    return None


_DEFAULT = RouterDecision(ExecutionClass.fast_conversation, action=GoalAction.answer, source="router_default")


def _shadow_enabled() -> bool:
    """Miss telemetry, on by default. Off is the escape hatch, not the norm — the whole point is that
    misses stop being invisible. Kill switch: BRUCE_ROUTER_SHADOW_OFF."""
    import os
    return os.environ.get("BRUCE_ROUTER_SHADOW_OFF", "").strip().lower() not in {"1", "true", "yes", "on"}


async def _stage1(user_id: UUID, text: str, *, has_reply_ref: bool, model=None) -> RouterDecision:
    """The COMPACT router model (Phase C), reached only when Stage 0 was inconclusive. `model` is an injected
    RouterModelProvider (a fake in tests); in production the CompactRouterModel is used when BRUCE_ROUTER_
    STAGE1 is on. Any timeout/error/invalid/low-confidence -> the deterministic default (never blocks)."""
    from . import router_model, semantic_triage

    # M2: when the semantic path is on, understanding and orchestration are separate layers
    # (SemanticRouterModel), and the confidence floor lives in the derivation rather than in the transport
    # policy — so the policy floor is zeroed rather than re-gating a deliberate clarifying question back
    # into the silent default.
    policy = None
    if model is not None:
        provider = model
    elif semantic_triage.enabled():
        provider, policy = router_model.SemanticRouterModel(), router_model.semantic_policy()
    elif router_model.stage1_enabled():
        provider = router_model.CompactRouterModel()
    else:
        provider = None
    if provider is None:
        return _DEFAULT
    try:
        request = await router_model.build_request(user_id, text, has_reply_ref=has_reply_ref)
        outcome = await router_model.route_stage1(request, provider=provider, policy=policy)
        if outcome.response is not None:
            return outcome.response.to_router_decision()
    except Exception:
        pass
    return _DEFAULT


async def route(user_id: UUID, text: str, *, has_attachments: bool = False, has_reply_ref: bool = False,
                model=None) -> tuple[RouterDecision, RouterTiming]:
    """Classify the execution path. Deterministic Stage 0 first; the compact Stage-1 model ONLY when Stage 0
    is inconclusive (so a resolved turn makes no model call)."""
    timing = RouterTiming()
    t0 = time.perf_counter()
    d = await _stage0(user_id, text, has_attachments=has_attachments, has_reply_ref=has_reply_ref)
    timing.stage0_ms = (time.perf_counter() - t0) * 1000.0
    timing.stage0_resolved = d is not None
    if d is not None:
        timing.total_ms = timing.stage0_ms
        return d, timing
    t1 = time.perf_counter()
    d = await _stage1(user_id, text, has_reply_ref=has_reply_ref, model=model)
    timing.stage1_ms = (time.perf_counter() - t1) * 1000.0
    timing.total_ms = timing.stage0_ms + timing.stage1_ms

    # M1B SHADOW: Stage 0 missed. Today that silently becomes fast_conversation, so a paraphrase the
    # router does not recognise is indistinguishable from a correct chat classification and the true
    # miss rate is unmeasurable. Record WHY the miss happened; change nothing about the decision.
    # Deliberately makes no model call — a shadow semantic call on every miss would add exactly the
    # latency that got Stage-1 gated off, on turns that are already falling through.
    if _shadow_enabled():
        try:
            from . import router_shadow
            if router_shadow.is_miss(d):
                await router_shadow.classify_miss(user_id, text)
        except Exception:
            pass                                    # observability must never break a turn
    return d, timing
