"""Router authority (G0 Activation, Phase A) — let the deterministic FastRouter OWN dispatch for the lanes
where it is provably authoritative, instead of shadow-logging while the vision reasoner runs every turn.

The reasoner is the single expensive step per turn (a multimodal model call). For a DETERMINISTIC text-action
lane the router already resolved — a calendar mutation on a concrete entity, a world-state statement, an
approval of a pending offer — the WINNING handler re-derives everything from msg.text + DB and ignores the
reasoner's decision entirely (4 of 7 handlers never read it), AND the router's Stage-0 rule already confirmed
the DB precondition (the entity resolved, the mission is pending). So on those lanes the reasoner's output is
unused: skip it, dispatch on a synthetic decision, get the identical outcome with no model on the hot path.

Everything else DEFERS to the reasoner and is never dropped: chat (the model generates the reply), a fresh
schedule / event candidate (needs perception-extracted entities), anything with an attachment or explicit
reply target, or a low-confidence route.

Rollout is a deterministic per-user canary (BRUCE_ROUTER_AUTHORITY_PCT, 0-100) with a hard-off kill switch,
so authority ramps and reverts without touching the pipeline.
"""

from __future__ import annotations

import hashlib
import os
from uuid import UUID

from .conversation_contract import ConversationDecision, IntentKind, ResponseType, RiskLevel
from .runtime_contracts import ExecutionClass, GoalAction, RouterDecision

_KILL_ON = {"1", "true", "yes", "on"}   # BRUCE_ROUTER_AUTHORITY_OFF=<truthy> engages the kill switch

# (execution_class, action) lanes where the router is AUTHORITATIVE and the reasoner is skippable: the winning
# handler re-derives from msg.text + DB, and the router's Stage-0 rule already verified the DB precondition.
# NB: fast_conversation is NOT here — the model GENERATES the chat reply; status_query is NOT here — it depends
# on a mission existing, which the router's language rule does not verify.
_SKIPPABLE = {
    (ExecutionClass.direct_action, GoalAction.update),
    (ExecutionClass.direct_action, GoalAction.delete),
    (ExecutionClass.direct_action, GoalAction.repair),
    (ExecutionClass.direct_action, GoalAction.remember),
}
_MIN_CONFIDENCE = 0.75

# synthetic intent/response per skippable action — persisted + logged, but the winning handler ignores it and
# emits its own fact-locked reply, so these are provenance labels, not content.
_SYNTH = {
    GoalAction.update: (IntentKind.status_cancel_correction, ResponseType.status),
    GoalAction.delete: (IntentKind.status_cancel_correction, ResponseType.status),
    GoalAction.repair: (IntentKind.status_cancel_correction, ResponseType.status),
    GoalAction.remember: (IntentKind.status_cancel_correction, ResponseType.status),
    GoalAction.create: (IntentKind.approval, ResponseType.mission_ack),   # approval continuation
}


def authority_pct() -> int:
    """Rollout percentage [0,100]; a hard-off kill switch (BRUCE_ROUTER_AUTHORITY_OFF) forces 0 (full legacy)."""
    if os.environ.get("BRUCE_ROUTER_AUTHORITY_OFF", "").strip().lower() in _KILL_ON:
        return 0
    try:
        return max(0, min(100, int(os.environ.get("BRUCE_ROUTER_AUTHORITY_PCT", "0"))))
    except ValueError:
        return 0


def _bucket(user_id: UUID) -> int:
    """Deterministic 0-99 bucket for a user (stable across turns; no randomness)."""
    return int(hashlib.sha256(str(user_id).encode()).hexdigest()[:8], 16) % 100


def is_authoritative(user_id: UUID) -> bool:
    """Is this user in the authority canary right now?"""
    return _bucket(user_id) < authority_pct()


def reasoner_skippable(decision: RouterDecision, *, has_attachments: bool, has_reply_ref: bool) -> bool:
    """Is this a lane the router owns and the reasoner is safe to skip? Requires a DETERMINISTIC, confident
    decision, no perception input, and a skippable (class, action) — including an approval CONTINUATION
    (create + decision_id, whose handler rebuilds from the pending mission, not from reasoner entities)."""
    if has_attachments or has_reply_ref:
        return False
    if decision.source != "deterministic" or decision.confidence < _MIN_CONFIDENCE:
        return False
    if (decision.execution_class, decision.action) in _SKIPPABLE:
        return True
    return (decision.execution_class == ExecutionClass.direct_action
            and decision.action == GoalAction.create and bool(decision.decision_id))


def synthetic_decision(decision: RouterDecision) -> ConversationDecision:
    """A minimal, model_dump-able decision for a skippable lane. The winning handler ignores it and produces
    its own fact-locked reply; this only carries a plausible intent/response label for persistence + logs."""
    intent, response_type = _SYNTH.get(
        decision.action, (IntentKind.status_cancel_correction, ResponseType.status))
    # user_visible_response is IGNORED by the winning deterministic handler (it emits its own fact-locked
    # copy). It only surfaces in the rare race where no handler claims (e.g. the entity was deleted between
    # the router check and dispatch) -> a benign non-claim, never an empty bubble.
    return ConversationDecision(intent=intent, response_type=response_type,
                               user_visible_response="one sec, let me take another look at that.",
                               risk_level=RiskLevel.none, confidence=1.0)
