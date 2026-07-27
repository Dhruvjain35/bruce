"""Compact Stage-1 router model (Phase C) — the fast, structured, hard-timeout classifier the FastRouter
reaches ONLY when deterministic Stage 0 is inconclusive. Implements the frozen RouterModelProvider contract.

Rules baked in:
  * a FAST model (gpt-5.4-mini, text-only, tiny structured output) — never the strongest planner model;
  * a HARD deadline (BRUCE_ROUTING_TIMEOUT_S) with NO retry — on breach the router falls back to the
    deterministic default, never blocking a turn;
  * strict schema + enum validation — an invalid/over-broad/low-confidence response is NOT trusted and
    falls back deterministically (RouterModelPolicy.accepts);
  * the model sees a COMPACT request — the message, a one-line active-run summary, a pending decision id, a
    few entity titles, and the live capability FAMILIES — never raw history, images, or tool schemas;
  * telemetry is CONTENT-FREE — labels/latency only, NEVER the message text or entity content;
  * this is reached ONLY from Stage 1, so a deterministically-resolved turn makes NO model call.

Gated by BRUCE_ROUTER_STAGE1 — off => the router keeps its "unrouted == chat" deterministic default.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, Field
from pydantic_ai import Agent, PromptedOutput

from . import llm
from .router_model_contract import (ActiveRunSummary, EntityRef, RouterModelPolicy, RouterModelRequest,
                                    RouterModelResponse)
from .runtime_contracts import ExecutionClass, GoalAction

log = logging.getLogger("bruce.router_model")   # CONTENT-FREE: labels/latency only, never message text

_TRUTHY = {"1", "true", "yes", "on"}


def stage1_enabled() -> bool:
    return os.environ.get("BRUCE_ROUTER_STAGE1", "").strip().lower() in _TRUTHY


_SYSTEM = (
    "You are Bruce's FAST router. Classify ONE student message into an execution path. Output ONLY the "
    "structured fields — do NOT answer the message. Paths:\n"
    "- fast_conversation: chat, tutoring, a question, an ack — anything with no action to perform.\n"
    "- direct_action: ONE concrete operation on ONE thing (add/move/delete a calendar event; remember a "
    "fact like a timezone).\n"
    "- foreground_agent: ambiguous or multi-step work the student is actively waiting on.\n"
    "- background_mission: a follow-up / monitor / long-running handoff (\"keep on top of X til friday\").\n"
    "Set action (create/update/delete/remember/answer/plan/monitor/...), domain (calendar/... or null for "
    "chat), confidence 0..1, needs_deeper_planning, and any ambiguity tags. Only these capability FAMILIES "
    "are live for this student: {families}. Never route to a domain that is not live."
)


class _RouterOut(BaseModel):
    """The tiny structured output the model must produce — validated by pydantic before we trust it."""
    execution_class: str
    action: str | None = None
    domain: str | None = None
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    needs_deeper_planning: bool = False
    ambiguity: list[str] = Field(default_factory=list)


def _to_response(out: _RouterOut) -> RouterModelResponse:
    """Strict map to the frozen response — invalid enum values raise (-> deterministic fallback upstream)."""
    ec = ExecutionClass(out.execution_class)                          # raises ValueError on unknown class
    action = GoalAction(out.action) if out.action else None           # raises on unknown action
    return RouterModelResponse(
        execution_class=ec, action=action, domain=out.domain,
        confidence=float(out.confidence), ambiguity=tuple(out.ambiguity or ()),
        needs_deeper_planning=bool(out.needs_deeper_planning))


def _render_request(req: RouterModelRequest) -> str:
    """The compact prompt body. Content-bounded: message + optional reply summary + a one-line run summary +
    a few entity titles. No raw history, no tool schemas."""
    lines = [f"MESSAGE: {req.text}"]
    if req.has_reply_ref and req.reply_summary:
        lines.append(f"REPLYING TO: {req.reply_summary}")
    if req.active_run is not None:
        r = req.active_run
        lines.append(f"OPEN TASK: {r.domain} {r.status} — {r.goal_summary}"
                     + (" (awaiting your decision)" if r.awaiting_decision else ""))
    if req.pending_decision_id:
        lines.append("There is a pending yes/no decision the student may be answering.")
    if req.top_entities:
        lines.append("KNOWN ITEMS: " + "; ".join(
            f"{e.title}" + (f" ({e.when})" if e.when else "") for e in req.top_entities))
    return "\n".join(lines)


class CompactRouterModel:
    """The production Stage-1 provider (RouterModelProvider). One fast structured call, no vision, no tools."""
    provider = "openai"
    model = llm.MODEL_ROUTING

    async def classify(self, request: RouterModelRequest) -> RouterModelResponse:
        families = ", ".join(request.live_capability_families) or "none"
        agent = Agent(llm.routing_model(), output_type=PromptedOutput(_RouterOut),
                      system_prompt=_SYSTEM.format(families=families))
        result = await agent.run(_render_request(request))
        return _to_response(result.output)


class SemanticRouterModel:
    """M2 Stage-1 + Stage-3: read MEANING with a small fast model, then DERIVE the execution class in
    deterministic code.

    Same `RouterModelProvider` interface as `CompactRouterModel`, so the pipeline is unchanged and the two
    can be measured head to head on identical inputs. The difference is entirely in who decides what: the
    model is no longer asked whether something is a background mission, because it cannot see the leases,
    the connections, or the notification guarantees that make that a mission.

    The confidence floor lives in `execution_derivation.MIN_CONFIDENCE` — ONE floor, applied where the
    routing decision is actually made. A low-confidence read here becomes a clarifying question, which is
    a real routing outcome and must not be re-gated into the silent deterministic default; that is why
    `semantic_policy()` sets the transport-level floor to zero.
    """

    provider = "openai"
    model = llm.MODEL_ROUTING

    def __init__(self, triage_provider=None):
        self._triage = triage_provider

    async def classify(self, request: RouterModelRequest) -> RouterModelResponse:
        from . import execution_derivation, semantic_contracts, semantic_triage

        body = semantic_triage.render_request(
            request.text, live_families=live_capability_families(request),
            reply_summary=request.reply_summary,
            open_task=(f"{request.active_run.domain} — {request.active_run.goal_summary}"
                       if request.active_run else None),
            awaiting_decision=bool(request.pending_decision_id
                                   or (request.active_run and request.active_run.awaiting_decision)))
        turn = await semantic_triage.triage(body, provider=self._triage)
        if isinstance(turn, semantic_contracts.TriageFailure):
            # Explicit, not silent. A partial read (an unrecognised optional field) is still usable; a
            # timeout or transport loss is not, and raising sends the caller to the deterministic default
            # with a logged reason rather than a fabricated chat classification.
            if turn.partial is None:
                raise RuntimeError(f"triage_failed:{turn.reason}")
            turn = turn.partial

        ctx = await self._context(request)
        d = execution_derivation.derive(turn, ctx)
        return RouterModelResponse(
            execution_class=ExecutionClass(d.execution_class),
            action=GoalAction(d.action) if d.action else None,
            domain=d.domain, candidate_capabilities=d.capabilities,
            continuation_run_id=d.continuation_run_id, correction_of_run_id=d.correction_of_run_id,
            confidence=d.confidence, ambiguity=d.ambiguity,
            needs_deeper_planning=d.needs_clarification)

    async def _context(self, request: RouterModelRequest):
        """Runtime facts. Falls back to the request's own summary when there is no user to look up, so the
        harness can exercise derivation without a database."""
        from . import execution_derivation
        from .semantic_contracts import TurnContext

        if request.user_id:
            return await execution_derivation.build_context(UUID(request.user_id),
                                                            has_reply_ref=request.has_reply_ref)
        return TurnContext(
            live_families=frozenset(), live_capabilities=frozenset(),
            pending_decision_id=request.pending_decision_id,
            active_run_id=request.active_run.run_id if request.active_run else None,
            active_run_domain=request.active_run.domain if request.active_run else None,
            has_reply_ref=request.has_reply_ref)


def live_capability_families(request: RouterModelRequest) -> tuple[str, ...]:
    """Registry families ("calendar", "gmail") rendered in the semantic layer's provider-free vocabulary."""
    seen = {"gmail": "communication", "email": "communication", "calendar": "calendar"}
    return tuple(sorted({seen.get(f, f) for f in request.live_capability_families}))


def semantic_policy() -> RouterModelPolicy:
    """Transport-level policy for the semantic path. min_confidence=0 on purpose — see SemanticRouterModel."""
    from . import semantic_triage
    return RouterModelPolicy(timeout_s=semantic_triage.TRIAGE_TIMEOUT_S, min_confidence=0.0)


async def build_request(user_id: UUID, text: str, *, has_reply_ref: bool = False,
                        reply_summary: str | None = None) -> RouterModelRequest:
    """Assemble the COMPACT request from bounded DB reads (only runs when Stage 0 was inconclusive). Every
    read is best-effort — a store hiccup just omits that context, never blocks routing."""
    from . import agent_run_store, entity_store, mission_kernel, tool_registry

    active = None
    try:
        run = await agent_run_store.latest_active(user_id)
        if run:
            goal = run.get("goal") or {}
            what = goal.get("desired_outcome") or goal.get("title") or run.get("domain") or "a task"
            active = ActiveRunSummary(run_id=str(run.get("id")), domain=run.get("domain") or "",
                                      status=run.get("status") or "", goal_summary=str(what))
    except Exception:
        active = None

    pending = None
    try:
        p = await mission_kernel.latest_pending_calendar_mission(user_id)
        pending = str(p["mission_id"]) if p else None
    except Exception:
        pending = None

    entities: tuple[EntityRef, ...] = ()
    try:
        evs = await entity_store.active_events(user_id, limit=3)
        entities = tuple(EntityRef(entity_id=str(e.get("id")), title=str(e.get("title") or "event"))
                         for e in evs)
    except Exception:
        entities = ()

    families = tuple(sorted({s.capability.split(".")[0] for s in tool_registry.specs() if s.live}))

    return RouterModelRequest(text=text or "", has_reply_ref=has_reply_ref, reply_summary=reply_summary,
                              active_run=active, pending_decision_id=pending, top_entities=entities,
                              live_capability_families=families, user_id=str(user_id))


@dataclass(frozen=True)
class Stage1Outcome:
    response: RouterModelResponse | None      # None -> caller uses the deterministic fallback
    fell_back: bool
    reason: str                               # ok | timeout | error | low_confidence
    stage1_ms: float


async def route_stage1(request: RouterModelRequest, *, provider, policy: RouterModelPolicy | None = None,
                       timeout_s: float | None = None) -> Stage1Outcome:
    """Orchestrate ONE Stage-1 model call under a hard deadline + validation + confidence gate. Returns a
    trusted response, or None (=> deterministic fallback) on timeout / error / invalid / low-confidence.
    Content-free logging throughout."""
    policy = policy or RouterModelPolicy(timeout_s=timeout_s if timeout_s is not None else llm.ROUTING_TIMEOUT_S)
    deadline = timeout_s if timeout_s is not None else policy.timeout_s
    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(provider.classify(request), timeout=deadline)
    except asyncio.TimeoutError:
        ms = (time.perf_counter() - t0) * 1000.0
        log.info("stage1 fell_back=timeout ms=%.0f", ms)
        return Stage1Outcome(None, True, "timeout", ms)
    except Exception:
        ms = (time.perf_counter() - t0) * 1000.0
        log.info("stage1 fell_back=error ms=%.0f", ms)     # NO exception text (may echo the message)
        return Stage1Outcome(None, True, "error", ms)
    ms = (time.perf_counter() - t0) * 1000.0
    if resp is None or not policy.accepts(resp):
        log.info("stage1 fell_back=low_confidence conf=%.2f ms=%.0f",
                 resp.confidence if resp else 0.0, ms)
        return Stage1Outcome(None, True, "low_confidence", ms)
    log.info("stage1 ok ec=%s action=%s domain=%s conf=%.2f ms=%.0f", resp.execution_class.value,
             resp.action.value if resp.action else None, resp.domain, resp.confidence, ms)
    return Stage1Outcome(resp, False, "ok", ms)


# --- metrics harness (Phase C) — the router gates, runnable against a fake OR the real model ------

@dataclass(frozen=True)
class RouterEvalMetrics:
    n: int
    exec_class_accuracy: float       # over the non-fallback (graded) subset
    domain_accuracy: float
    fallback_rate: float
    timeout_rate: float
    p50_ms: float
    p95_ms: float
    ece: float                       # confidence calibration (Expected Calibration Error)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


async def evaluate_stage1(examples: list[dict], *, provider, policy: RouterModelPolicy | None = None,
                          families: tuple[str, ...] = ("calendar",)) -> RouterEvalMetrics:
    """Run the Stage-1 gate over labeled examples ({text, expected_class[, expected_domain]}) and report the
    router metrics: execution-class + domain accuracy (on the graded, non-fallback subset), fallback +
    timeout rates, p50/p95 latency, and confidence ECE. Provider is injected — a fake for the machinery
    test, the CompactRouterModel for a live (cost-bearing) eval."""
    from .router_model_contract import CalibrationPoint, expected_calibration_error

    ec_correct = dom_correct = dom_graded = fell = timed = 0
    lat: list[float] = []
    calib: list[CalibrationPoint] = []
    for ex in examples:
        req = RouterModelRequest(text=ex["text"], live_capability_families=families)
        out = await route_stage1(req, provider=provider, policy=policy)
        lat.append(out.stage1_ms)
        if out.fell_back:
            fell += 1
            timed += (out.reason == "timeout")
            continue
        r = out.response
        ok = (r.execution_class.value == ex["expected_class"])
        ec_correct += ok
        calib.append(CalibrationPoint(r.confidence, ok))
        if "expected_domain" in ex:
            dom_graded += 1
            dom_correct += (r.domain == ex["expected_domain"])
    n = len(examples)
    graded = n - fell
    return RouterEvalMetrics(
        n=n, exec_class_accuracy=(ec_correct / graded if graded else 0.0),
        domain_accuracy=(dom_correct / dom_graded if dom_graded else 0.0),
        fallback_rate=(fell / n if n else 0.0), timeout_rate=(timed / n if n else 0.0),
        p50_ms=_pct(lat, 50), p95_ms=_pct(lat, 95), ece=expected_calibration_error(calib))
