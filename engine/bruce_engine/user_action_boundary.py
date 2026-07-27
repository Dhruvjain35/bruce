"""The negative-action veto — deterministic, trusted-text-only, and ahead of every semantic reading.

WHY THIS IS NOT MORE PROMPT WORK. The 334-scenario evaluation drove model-produced false actions from
0.281 to 0.061, and 0.061 is not zero. It will never be zero: the model is a probabilistic reader, and a
probabilistic reader placed upstream of a provider write means some fraction of refusals execute. The
answer is not a better reader. It is an execution boundary the reader cannot cross.

So this module answers one question before the model is consulted at all: did the student, in their own
words, say don't. If the answer is anything other than a clean affirmative, no provider write can be
derived from this turn regardless of what the model later concludes.

TRUSTED TEXT ONLY. Quoted, forwarded, attached, provider-authored and model-authored content can neither
authorize nor reverse authorization — a coach's "yes add it", pasted by the student, is information about
a conversation. `decision_resolver.trusted_reply_text` already strips those regions and is reused rather
than reimplemented, so there is exactly one definition of what counts as the student's own words.

MONOTONIC. Negative, withdrawal, cancellation and ambiguous are terminal for the turn. Nothing later in
the pipeline can upgrade them. Only a LATER trusted turn carrying clear affirmative authorization creates
a new authorization state.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from . import decision_resolver

log = logging.getLogger("bruce.action_boundary")   # CONTENT-FREE: labels only, never message text


class Polarity(str, Enum):
    affirmative = "affirmative"
    negative = "negative"            # "no", "dont do that"
    withdrawal = "withdrawal"        # asked, then took it back in the same breath
    cancellation = "cancellation"    # calls off work Bruce proposed or is running
    ambiguous = "ambiguous"
    unrelated = "unrelated"


class Target(str, Enum):
    pending_decision = "pending_decision"
    active_run = "active_run"
    proposed_operation = "proposed_operation"
    provider_entity = "provider_entity"
    unknown = "unknown"


# Everything below is TERMINAL for the turn: no provider write, no approval, no execution run.
BLOCKING = frozenset({Polarity.negative, Polarity.withdrawal, Polarity.cancellation, Polarity.ambiguous})

# Calling off Bruce's own work. Distinct from a request to delete something that already exists in a
# provider — see the `provider_entity` handling below, which is the distinction the evaluation caught
# being collapsed into calendar.delete_event.
_CANCEL = re.compile(r"\b(?:cancel|call it off|abort|stop|hold off|never ?mind|nvm|forget it|"
                     r"scratch that|dont bother|drop it)\b")

# A reference to something that already exists on the provider side, which cancellation must NOT touch
# without its own resolution and its own authorization.
_EXISTING_ENTITY = re.compile(r"\b(?:delete|remove|take .{0,12}off|get rid of|erase)\b")

# An action was requested and then retracted within the same message.
_ACTION_VERB = re.compile(r"\b(?:add|put|schedule|book|save|create|send|email|mail|move|change|set)\b")

# An inline quotation of someone ELSE's words. `trusted_reply_text` strips quoted REGIONS (reply blocks,
# forwarded headers); this catches the other shape — a student pasting a sentence mid-message. Measured:
# 'coach replied "yes add it" — what does he mean' authorized a write, because the quoted affirmative was
# read as the student's own. Stripped before authorization is considered, never before a refusal is.
_INLINE_QUOTE = re.compile(r"[\"\u201c\u201d][^\"\u201c\u201d]{1,200}[\"\u201c\u201d]")


@dataclass(frozen=True)
class UserActionBoundary:
    """The deterministic verdict on one turn's trusted text. Computed BEFORE semantic routing."""
    trusted_text: str
    polarity: Polarity
    target: Target
    affirmative_span: str | None = None
    negative_span: str | None = None
    last_reversal_position: int | None = None
    confidence: float = 1.0
    source_message_id: str | None = None

    def blocks_execution(self) -> bool:
        return self.polarity in BLOCKING

    def authorizes(self) -> bool:
        """Deliberately narrow. Only a clean affirmative authorizes, and `unrelated` does not: silence
        about a pending question is not consent."""
        return self.polarity is Polarity.affirmative


def evaluate(text: str | None, *, has_pending_decision: bool = False, has_active_run: bool = False,
             source_message_id: str | None = None) -> UserActionBoundary:
    """Resolve one turn against the refusal/cancellation boundary. No model, no network, no I/O."""
    trusted = decision_resolver.trusted_reply_text(text)
    norm = decision_resolver.normalize(trusted)
    # Someone else's words may not authorize. They MAY still be part of a refusal, so the stripping is
    # applied only to what the affirmative path is allowed to see — asymmetric on purpose, in the safe
    # direction: removing a quote can turn a yes into a non-yes, never a no into a yes.
    resolution = decision_resolver.resolve_approval(_INLINE_QUOTE.sub(" ", text or ""))

    cancel = _CANCEL.search(norm)
    names_existing = bool(_EXISTING_ENTITY.search(norm))

    # PRECEDENCE, in the stated order. Every branch below the first is equally blocking; the ordering
    # decides how the block is DESCRIBED, and cancellation is the more specific description when the
    # student is calling off Bruce's own work rather than answering no to a question.
    if cancel:
        target = (Target.provider_entity if names_existing
                  else Target.pending_decision if has_pending_decision
                  else Target.active_run if has_active_run
                  else Target.unknown)
        return UserActionBoundary(trusted, Polarity.cancellation, target,
                                  negative_span=cancel.group(0), source_message_id=source_message_id)

    if resolution is decision_resolver.Resolution.rejected:
        # A retraction of something asked in the same message reads as withdrawal rather than a bare no,
        # because the two are handled identically here but read very differently in a log.
        polarity = Polarity.withdrawal if _ACTION_VERB.search(norm) else Polarity.negative
        target = (Target.pending_decision if has_pending_decision
                  else Target.active_run if has_active_run
                  else Target.proposed_operation)
        span = _CANCEL.search(norm) or _ACTION_VERB.search(norm)
        return UserActionBoundary(trusted, polarity, target, negative_span=span.group(0) if span else None,
                                  source_message_id=source_message_id)

    if resolution is decision_resolver.Resolution.ambiguous:
        return UserActionBoundary(trusted, Polarity.ambiguous,
                                  Target.pending_decision if has_pending_decision else Target.unknown,
                                  confidence=0.5, source_message_id=source_message_id)

    if resolution is decision_resolver.Resolution.approved:
        return UserActionBoundary(trusted, Polarity.affirmative,
                                  Target.pending_decision if has_pending_decision else Target.proposed_operation,
                                  affirmative_span=norm[:60], source_message_id=source_message_id)

    return UserActionBoundary(trusted, Polarity.unrelated, Target.unknown,
                              source_message_id=source_message_id)


# --- dual-channel agreement ----------------------------------------------------------------------------

CONTRADICTION_BLOCK = "block"
CONTRADICTION_CLARIFY = "clarify"
AGREE = "agree"


def reconcile(boundary: UserActionBoundary, model_polarity: str | None) -> str:
    """Compare the deterministic verdict with the model's reading. THE SAFER ONE WINS, always.

    The model's `decision_polarity` stays a diagnostic: it is useful for spotting where the two
    disagree, and it is never permitted to upgrade a deterministic negative into an execution.
    """
    det_negative = boundary.polarity in (Polarity.negative, Polarity.withdrawal, Polarity.cancellation)
    if det_negative and model_polarity == "approve":
        log.warning("boundary_contradiction det=%s model=approve -> blocked", boundary.polarity.value)
        return CONTRADICTION_BLOCK
    if boundary.polarity is Polarity.ambiguous and model_polarity in ("approve", None):
        return CONTRADICTION_CLARIFY
    if boundary.authorizes() and model_polarity == "decline":
        # The student's own words read as yes and the model read no. Neither side is trusted enough to
        # execute on a disagreement about consent.
        log.warning("boundary_contradiction det=affirmative model=decline -> clarify")
        return CONTRADICTION_CLARIFY
    return AGREE
