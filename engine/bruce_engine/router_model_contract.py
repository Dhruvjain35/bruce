"""Stage-1 router-model contract (Phase C prep) — the provider-neutral interface the compact router model
implements, frozen ahead of the implementation so Phase C plugs in without a redesign.

There is NO model call here. This module is only: the request/response schemas the compact model sees and
returns, the provider abstraction, the timeout + fallback policy, a CONTENT-FREE telemetry record, and a
confidence-calibration harness. Phase C (the compact router model) and Phase A (which reaches Stage 1 when
deterministic routing is inconclusive) both import these read-only.

Design rules baked into the shapes:
  * the model sees a COMPACT, content-bounded request — no raw history, no images, no tool UNIVERSE (only
    the capability FAMILIES live for this user);
  * its output is structured + confidence-scored, validated and confidence-gated before it is trusted;
  * on timeout / error / invalid schema / low confidence the router falls back to the deterministic default,
    never blocking a turn;
  * telemetry is ids/labels/latency only — never user text.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from .runtime_contracts import ExecutionClass, GoalAction, RouterDecision


# --- request: the compact context the model is allowed to see ------------------------------------

@dataclass(frozen=True)
class ActiveRunSummary:
    """A one-line summary of the user's open AgentRun — never the full run, never chat history."""
    run_id: str
    domain: str
    status: str
    goal_summary: str
    awaiting_decision: bool = False


@dataclass(frozen=True)
class EntityRef:
    entity_id: str
    title: str
    when: str | None = None            # compact human time, e.g. "Jul 25 3pm"


@dataclass(frozen=True)
class RouterModelRequest:
    """Everything the Stage-1 model sees — compact and content-bounded on purpose."""
    text: str
    has_reply_ref: bool = False
    reply_summary: str | None = None
    active_run: ActiveRunSummary | None = None
    pending_decision_id: str | None = None
    top_entities: tuple[EntityRef, ...] = ()
    live_capability_families: tuple[str, ...] = ()   # ("calendar", "email") — families, not tools


# --- response: the structured routing decision ---------------------------------------------------

@dataclass(frozen=True)
class RouterModelResponse:
    execution_class: ExecutionClass
    action: GoalAction | None = None
    domain: str | None = None
    target_entity_ids: tuple[str, ...] = ()
    continuation_run_id: str | None = None
    correction_of_run_id: str | None = None
    candidate_capabilities: tuple[str, ...] = ()
    confidence: float = 0.5
    ambiguity: tuple[str, ...] = ()
    needs_deeper_planning: bool = False

    def to_router_decision(self) -> RouterDecision:
        """Lift the model output into the frozen RouterDecision the pipeline consumes (source=router_model)."""
        return RouterDecision(
            execution_class=self.execution_class, action=self.action, domain=self.domain,
            candidate_capabilities=self.candidate_capabilities,
            continuation_run_id=self.continuation_run_id, correction_of_run_id=self.correction_of_run_id,
            confidence=self.confidence, ambiguity=self.ambiguity,
            needs_deeper_planning=self.needs_deeper_planning, source="router_model")


# --- provider abstraction + timeout/fallback policy ----------------------------------------------

class RouterModelProvider(Protocol):
    """The compact model behind Stage 1 (deliberately NOT the strongest model). Any provider implements
    this; the router never depends on a concrete one."""
    async def classify(self, request: RouterModelRequest) -> RouterModelResponse: ...


@dataclass(frozen=True)
class RouterModelPolicy:
    """Timeout + fallback contract. On breach of any of these the router uses the deterministic default."""
    timeout_s: float = 1.5
    min_confidence: float = 0.55       # below this -> not trusted -> deterministic fallback
    max_candidates: int = 4

    def accepts(self, response: RouterModelResponse) -> bool:
        """Schema/confidence gate: a trusted response is confident enough and not over-broad."""
        return (response.confidence >= self.min_confidence
                and len(response.candidate_capabilities) <= self.max_candidates)


# --- content-free telemetry ----------------------------------------------------------------------

@dataclass(frozen=True)
class RouterTelemetry:
    """CONTENT-FREE routing telemetry: ids/labels/latency only, NEVER user text or entity content."""
    decision_id: str
    execution_class: str
    action: str | None
    domain: str | None
    confidence: float
    source: str                        # deterministic | router_model | fallback
    stage1_used: bool
    stage1_ms: float
    fell_back: bool
    ambiguity: tuple[str, ...] = ()


# --- confidence calibration harness --------------------------------------------------------------

@dataclass(frozen=True)
class CalibrationPoint:
    predicted_confidence: float
    was_correct: bool


def expected_calibration_error(points: list[CalibrationPoint], *, bins: int = 10) -> float:
    """Expected Calibration Error: the mean gap between predicted confidence and actual accuracy across
    confidence bins, weighted by bin population. 0 == perfectly calibrated. Phase C gates the router model
    on this over a labeled set so 'confidence 0.9' actually means ~90% correct."""
    if not points:
        return 0.0
    n = len(points)
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        bucket = [p for p in points if (lo <= p.predicted_confidence < hi) or (b == bins - 1 and p.predicted_confidence == 1.0)]
        if not bucket:
            continue
        acc = sum(1 for p in bucket if p.was_correct) / len(bucket)
        conf = sum(p.predicted_confidence for p in bucket) / len(bucket)
        ece += (len(bucket) / n) * math.fabs(acc - conf)
    return ece
