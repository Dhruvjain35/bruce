"""Founder-alpha gating — two flags, an explicit allowlist, and a kill switch above both.

WHY NOT REUSE `BRUCE_ROUTER_SEMANTIC`. That flag means "the semantic router is authoritative for
routing", and it is off because the release gates are not met. Founder semantic rescue is a different
claim: the model may INTERPRET a turn Stage-0 could not, and may never authorize anything. Overloading
one flag to mean both would make "is semantic authority on?" unanswerable, and the first time someone
turned it on for the founder they would also be turning it on as routing authority for whoever came next.

DEFAULT OFF, ALLOWLIST, KILL SWITCH — three independent things, deliberately. A flag alone is one typo
away from applying to everyone; an allowlist alone cannot be turned off in a hurry; a kill switch alone
does not stop a flag from being enabled for the wrong account. Rescue requires all three to agree, and
`kill()` overrides both of the others because the moment you need it is the moment you cannot afford to
reason about precedence.

WHAT THESE FLAGS DO NOT DO. They create no enrollment and no entitlement. A founder with both flags on
and no live enrollment still cannot use Bruce, because `access_control` is a separate gate that this
module does not touch and cannot reach. That separation is what makes "the alpha is closed" checkable
independently of whatever the flags happen to say.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

log = logging.getLogger("bruce.founder_alpha")   # CONTENT-FREE: flags and user ids only

_ON = {"1", "true", "yes", "on"}

FOUNDER_ALPHA = "BRUCE_FOUNDER_ALPHA"
SEMANTIC_RESCUE = "BRUCE_SEMANTIC_RESCUE"
FOUNDER_USER_IDS = "BRUCE_FOUNDER_USER_IDS"      # comma-separated allowlist
KILL = "BRUCE_FOUNDER_ALPHA_KILL"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _ON


def killed() -> bool:
    """The override. Checked first everywhere, so a kill cannot be argued with by a flag."""
    return _flag(KILL)


def allowlisted(user_id: UUID | str | None) -> bool:
    """Is this exactly one of the named founder accounts?

    Parsed per call rather than cached: the allowlist changes by redeploying the environment variable,
    and a cached copy would mean a removed founder keeps access until the process restarts.
    """
    if user_id is None:
        return False
    raw = os.environ.get(FOUNDER_USER_IDS, "")
    allowed = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return str(user_id).strip().lower() in allowed


def alpha_enabled(user_id: UUID | str | None) -> bool:
    """Founder-alpha capabilities, for one allowlisted account."""
    if killed():
        return False
    return _flag(FOUNDER_ALPHA) and allowlisted(user_id)


def rescue_enabled(user_id: UUID | str | None) -> bool:
    """Semantic rescue, for one allowlisted account inside the alpha.

    Requires the alpha flag as well as its own: rescue is a capability OF the founder alpha, and being
    able to turn it on without the alpha would be a third state nobody reasoned about.
    """
    if killed():
        return False
    return _flag(SEMANTIC_RESCUE) and alpha_enabled(user_id)


def status(user_id: UUID | str | None) -> dict:
    """Everything a trace or an operator needs, in one call. No secrets, no message content."""
    return {
        "killed": killed(),
        "alpha_flag": _flag(FOUNDER_ALPHA),
        "rescue_flag": _flag(SEMANTIC_RESCUE),
        "allowlisted": allowlisted(user_id),
        "alpha_enabled": alpha_enabled(user_id),
        "rescue_enabled": rescue_enabled(user_id),
    }


# --- what the founder alpha will and will not attempt ------------------------------------------------
# The supported list is deliberately an ALLOWLIST of (provider, operation). An unsupported request must
# reach a stated limitation, never a silent chat reply — "Bruce didn't do it and didn't say why" is the
# failure this whole lane exists to remove, and a denylist would let a new provider through by default.

SUPPORTED_OPERATIONS: frozenset[str] = frozenset({
    "google_calendar.create_event",
    "google_calendar.update_event",
    "gmail.send_message",
    "gmail.create_draft",
    "gmail.find_reply",          # the read that backs reply monitoring
})

# Named so a blocked turn can say which one applies rather than a generic refusal.
BLOCKED_MIXED_INTENT = "mixed_intent_write"
BLOCKED_SEVERAL_GOALS = "several_consequential_goals"
BLOCKED_AMBIGUOUS_DESTRUCTIVE = "ambiguous_destructive_operation"
BLOCKED_UNRESOLVED_DELETION = "provider_deletion_without_exact_entity"
BLOCKED_NO_ADVANCER = "background_work_without_advancer"
BLOCKED_LOW_CONFIDENCE = "low_confidence_executable"
BLOCKED_UNSUPPORTED = "unsupported_provider_or_operation"

BLOCKED_REASONS = frozenset({
    BLOCKED_MIXED_INTENT, BLOCKED_SEVERAL_GOALS, BLOCKED_AMBIGUOUS_DESTRUCTIVE,
    BLOCKED_UNRESOLVED_DELETION, BLOCKED_NO_ADVANCER, BLOCKED_LOW_CONFIDENCE, BLOCKED_UNSUPPORTED,
})

# Below this a semantic executable reading is not trusted to become a PROPOSAL, let alone an action. It
# is higher than the router's own floor (0.55) on purpose: that one decides whether to ask a question,
# this one decides whether to put a concrete operation in front of the founder with a yes/no next to it.
MIN_PROPOSAL_CONFIDENCE = 0.70


def supported(provider: str | None, operation: str | None) -> bool:
    return f"{provider}.{operation}" in SUPPORTED_OPERATIONS


def blocked_reason(*, provider: str | None, operation: str | None, confidence: float,
                   goal_count: str | None, destructive_target_resolved: bool = True,
                   has_advancer: bool = True) -> str | None:
    """The one reason this turn cannot become a proposal, or None.

    Order matters: the most specific true reason wins, because the founder is going to read it. "several
    goals" is more useful than "unsupported" when both are true, and a bare "I can't do that" for a
    request Bruce could do if it were one sentence is the answer that makes someone stop trying.
    """
    if goal_count == "several":
        return BLOCKED_SEVERAL_GOALS
    if not supported(provider, operation):
        return BLOCKED_UNSUPPORTED
    from . import authorization_evidence as ae
    if ae.is_destructive(provider or "", operation or "") and not destructive_target_resolved:
        return BLOCKED_UNRESOLVED_DELETION
    if not has_advancer:
        return BLOCKED_NO_ADVANCER
    if confidence < MIN_PROPOSAL_CONFIDENCE:
        return BLOCKED_LOW_CONFIDENCE
    return None
