"""Semantic rescue — the model may understand a turn Stage-0 missed. It may not authorize anything.

THE PROBLEM THIS SOLVES. Stage-0 is deterministic and therefore narrow: it matches what it was built to
match and returns UNKNOWN for everything else. Today an UNKNOWN turn falls through to generic
conversation, so a real request phrased in a way nobody anticipated gets a friendly reply and no work —
and the student cannot tell the difference between "Bruce decided not to" and "Bruce didn't understand".
That is the single most common way this system is unhelpful.

THE PROBLEM THIS MUST NOT CREATE. The obvious fix is to let the semantic model route the turn, which
hands execution authority to a probabilistic reader. Measured on the 334-scenario open set, that reader
produced false actions at 0.061 after the prompt work was exhausted. A boundary built from its output
inherits that rate exactly.

SO THE SPLIT IS ABSOLUTE, AND IT IS VISIBLE IN THE TYPES. `SemanticRescueResult` has no field that can
carry authorization, a receipt, a completion state, or permission to skip confirmation — not because
those are validated away, but because there is nowhere to put them. Everything consequential is derived
by deterministic code from that reading plus runtime state: the concrete provider, the concrete
operation, the exact arguments, the fingerprint, and whether the founder alpha supports it at all.

    the model understands  ->  the user authorizes  ->  the runtime executes  ->  the provider verifies

WHAT REACHES A PROVIDER, AND WHEN. Nothing, until a trusted confirmation resolves the exact pending
Decision this module created, bound to the exact arguments fingerprint it recorded. The chain after that
is the one already deployed and measured: UserActionBoundary, AuthorizationEvidence, policy,
CapabilitySnapshot, MutationGateway, fetch-back, receipt, ResponseAuthority. This module adds a gate in
front of it and changes nothing behind it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID, uuid4

log = logging.getLogger("bruce.semantic_rescue")   # CONTENT-FREE: ids, roles, verdicts

# How long a proposal stays answerable. Long enough to walk away from a phone mid-conversation, short
# enough that "yes" an hour later cannot execute something the founder has forgotten proposing.
PROPOSAL_TTL = timedelta(minutes=30)


class RescueOutcome(str, Enum):
    """What the runtime should DO with a rescued turn. Derived, never emitted by the model."""

    converse = "converse"                # answer naturally, create nothing
    clarify = "clarify"                  # one useful question
    resolve_against_state = "resolve_against_state"   # correction or continuation
    propose = "propose"                  # an executable goal -> pending Decision + confirmation
    blocked = "blocked"                  # supported-scope refusal, with a stated reason
    decline = "decline"                  # the boundary said no; nothing happens


@dataclass(frozen=True)
class SemanticRescueResult:
    """What the model is allowed to say about a turn.

    Note what is absent and cannot be added without changing this class: there is no `authorized`, no
    `authorization_id`, no `receipt`, no `completed`, no `skip_confirmation`. A model that wanted to
    grant itself permission has no field to express it in, which is a stronger guarantee than a check
    that could be forgotten.
    """

    turn_role: str
    actionability: str
    desired_outcome: str = ""
    domain_candidates: tuple[str, ...] = ()
    operation_family: str = "none"
    target_entities: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    correction_target: str | None = None
    continuation_target: str | None = None
    temporal_intent: str | None = None
    constraints: dict = field(default_factory=dict)
    missing_information: tuple[str, ...] = ()
    confidence: float = 0.0
    uncertainty: tuple[str, ...] = ()
    needs_clarification: bool = False
    proposed_goal: str = ""


@dataclass(frozen=True)
class ProposedOperation:
    """The deterministic half: a concrete operation, bound to exact arguments.

    `fingerprint` is computed with `authorization_evidence.fingerprint` over
    `authorization_evidence.normalize_arguments`, deliberately the same functions the execution gate
    uses. Two different fingerprint implementations would eventually disagree, and the disagreement
    would surface as a confirmed proposal that refuses to execute — or worse, one that executes
    something slightly different from what was shown.
    """

    goal_id: str
    provider: str
    operation: str
    arguments: dict
    fingerprint: str
    destructive: bool
    summary: str                       # what to show the founder, in structured terms


@dataclass(frozen=True)
class RescueDecision:
    """The whole verdict for one rescued turn: what to do, and what (if anything) is being proposed."""

    outcome: RescueOutcome
    result: SemanticRescueResult | None = None
    proposal: ProposedOperation | None = None
    blocked_reason: str | None = None
    clarification: str | None = None
    trace: dict = field(default_factory=dict)


def goal_fingerprint(provider: str, operation: str, arguments: dict) -> str:
    """One fingerprint function for the whole system. See `ProposedOperation`."""
    from . import authorization_evidence as ae
    return ae.fingerprint(ae.normalize_arguments(provider, operation, arguments))


def derive(result: SemanticRescueResult, *, user_id: UUID, arguments: dict | None = None,
           provider: str | None = None, operation: str | None = None,
           destructive_target_resolved: bool = True, has_advancer: bool = True,
           boundary_blocks: bool = False) -> RescueDecision:
    """SemanticRescueResult + runtime facts -> what to do. Pure, total, and no I/O.

    The order is the safety argument. The deterministic refusal boundary is consulted FIRST, ahead of
    anything the model concluded, because a turn where the founder said don't must not become a proposal
    with a yes/no next to it — presenting one is itself a small failure, and it is the step before a
    large one.
    """
    from . import founder_alpha

    if boundary_blocks:
        return RescueDecision(RescueOutcome.decline, result,
                              trace={"reason": "user_action_boundary_blocks"})

    role, act = result.turn_role, result.actionability

    if role in ("correction", "continuation"):
        return RescueDecision(RescueOutcome.resolve_against_state, result)

    if result.needs_clarification or act == "ambiguous":
        return RescueDecision(RescueOutcome.clarify, result,
                              clarification=_one_question(result))

    if act in ("no_action", "information_only") or role == "conversation":
        # A conversation is a conversation. This branch is why rescue cannot manufacture work: the model
        # saying "executable" is necessary for a proposal, never sufficient on its own, and casual chat
        # that the model over-reads still has to pass everything below.
        return RescueDecision(RescueOutcome.converse, result)

    if act not in ("executable", "durable_monitoring"):
        return RescueDecision(RescueOutcome.converse, result)

    reason = founder_alpha.blocked_reason(
        provider=provider, operation=operation, confidence=result.confidence,
        goal_count=("several" if len(result.target_entities) > 1 and result.constraints.get("multi_goal")
                    else "one"),
        destructive_target_resolved=destructive_target_resolved, has_advancer=has_advancer)
    if reason is not None:
        return RescueDecision(RescueOutcome.blocked, result, blocked_reason=reason)

    if result.missing_information:
        # Missing information is a QUESTION, not a proposal with blanks in it. A confirmation shown over
        # arguments Bruce guessed is a confirmation of the guess.
        return RescueDecision(RescueOutcome.clarify, result, clarification=_one_question(result))

    args = dict(arguments or {})
    proposal = ProposedOperation(
        goal_id=str(uuid4()), provider=provider or "", operation=operation or "", arguments=args,
        fingerprint=goal_fingerprint(provider or "", operation or "", args),
        destructive=_is_destructive(provider, operation),
        summary=_summary(provider, operation, args))
    return RescueDecision(RescueOutcome.propose, result, proposal=proposal)


def _is_destructive(provider: str | None, operation: str | None) -> bool:
    from . import authorization_evidence as ae
    return ae.is_destructive(provider or "", operation or "")


def _one_question(result: SemanticRescueResult) -> str:
    """The single thing genuinely missing, named. Not a sentence — the wording is the response layer's
    job, and mapping operations to fixed phrasings is what produces a system that sounds like a form."""
    if result.missing_information:
        return result.missing_information[0]
    if result.uncertainty:
        return result.uncertainty[0]
    return "which one"


def _summary(provider: str | None, operation: str | None, arguments: dict) -> str:
    """Structured, not conversational. `response_render` turns this into words; putting the sentence
    here would make every proposal sound identical and would put copy in the safety layer."""
    keys = ", ".join(f"{k}={v}" for k, v in sorted(arguments.items()) if v not in (None, ""))
    return f"{provider}.{operation}({keys})"


# --- the pending Decision ------------------------------------------------------------------------------

@dataclass(frozen=True)
class PendingProposal:
    """One canonical pending Decision, bound to one operation and one fingerprint.

    `expires_at` is not decoration. Without it a proposal made this morning is answerable this evening
    by a "yeah" that was about something else entirely — which is the shape of accident that is hardest
    to explain afterwards.
    """

    decision_id: str
    user_id: UUID
    conversation_id: str | None
    source_message_id: str | None
    goal_id: str
    provider: str
    operation: str
    normalized_arguments: dict
    arguments_fingerprint: str
    trusted_authorization_required: bool
    created_at: datetime
    expires_at: datetime
    status: str = "pending"
    presentation: str = ""

    def live(self, *, now: datetime) -> bool:
        return self.status == "pending" and self.expires_at > now


def build_pending(proposal: ProposedOperation, *, user_id: UUID, conversation_id: str | None,
                  source_message_id: str | None, presentation: str = "",
                  now: datetime | None = None) -> PendingProposal:
    from . import authorization_evidence as ae
    now = now or datetime.now(timezone.utc)
    normalized = ae.normalize_arguments(proposal.provider, proposal.operation, proposal.arguments)
    return PendingProposal(
        decision_id=str(uuid4()), user_id=user_id, conversation_id=conversation_id,
        source_message_id=source_message_id, goal_id=proposal.goal_id, provider=proposal.provider,
        operation=proposal.operation, normalized_arguments=normalized,
        arguments_fingerprint=proposal.fingerprint,
        # Always true in the founder alpha. Present as a field rather than assumed so that the day a
        # standing policy makes something automatic, the change is visible in the record instead of
        # being an absence nobody notices.
        trusted_authorization_required=True,
        created_at=now, expires_at=now + PROPOSAL_TTL, presentation=presentation)


# --- confirmation ---------------------------------------------------------------------------------------

APPROVED = "approved"
REJECTED = "rejected"
AMBIGUOUS = "ambiguous"
EXPIRED = "expired"
FINGERPRINT_CHANGED = "fingerprint_changed"
WRONG_DECISION = "wrong_decision"
NOT_TRUSTED = "not_trusted"


def resolve_confirmation(pending: PendingProposal | None, *, user_id: UUID, text: str | None,
                         conversation_id: str | None = None, current_fingerprint: str | None = None,
                         untrusted_content: str | None = None,
                         now: datetime | None = None) -> str:
    """Does this turn approve THAT proposal? Pure, and deliberately hard to satisfy.

    Every failure mode here has already been seen in this codebase: a quoted "yes add it" authorizing a
    write, a decline under a pending Decision executing the proposal because the branch read the turn
    ROLE and not the polarity, and an approval spent on arguments that had changed since it was shown.
    So this checks identity, then trust, then liveness, then the fingerprint, and only then polarity.
    """
    from . import user_action_boundary as uab

    now = now or datetime.now(timezone.utc)
    if pending is None:
        return WRONG_DECISION
    if pending.user_id != user_id:
        log.warning("rescue_confirmation_cross_user decision=%s", pending.decision_id)
        return WRONG_DECISION
    if conversation_id is not None and pending.conversation_id is not None \
            and pending.conversation_id != conversation_id:
        return WRONG_DECISION
    if not pending.live(now=now):
        return EXPIRED
    if current_fingerprint is not None and current_fingerprint != pending.arguments_fingerprint:
        # The world moved between the proposal and the answer. "Yes" meant the thing that was shown.
        return FINGERPRINT_CHANGED

    boundary = uab.evaluate(text, has_pending_decision=True)
    if boundary.authorizes() and _affirmative_came_from(untrusted_content, boundary):
        # MEASURED HERE, not theorised. `[screenshot] Coach: yes ur good to add it` approved a proposal
        # in this suite's first run, because `user_action_boundary` strips quoted REGIONS and an OCR
        # prefix is not a quote marker. The structural answer is the one #120 landed for memory: pass
        # untrusted material SEPARATELY and refuse an affirmative that is attributable to it, rather than
        # teaching a matcher one more marker to recognise.
        log.warning("rescue_confirmation_from_untrusted decision=%s", pending.decision_id)
        return AMBIGUOUS
    if boundary.polarity in (uab.Polarity.negative, uab.Polarity.withdrawal,
                             uab.Polarity.cancellation):
        return REJECTED
    if boundary.polarity is uab.Polarity.ambiguous:
        return AMBIGUOUS
    if boundary.authorizes():
        return APPROVED
    # `unrelated` lands here: silence about a pending question is not consent, and never becomes one by
    # the founder talking about something else.
    return AMBIGUOUS


def _affirmative_came_from(untrusted: str | None, boundary) -> bool:
    """Is the yes we found actually someone else's words?

    Compares the affirmative span the boundary matched against the untrusted material. Not a search for
    markers — the caller states what is untrusted, and this checks whether the approval lives there.
    """
    if not untrusted:
        return False
    from . import decision_resolver
    span = decision_resolver.normalize(getattr(boundary, "affirmative_span", "") or "")
    if not span:
        return False
    return span[:40] in decision_resolver.normalize(untrusted)


def supersedes(old: PendingProposal, new_fingerprint: str) -> bool:
    """A correction before approval. The old proposal must stop being answerable the moment the goal it
    described is no longer the goal — otherwise a stale "yes" executes the version that was corrected."""
    return old.arguments_fingerprint != new_fingerprint
