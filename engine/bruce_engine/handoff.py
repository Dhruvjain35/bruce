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

import re
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


# Explicit user DELEGATION to Bruce. Presence is REQUIRED before any mission-creating action is even
# considered; the model's needs_mission flag alone is insufficient. Semantic, not a fixed phrase list:
# delegation verb + an object variant (this / it / that / "ts" = this), tolerant of slang + filler
# ("bro", "pls", "rn", "gng", "for me"). Status-check phrases ("did that go through") are handled by the
# status-query path, never here. A DIFFERENT object ("that class", "this problem") without a delegation
# verb is not a handoff.
_OBJ = r"(?:this|that|it|ts|tis)"
_HANDOFF_PATTERNS = (
    rf"\bhandle\s+{_OBJ}\b",
    rf"\bdeal\s+with\s+{_OBJ}\b",
    rf"\btake\s+care\s+of\s+{_OBJ}\b",
    rf"\btake\s+{_OBJ}\s+(?:from\s+here|over)\b",
    r"\btake\s+over\s+(?:from\s+here|this|it)?\b",
    rf"\b(?:sort|figure)\s+(?:{_OBJ}\s+)?out\b",
    rf"\bget\s+{_OBJ}\s+(?:done|sorted|handled)\b",
    rf"\bmake\s+sure\s+{_OBJ}\b[^.?!]*\b(?:done|happens|gets|goes\s+through)\b",
    rf"\bmake\s+{_OBJ}\s+happen\b",
    rf"\b(?:follow|following)\s+up\s+(?:on\s+)?{_OBJ}\b",
    rf"\bkeep\s+(?:following\s+up|chasing|on\s+(?:top\s+of\s+)?{_OBJ}|at\s+{_OBJ})\b",
    rf"\bstay\s+on\s+(?:top\s+of\s+)?{_OBJ}\b",
    r"\bonly\s+(?:bother|bug|ping|hit|tell|text|loop|wake)\s+me\b",
    # durable handoffs with a NOUN object or a durability marker (til / from here / run point / own it)
    r"\bstay\s+on\s+(?:top\s+of\s+)?the\b",
    r"\brun\s+point\s+on\b",
    r"\brun\s+(?:w(?:ith)?\s+it|the\s+whole)\b",
    r"\btake\s+the\s+reins\b",
    r"\bkeep\s+(?:nudging|pinging|chasing|following\s+up|on\s+(?:top|it)|at\s+it)\b",
    r"\b(?:u|you|ur)\s+own\s+(?:the|this|it)\b|\bown\s+the\s+(?:whole\s+)?\w",
    r"\bdeal\s+w(?:ith)?\s+the\b",
    r"\bfrom\s+here\s+on\b|\bfrom\s+here\b(?=.*\b(?:handle|deal|run|own|stay|keep|take)\b)",
    r"\b(?:take|handle|run|own)\b[^.?!\n]*\b(?:til|until)\b",     # "keep nudging … til she picks"
    r"\bhandle\s+the\s+(?:whole\s+)?\w",
    r"\btake\s+ts\b",
)
_HANDOFF_RE = re.compile("|".join(_HANDOFF_PATTERNS), re.IGNORECASE)
# First person doing it themselves — "i'll handle it", "i got this", "how do i handle this equation",
# "should i deal with it" — the USER is acting, NOT delegating to Bruce. Suppress (a tutoring "how do i
# handle this" must never be read as a handoff). 2nd person ("can u handle this") is NOT suppressed.
_SELF_HANDLING_RE = re.compile(
    r"\b(?:(?:how\s+)?(?:do|should|can|could|would)\s+i|imma|"
    r"i(?:'?ll| will| can| ?a?m| got|'?ve\s+got| gonna| ll| m)?)\s+"
    r"(?:handle|deal|take|sort|figure|got|do)\b", re.IGNORECASE)

_MIN_MISSION_CONFIDENCE = 0.55


# --- Scheduling EXECUTION intent (calendar-specific authorizer) -----------------------------------
# The generic handoff verbs ("handle it") authorize a durable CAPTURE. Scheduling has its own explicit
# verbs a student actually uses — "schedule this", "put/add this on/to my calendar", "calendar this",
# "save these dates", "block this off" — that authorize a REAL calendar write. Deterministic, tolerant
# of slang/abbreviations ("ts"=this) and filler ("yo", "rq", "for me", "bro"). This is an AUTHORIZATION
# gate, so — like handoff language — it is never derived from a model flag, and it is suppressed when the
# user is asking ABOUT scheduling ("how do i schedule this?") or saying THEY will do it ("i'll schedule
# this myself"). The CalendarScheduleHandler additionally requires a real dated event + a connected
# calendar, so a bare "add this" only ever schedules when an event is genuinely present.
_S_OBJ = r"(?:this|that|it|ts|tis|these|those|the\s+dates?|these\s+dates?|those\s+dates?)"
_SCHED_PATTERNS = (
    rf"\bschedule\s+{_S_OBJ}\b",
    rf"\b(?:put|add|throw|pop|stick|slot|drop|chuck|get)\s+{_S_OBJ}\s+(?:on|in|to|onto|into)\s+(?:my\s+|the\s+)?cal(?:endar)?\b",
    rf"\b(?:add|save)\s+{_S_OBJ}\s+to\s+(?:my\s+|the\s+)?cal(?:endar)?\b",
    rf"\bcalendar\s+{_S_OBJ}\b",
    r"\bsave\s+(?:the|these|those)\s+dates?\b",
    r"\bsave\s+the\s+date\b",
    rf"\bblock\s+(?:{_S_OBJ}\s+)?off\b",
    rf"\bblock\s+off\s+{_S_OBJ}\b",
    rf"\b(?:add|save)\s+{_S_OBJ}\b",          # broad — safe: handler still requires a real dated event
)
_SCHED_RE = re.compile("|".join(_SCHED_PATTERNS), re.IGNORECASE)
# Questions / self-handling: "how do i schedule this", "should i schedule this", "what would i put on
# my calendar", "i'll schedule this myself". 2nd person ("can u schedule this") is NOT suppressed.
_SCHED_SUPPRESS = re.compile(
    r"\b(?:how|when|where|what|why|whether)\b[^.?!]*\b(?:do|should|would|can|could|will|shall)\s+i\b|"
    r"\b(?:should|would|could|shall|do|can)\s+i\b[^.?!]*\b(?:schedule|calendar|add|put|block|save)\b|"
    r"\bi\s?(?:'?ll| will| can| am|'?m| a?m| gonna| finna)\s+(?:schedule|add|put|calendar|block|save|do|handle)\b|"
    r"\bimma\s+(?:schedule|add|put|calendar|block|save)\b|"
    r"\b(?:nvm|never\s*mind|forget\s+it|don'?t|do\s+not|nah\s+don'?t)\b",   # negation cancels the intent
    re.IGNORECASE)


def has_scheduling_execution_intent(text: str | None) -> bool:
    """True iff the user's own words explicitly tell Bruce to put an event ON their calendar (a real
    write authorization). Deterministic; suppressed for questions-about-scheduling and self-handling."""
    t = (text or "").lower()
    if not t:
        return False
    if _SCHED_SUPPRESS.search(t):
        return False
    return bool(_SCHED_RE.search(t))


def has_explicit_handoff_language(text: str | None) -> bool:
    """True iff the user's own words explicitly DELEGATE to Bruce (take this on / handle it / follow up).
    Deterministic pattern match tolerant of slang, abbreviations ('ts'=this), and filler; suppressed when
    the user says THEY will handle it. No model, no inference — this is the authorization gate, so a
    hallucinated model flag can never substitute for it."""
    t = (text or "").lower()
    if not t:
        return False
    if _SELF_HANDLING_RE.search(t):        # "i'll handle this" -> the user is doing it, not Bruce
        return False
    return bool(_HANDOFF_RE.search(t))


# Language that asks about an EXISTING mission's state (a read, never a create). Disjoint from the handoff
# creation set on purpose: "did that go through?" reports status, it does not start a new mission.
_STATUS_QUERY = (
    "what are u doing with", "what are you doing with", "what are u doing", "what are you doing",
    "what's the status", "whats the status", "status update", "any update", "any updates",
    "how's that going", "hows that going", "how's it going with", "hows it going with",
    "where are we on", "where are we with", "what's happening with", "whats happening with",
    "did that go through", "did this go through", "did it go through", "did that actually go through",
    "what's up with that", "whats up with that", "hows that coming", "how's that coming",
)


def has_status_query_language(text: str | None) -> bool:
    """True iff the user is asking about the state of something Bruce is (or might be) handling."""
    t = (text or "").lower()
    return any(p in t for p in _STATUS_QUERY)


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
