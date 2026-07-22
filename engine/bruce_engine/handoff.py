"""HandoffDecision authority (D-INT-3, integration invariant 2).

The conversation model may PROPOSE a handoff (needs_mission / proposed_goal / a suggested capability),
but it may NEVER directly authorize durable mission creation. This module is the DETERMINISTIC policy
that decides — from explicit user language, existing mission state, the capability registry, integration
availability, autonomy level, risk, reversibility, standing protocols, and confidence — what action a
turn warrants. A hallucinated ``needs_mission`` flag with no explicit user handoff language always
resolves to ``answer_only`` and NEVER sets ``authorizes_mutation``.

D-INT-3 scope: this module + its telemetry-only handler create NO state. ``authorizes_mutation`` is the
frozen contract the mission kernel (workstream A) will later consume to actually create a Mission; until
then nothing acts on it. That separation is what makes "the model can't create state" a provable property.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class HandoffAction(str, Enum):
    """The 8 dispositions a turn can warrant. Only the two mutating actions can carry authority, and
    only via this deterministic policy — never from a model flag."""
    answer_only = "answer_only"
    remember_context = "remember_context"
    propose_mission = "propose_mission"
    create_mission_under_protocol = "create_mission_under_protocol"
    request_decision = "request_decision"
    execute_approved_action = "execute_approved_action"
    report_verified_result = "report_verified_result"
    unsupported = "unsupported"


# Actions that would CREATE or MUTATE durable state. Reaching one requires the deterministic gates below;
# the mission kernel will refuse to create anything unless HandoffDecision.authorizes_mutation is True.
MUTATING_ACTIONS = frozenset({
    HandoffAction.create_mission_under_protocol,
    HandoffAction.execute_approved_action,
})


@dataclass(frozen=True)
class HandoffInputs:
    """Everything the policy is allowed to consider. Model signals are clearly labelled as PROPOSALS."""
    user_text: str | None
    model_needs_mission: bool = False           # the model PROPOSES — never authoritative on its own
    model_proposed_goal: str | None = None
    model_suggested_capability: str | None = None
    existing_mission_active: bool = False
    capability_supported: bool = False          # is a real capability wired for this goal?
    integration_available: bool = False
    autonomy_level: str = "A0"
    risk: str = "low"                           # low | medium | high | critical
    reversible: bool = True
    has_matching_protocol: bool = False         # a standing, user-authored protocol that pre-authorizes
    confidence: float = 0.0


@dataclass(frozen=True)
class HandoffDecision:
    action: HandoffAction
    reason: str                                 # privacy-safe category (NO user content)
    requires_approval: bool = False
    authorizes_mutation: bool = False           # HARD: True only via this policy, NEVER from a model flag
    confidence: float = 0.0

    def telemetry(self) -> dict:
        """Privacy-safe telemetry only — action/reason/flags, never any user content."""
        return {"action": self.action.value, "reason": self.reason,
                "requires_approval": self.requires_approval,
                "authorizes_mutation": self.authorizes_mutation}


# Explicit user language that STRONGLY indicates a handoff (DELEGATION). Presence of one of these is
# REQUIRED before any mission-creating action is even considered; the model's needs_mission flag alone is
# insufficient. Status-check phrases ("did that go through") are deliberately EXCLUDED — those ask about an
# existing mission's state (a read), never create one; the status-query path handles them.
_EXPLICIT_HANDOFF = (
    "take this from here", "take it from here", "take this over", "take over from here",
    "handle this", "handle it", "deal with this", "deal with it", "take care of this",
    "sort this out", "get this done", "get this sorted", "make it happen",
    "keep following up", "follow up on this", "keep chasing", "stay on top of this",
    "make sure this gets done", "make sure it gets done", "make sure this happens",
    "only bother me when", "only bug me when", "only ping me when", "only tell me when",
)

_MIN_MISSION_CONFIDENCE = 0.55


def has_explicit_handoff_language(text: str | None) -> bool:
    """True iff the user's own words explicitly ask Bruce to take something on / follow up / confirm it
    went through. Deterministic substring match on a fixed phrase set — no model, no inference."""
    t = (text or "").lower()
    return any(p in t for p in _EXPLICIT_HANDOFF)


def decide_handoff(inp: HandoffInputs) -> HandoffDecision:
    """The deterministic authority. Order matters — each gate is a hard precondition for the next:

      1. No explicit user handoff language  -> the model's needs_mission is ADVISORY ONLY. At most
         remember_context; NEVER a mutating action. (This is the anti-hallucination gate.)
      2. Explicit handoff, capability not wired -> honest unsupported.
      3. Explicit + supported, but high/critical risk OR irreversible OR low confidence -> request_decision
         (approval required before anything durable).
      4. Explicit + supported + acceptable risk: the explicit handoff IS the authorization to CREATE a
         durable mission (authorizes_mutation=True). Creating the mission record takes NO external action;
         it lands in a proposed/understanding state. A named protocol governs later auto-EXECUTION, not
         creation, so it changes only the reason here, not the authority.

    authorizes_mutation is set ONLY when the USER'S OWN words explicitly hand off to a supported, acceptable
    capability. It is NEVER set from a model flag; propose_mission (a soft offer that creates nothing) and
    the no-explicit / unsupported / needs-approval paths all leave it False.
    """
    explicit = has_explicit_handoff_language(inp.user_text)

    if not explicit:
        action = (HandoffAction.remember_context
                  if (inp.model_needs_mission and inp.model_proposed_goal)
                  else HandoffAction.answer_only)
        return HandoffDecision(action=action, reason="no_explicit_user_handoff", confidence=inp.confidence)

    if not inp.capability_supported:
        return HandoffDecision(action=HandoffAction.unsupported,
                               reason="explicit_handoff_capability_unsupported", confidence=inp.confidence)

    if inp.risk in ("high", "critical") or not inp.reversible or inp.confidence < _MIN_MISSION_CONFIDENCE:
        return HandoffDecision(action=HandoffAction.request_decision, requires_approval=True,
                               reason="explicit_handoff_needs_approval", confidence=inp.confidence)

    return HandoffDecision(
        action=HandoffAction.create_mission_under_protocol, authorizes_mutation=True,
        reason=("explicit_handoff_under_protocol" if inp.has_matching_protocol
                else "explicit_handoff_supported"),
        confidence=inp.confidence)
