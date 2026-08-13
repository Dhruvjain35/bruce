"""AuthorizationEvidence — the POSITIVE half of the execution boundary.

#119 built the negative half: a refusal cannot cross into a provider write. That is necessary and not
sufficient. A veto only answers "did the student say don't". It says nothing about the far more common
shape of a wrong action, which is Bruce executing something nobody ever said yes to — an inferred goal, a
model's confident misreading, a stale approval reused for different arguments, a webhook redelivery
re-sending an email, an approval typed by someone else in a forwarded thread.

So execution is not permitted by the ABSENCE of a refusal. It is permitted only by the PRESENCE of an
authorization: a durable record that a specific trusted message from a specific user authorized a specific
operation with a specific set of arguments, and that nothing has since invalidated it.

WHAT IS NOT AUTHORIZATION (the first four rules, and the reason this module exists):

    actionability == executable      is a reading of language
    decision_polarity == approve     is a reading of language
    turn_role == decision_response   is a reading of language
    a generated plan                 is a proposal about the future

None of those are consent. All four are produced by a model that is wrong some measurable fraction of the
time — measured on the open set at 0.061 false actions after the prompt work was exhausted. A boundary
built out of the model's own output inherits that error rate exactly. This one is built out of the
student's own trusted words plus deterministic state, so the model's error rate does not appear in it.

THE TWO HALVES MEET HERE. `user_action_boundary` can VETO. This module can PERMIT. Neither can do the
other's job, and a provider write requires both: no blocking boundary, and a valid authorization.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID, uuid4

from . import decision_resolver, directive_scope
from . import user_action_boundary as uab

log = logging.getLogger("bruce.authz")   # CONTENT-FREE: ids, operations, verdicts — never message text


class AuthorizationType(str, Enum):
    """How consent was established. The type is not decoration: rule 12 keys off it, because the kinds of
    consent are not interchangeable at the moment something irreversible is about to happen."""

    direct_explicit = "direct_explicit"
    """The student's own message named this operation. The strongest ordinary case and the most common."""

    decision_approval = "decision_approval"
    """Bruce proposed an exact operation, and the student answered yes to THAT proposal. The only kind
    where the arguments were on screen before consent was given, which is why destructive work requires
    this shape or a correction of it."""

    correction_confirmation = "correction_confirmation"
    """A repair of an operation the student already authorized ("no, i said friday"). It inherits the
    original consent's standing because it narrows an operation the student had already agreed to."""

    standing_policy = "standing_policy"
    """A durable preference the student set once ("always add stuff from coach's emails"). Convenience,
    never consent for anything irreversible — the student was not present for this particular decision."""

    delegated_permission = "delegated_permission"
    """Granted by another authorized principal (a parent account, a future shared workspace). Bruce may
    never mint one for itself; it exists so that the day one is introduced, rule 12 already refuses it
    for destructive work rather than being retrofitted afterwards."""


# --- denial reasons ------------------------------------------------------------------------------------
# Typed and exhaustive: an execution refusal must be explainable to a human without reading the code, and
# countable in the evaluation without string-matching a log line.

ALLOW = "allow"
NO_AUTHORIZATION = "no_authorization"
NOT_AFFIRMATIVE = "polarity_not_affirmative"
UNTRUSTED_SOURCE = "untrusted_source"
WRONG_USER = "wrong_user"
WRONG_CONVERSATION = "wrong_conversation"
OPERATION_MISMATCH = "operation_mismatch"
PROVIDER_MISMATCH = "provider_mismatch"
ARGUMENTS_CHANGED = "arguments_changed"
EXPIRED = "expired"
INVALIDATED = "invalidated"
SUPERSEDED = "superseded"
CONSUMED = "consumed"
DESTRUCTIVE_NEEDS_OWN = "destructive_requires_own_authorization"
NOT_YET_VALID = "not_yet_valid"

DENIALS = frozenset({NO_AUTHORIZATION, NOT_AFFIRMATIVE, UNTRUSTED_SOURCE, WRONG_USER, WRONG_CONVERSATION,
                     OPERATION_MISMATCH, PROVIDER_MISMATCH, ARGUMENTS_CHANGED, EXPIRED, INVALIDATED,
                     SUPERSEDED, CONSUMED, DESTRUCTIVE_NEEDS_OWN, NOT_YET_VALID})


# --- destructive operations ----------------------------------------------------------------------------

DESTRUCTIVE: frozenset[str] = frozenset({
    "google_calendar.delete_event",   # the event is gone; there is no provider undo
    "gmail.send_message",             # mail cannot be unsent
    "gmail.reply_to_thread",
})
"""Operations whose effect cannot be taken back by Bruce. Rule 12: these are never authorized by a
standing policy, a delegated permission, or an inferred request — only by consent given against the exact
arguments (a Decision the student approved, a correction of one, or the student explicitly naming this
operation on a resolved entity in their own words).

A calendar CREATE is deliberately absent. It is reversible by Bruce itself (`calendar_adapter.undo` reads
back to prove the event is gone), and treating it as destructive would put a confirmation in front of the
single most common thing a student asks for. `tool_registry` marks it reversible=False, which means
something narrower there — that the TOOL has no undo argument, not that the world cannot be restored.
That divergence is deliberate and is asserted in the tests so the two definitions cannot silently merge.
"""

# Every write capability in the registry must be classified here — destructive or not. A new write tool
# that nobody classified would otherwise default to "not destructive", which is the wrong direction to
# fail. `test_authorization_evidence.py` asserts this set stays exhaustive.
NON_DESTRUCTIVE_WRITES: frozenset[str] = frozenset({
    "google_calendar.create_event",
    "google_calendar.update_event",
    "gmail.create_draft",
})


def is_destructive(provider: str, operation: str) -> bool:
    return f"{provider}.{operation}" in DESTRUCTIVE


# Operations that act on something ALREADY IN the student's world. These require the student's own words
# to point at them, because the alternative is Bruce changing a thing that exists on the strength of a
# turn that never mentioned it.
#
# Measured, and this is why the set exists: the adversarial corpus fed 238 turns to the boundary, and a
# message reading only "see" — attached to a screenshot in which a coach says "yes ur excused" — minted an
# authorization and updated a real calendar event. The trusted text did not refuse, and "did not refuse"
# was being treated as "asked for it". Creating something new is deliberately NOT in this set: "add
# practice friday" and a flyer with a date on it are the ordinary ask, and a create is reversible.
TARGETS_EXISTING_ENTITY: frozenset[str] = DESTRUCTIVE | frozenset({
    "google_calendar.update_event",
})


def needs_grounded_request(provider: str, operation: str) -> bool:
    return f"{provider}.{operation}" in TARGETS_EXISTING_ENTITY


# Which authorization types may stand behind a destructive operation at all. `direct_explicit` is admitted
# only with `explicit_operation_request` set — see `grant`.
_DESTRUCTIVE_OK = frozenset({AuthorizationType.decision_approval,
                             AuthorizationType.correction_confirmation,
                             AuthorizationType.direct_explicit})


# --- lifetimes -----------------------------------------------------------------------------------------
# Consent goes stale. How fast depends on how present the student was when they gave it.

TTL: dict[AuthorizationType, timedelta] = {
    # The student typed it just now and is watching. Long enough for the turn and its retries.
    AuthorizationType.direct_explicit: timedelta(minutes=15),
    # Bruce showed the exact operation and the student said yes. The work may legitimately run later (a
    # mission picks it up, a lease retries), so this outlives the turn — but not the day.
    AuthorizationType.decision_approval: timedelta(hours=24),
    AuthorizationType.correction_confirmation: timedelta(minutes=15),
    # A preference, not a decision. Long-lived by nature, and barred from destructive work regardless.
    AuthorizationType.standing_policy: timedelta(days=30),
    AuthorizationType.delegated_permission: timedelta(hours=24),
}


# --- argument normalization ----------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def normalize_arguments(provider: str, operation: str, arguments: dict | None) -> dict:
    """Canonical form of the arguments an authorization is bound to.

    The fingerprint has to be stable against things that do not change what happens in the world — key
    order, surrounding whitespace, the casing of an email address — and sensitive to everything that does.
    Getting that backwards in either direction is a real failure: too loose and an approval for 4pm
    authorizes 9pm; too strict and a retry of the identical operation is refused as "arguments changed"
    and the student's work silently stops.

    Values that carry no meaning (None, empty string, empty collection) are DROPPED rather than recorded
    as present-and-empty, so an argument dict that gained an explicit `thread_id=None` still matches the
    approval that was granted before the field existed.
    """
    out: dict = {}
    for key in sorted((arguments or {}).keys()):
        value = _normalize_value(key, (arguments or {})[key])
        if value is None or value == "" or value == [] or value == {}:
            continue
        out[str(key)] = value
    return out


_EMAILISH = ("to", "cc", "bcc", "from", "recipient", "account", "sender")


def _normalize_value(key: str, value):
    if isinstance(value, str):
        # CONSEQUENTIAL TEXT BINDS EXACTLY. `_WS.sub(" ", ...)` is right for an argument whose whitespace
        # carries no meaning, and wrong for the fields a student actually read before approving: "\n\n"
        # and "\n" are two different emails, and an approval that cannot tell them apart is not an
        # approval. Flattening here meant a body whose paragraph breaks were replaced by spaces after
        # approval still matched its own fingerprint and sailed through `execution_gate.require`.
        # See consequential_payload.EXACT_TEXT_FIELDS — one list, so the two cannot disagree.
        from .consequential_payload import EXACT_TEXT_FIELDS
        if key.lower() in EXACT_TEXT_FIELDS:
            return value
        v = _WS.sub(" ", value).strip()
        # Addresses are case-insensitive in practice; the body of an email is not.
        if key.lower() in _EMAILISH or key.lower().endswith("_email"):
            v = v.lower()
        return v
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _normalize_value(str(k), value[k]) for k in sorted(value.keys())
                if _normalize_value(str(k), value[k]) not in (None, "", [], {})}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_normalize_value(key, v) for v in value]
        items = [i for i in items if i not in (None, "", [], {})]
        # Recipient lists are a set, not a sequence: cc'ing two people in the other order is the same act.
        if key.lower() in _EMAILISH and all(isinstance(i, str) for i in items):
            return sorted(set(items))
        return items
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def fingerprint(normalized_arguments: dict) -> str:
    """A stable digest of the exact arguments consent was given for. Rules 7 and 8 are this one line: an
    authorization matches only the arguments it was granted against, and any change produces a different
    fingerprint and therefore a different (missing) authorization."""
    blob = json.dumps(normalized_arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- the record ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationEvidence:
    """One durable, immutable answer to "who said Bruce could do this, exactly, and when".

    Frozen, and every state change returns a NEW instance. An authorization is a historical fact; a
    mutable one could be edited into existence after the operation it supposedly permitted, which is
    precisely the shape of audit record that proves nothing.
    """

    authorization_id: str
    user_id: UUID
    conversation_id: str | None
    trusted_message_id: str | None
    source_message_timestamp: datetime
    decision_id: str | None
    provider: str
    operation: str
    normalized_arguments: dict
    arguments_fingerprint: str
    polarity: uab.Polarity
    authorization_type: AuthorizationType
    confidence: float
    created_at: datetime
    expires_at: datetime
    invalidated_at: datetime | None = None
    invalidated_by_message_id: str | None = None
    superseded_by_authorization_id: str | None = None
    consumed_at: datetime | None = None
    operation_receipt_id: str | None = None

    # Not in the original field list, and load-bearing. Exactly-once execution depends on being able to
    # RETRY a failed attempt: a lease expires mid-send, the runner picks the step up again, and the
    # adapter's marker ledger collapses it to the one message already sent. If consumption denied every
    # re-presentation, a transient provider error would permanently strand work the student authorized.
    # So consumption is scoped to the ATTEMPT: the attempt that consumed it may retry; a different attempt
    # may not. Without this, rule 10 and exactly-once semantics contradict each other.
    consumed_by_attempt: str | None = None

    # Set only by `grant` for direct_explicit, and only when the caller can state that the student's own
    # trusted words named this operation on a resolved target. It is what lets "delete chess club" delete
    # chess club, while an operation Bruce merely INFERRED from context cannot touch anything destructive.
    explicit_operation_request: bool = False

    @property
    def capability_key(self) -> str:
        return f"{self.provider}.{self.operation}"

    def is_live(self, *, now: datetime) -> bool:
        return (self.invalidated_at is None and self.superseded_by_authorization_id is None
                and self.expires_at > now)


class AuthorizationError(Exception):
    """Raised only by `grant` when a CALLER tries to mint an authorization that could never be valid. Not
    used for execution denials — those are typed verdicts, because a denial is an ordinary runtime outcome
    the student must get an honest reply about, while a bad grant is a programming error."""


# --- minting -------------------------------------------------------------------------------------------


def grant(*, user_id: UUID, provider: str, operation: str, arguments: dict | None,
          authorization_type: AuthorizationType, boundary: uab.UserActionBoundary,
          trusted_message_id: str | None, source_message_timestamp: datetime,
          conversation_id: str | None = None, decision_id: str | None = None,
          explicit_operation_request: bool = False, confidence: float = 1.0,
          now: datetime | None = None, ttl: timedelta | None = None) -> AuthorizationEvidence:
    """THE ONLY constructor. Every field that could be forged is derived here rather than accepted.

    Note what is not a parameter: no SemanticTurn, no Derivation, no actionability, no model polarity, no
    plan. There is deliberately no way to pass this function a model's opinion, because rules 1-4 are not
    checks that can fail — they are things this signature cannot express.

    Rules 5 and 6 are the `boundary` argument: consent comes from a UserActionBoundary computed over
    TRUSTED text only, and `user_action_boundary` already strips quoted, forwarded and inline-pasted
    content before deciding. A grant is refused outright when that boundary blocks, so a message that
    contains a refusal can never mint an authorization no matter which branch of the pipeline calls this.
    """
    now = now or datetime.now(timezone.utc)

    if boundary.blocks_execution():
        # Rule 9, at mint time rather than only at check time. Nothing later in the turn can turn a
        # refusal into consent, so the record must not come into existence in the first place.
        raise AuthorizationError(f"boundary blocks execution ({boundary.polarity.value})")

    if authorization_type is AuthorizationType.direct_explicit and not explicit_operation_request:
        # A direct grant asserts the student NAMED this operation. A caller that cannot say so is
        # describing an inference, and an inference is a proposal, not consent.
        raise AuthorizationError("direct_explicit requires explicit_operation_request")

    if not trusted_message_id and authorization_type is not AuthorizationType.standing_policy:
        # Every authorization except a stored policy must point at the exact message that created it, or
        # the invalidation chain has nothing to hang from and provenance cannot be answered.
        raise AuthorizationError(f"{authorization_type.value} requires a trusted_message_id")

    if authorization_type is AuthorizationType.decision_approval and not decision_id:
        raise AuthorizationError("decision_approval requires the decision_id it answers")

    if is_destructive(provider, operation):
        # Rule 12, enforced at BOTH ends. Checking only at execution would leave a record on disk that
        # claims to authorize a deletion, and a record like that is one refactor away from being trusted.
        if authorization_type not in _DESTRUCTIVE_OK:
            raise AuthorizationError(
                f"{authorization_type.value} may not authorize the destructive {provider}.{operation}")
        if authorization_type is AuthorizationType.direct_explicit and not explicit_operation_request:
            raise AuthorizationError("a destructive operation needs the student to name it")

    normalized = normalize_arguments(provider, operation, arguments)
    ev = AuthorizationEvidence(
        authorization_id=str(uuid4()), user_id=user_id, conversation_id=conversation_id,
        trusted_message_id=trusted_message_id, source_message_timestamp=source_message_timestamp,
        decision_id=decision_id, provider=provider, operation=operation,
        normalized_arguments=normalized, arguments_fingerprint=fingerprint(normalized),
        polarity=uab.Polarity.affirmative, authorization_type=authorization_type,
        confidence=confidence, created_at=now,
        expires_at=now + (ttl or TTL[authorization_type]),
        explicit_operation_request=explicit_operation_request)
    log.info("authz_granted id=%s type=%s op=%s.%s destructive=%s", ev.authorization_id,
             authorization_type.value, provider, operation, is_destructive(provider, operation))
    return ev


def _grounded(request_span: str | None, trusted: str, untrusted: str | None) -> bool:
    """Is the caller pointing at words the STUDENT wrote?

    The caller has already done deterministic work to decide there is a request here — a mutation verb
    matched, a recipient resolved, a referent was found. This asks it to show which of the student's own
    words that conclusion rests on, and then checks the claim: the span must be present in the trusted
    text, and must not be something that only appears in the pasted, forwarded or OCR'd material.

    Not a phrase table. It matches nothing and knows no vocabulary; it verifies a provenance claim the
    caller makes, which is what stops a coach's "you're approved, send it whenever" from becoming the
    student's request just because the student pasted it.
    """
    if not request_span or not request_span.strip():
        return False
    span = decision_resolver.normalize(request_span)
    if not span or span not in decision_resolver.normalize(trusted):
        return False
    if untrusted and span in decision_resolver.normalize(untrusted):
        # The words exist, but they exist in someone else's message. Provenance is unprovable, so no.
        return False
    return True


def try_grant(*, user_id: UUID, provider: str, operation: str, arguments: dict | None,
              authorization_type: AuthorizationType, text: str | None,
              trusted_message_id: str | None, source_message_timestamp: datetime | None = None,
              conversation_id: str | None = None, decision_id: str | None = None,
              has_pending_decision: bool = False, has_active_run: bool = False,
              request_span: str | None = None, untrusted_content: str | None = None,
              explicit_operation_request: bool = True,
              scope: directive_scope.ScopeProposal | None = None,
              now: datetime | None = None) -> AuthorizationEvidence | None:
    """Mint from a raw turn, or return None when the turn does not authorize anything.

    THE DEFECT THIS SIGNATURE EXISTS TO CLOSE. The first version of this function minted whenever the
    boundary did not BLOCK. Run against the 238-case adversarial corpus, that turned "see", "from my
    mom", "is this real or a scam" and "not asking u to do anything just showing u this" into consent for
    a send or a calendar change, because the affirmative lived in an attached screenshot or a forwarded
    email and the student's own words merely failed to object. Thirty of the corpus's thirty-eight
    failures were that one mistake: absence of refusal read as permission — the same shape as the P0 this
    whole programme started from, rebuilt one layer up.

    So a turn now has to POSITIVELY authorize:

      * `decision_approval` requires `boundary.authorizes()` — a clean affirmative answering the exact
        question Bruce asked. Unrelated is not consent, and neither is silence.
      * anything touching an entity that already exists requires a `request_span`: the caller must point
        at the student's own words, and `_grounded` checks that the words are really theirs.
      * creating something new stays as it is. That is the ordinary ask, it is reversible, and requiring
        ceremony for it would put a confirmation in front of the most common thing a student wants.

    `untrusted_content` is never evaluated. It is passed so the grounding check can prove a span did not
    come from it, and so a caller has somewhere to put the pasted material other than the trusted text.

    `scope` is an optional `directive_scope.ScopeProposal` — a checked reading of which thing a negation in
    this turn governed. It is forwarded to the boundary and NEVER consulted here, so this function has no
    way to be talked into a grant the boundary did not reach. It is dropped unless it was built for the
    exact operation being authorized: a reading about a send may not speak for a deletion.
    """
    now = now or datetime.now(timezone.utc)
    if scope is not None and scope.operation_id != f"{provider}.{operation}":
        log.info("authz_scope_wrong_operation op=%s.%s", provider, operation)
        scope = None
    boundary = uab.evaluate(text, has_pending_decision=has_pending_decision,
                            has_active_run=has_active_run, source_message_id=trusted_message_id,
                            scope=scope)
    if boundary.blocks_execution():
        log.info("authz_refused_at_boundary op=%s.%s polarity=%s", provider, operation,
                 boundary.polarity.value)
        return None

    if authorization_type is AuthorizationType.decision_approval:
        if not boundary.authorizes():
            log.info("authz_not_an_approval op=%s.%s polarity=%s", provider, operation,
                     boundary.polarity.value)
            return None
    elif needs_grounded_request(provider, operation):
        trusted = decision_resolver.trusted_reply_text(text)
        if not _grounded(request_span, trusted, untrusted_content):
            log.info("authz_ungrounded_request op=%s.%s", provider, operation)
            return None

    try:
        return grant(user_id=user_id, provider=provider, operation=operation, arguments=arguments,
                     authorization_type=authorization_type, boundary=boundary,
                     trusted_message_id=trusted_message_id,
                     source_message_timestamp=source_message_timestamp or now,
                     conversation_id=conversation_id, decision_id=decision_id,
                     explicit_operation_request=explicit_operation_request, now=now)
    except AuthorizationError:
        # A grant this caller was never entitled to make. Fail shut and let the turn answer honestly.
        log.warning("authz_grant_refused op=%s.%s type=%s", provider, operation,
                    authorization_type.value)
        return None


# --- the invalidation chain ----------------------------------------------------------------------------


def invalidates(boundary: uab.UserActionBoundary) -> bool:
    """Rule 9. Any later refusal, cancellation, withdrawal or ambiguity kills outstanding authorization.

    Ambiguity counts. "wait, hold on" is not a refusal, and treating it as harmless is how a student who
    is mid-thought gets an email sent on their behalf. The cost of being wrong in this direction is one
    extra question; in the other direction it is an irreversible act.
    """
    return boundary.blocks_execution()


def invalidate(ev: AuthorizationEvidence, *, by_message_id: str | None,
               at: datetime | None = None) -> AuthorizationEvidence:
    """Kill this authorization, recording which message killed it. Idempotent: re-invalidating keeps the
    FIRST cause, because the first one is the true one and later turns must not rewrite that history."""
    if ev.invalidated_at is not None:
        return ev
    log.info("authz_invalidated id=%s op=%s", ev.authorization_id, ev.capability_key)
    return replace(ev, invalidated_at=at or datetime.now(timezone.utc),
                   invalidated_by_message_id=by_message_id)


def supersede(ev: AuthorizationEvidence, by: AuthorizationEvidence) -> AuthorizationEvidence:
    """Rule 8's other half. When the student changes the arguments, the new consent does not merely sit
    alongside the old one — the old one is closed out and points at its replacement, so an audit can
    follow "what did they actually agree to in the end" in one direction without guessing."""
    if ev.authorization_id == by.authorization_id:
        raise AuthorizationError("an authorization cannot supersede itself")
    return replace(ev, superseded_by_authorization_id=by.authorization_id,
                   invalidated_at=ev.invalidated_at or by.created_at)


def consume(ev: AuthorizationEvidence, *, attempt_key: str, receipt_id: str | None = None,
            at: datetime | None = None) -> AuthorizationEvidence:
    """Spend it. Rule 11: one authorization, one operation — a second operation needs its own consent.

    Scoped to `attempt_key` so a retry of the SAME attempt is still permitted; see `consumed_by_attempt`.
    """
    return replace(ev, consumed_at=at or datetime.now(timezone.utc),
                   consumed_by_attempt=attempt_key,
                   operation_receipt_id=receipt_id or ev.operation_receipt_id)


# --- the check -----------------------------------------------------------------------------------------


def check(ev: AuthorizationEvidence | None, *, user_id: UUID, provider: str, operation: str,
          arguments: dict | None, conversation_id: str | None = None, attempt_key: str | None = None,
          now: datetime | None = None) -> str:
    """Pure, total: may THIS authorization execute THIS operation right now? Returns ALLOW or one reason.

    Order matters and is chosen so the reported reason is the most specific true one. Identity is checked
    before content (a record belonging to another student is not "arguments changed", it is a leak), and
    content before lifecycle (arguments that never matched were never authorized, expired or not).
    """
    now = now or datetime.now(timezone.utc)

    if ev is None:
        return NO_AUTHORIZATION

    # --- who
    if ev.user_id != user_id:
        # The one denial that is also a security event: evidence from another account reached this call.
        log.warning("authz_cross_user id=%s", ev.authorization_id)
        return WRONG_USER
    if conversation_id is not None and ev.conversation_id is not None and ev.conversation_id != conversation_id:
        # Consent given in one thread does not travel to another. A student approving something in a DM
        # with Bruce has not approved it inside a different conversation they may not even be reading.
        return WRONG_CONVERSATION

    # --- what (rules 1-4 collapse to this: only an affirmative record exists at all)
    if ev.polarity is not uab.Polarity.affirmative:
        return NOT_AFFIRMATIVE
    if ev.trusted_message_id is None and ev.authorization_type is not AuthorizationType.standing_policy:
        return UNTRUSTED_SOURCE
    if ev.provider != provider:
        return PROVIDER_MISMATCH
    if ev.operation != operation:
        return OPERATION_MISMATCH

    # --- rules 7 + 8
    incoming = normalize_arguments(provider, operation, arguments)
    if fingerprint(incoming) != ev.arguments_fingerprint:
        return ARGUMENTS_CHANGED

    # --- rule 12
    if is_destructive(provider, operation):
        if ev.authorization_type not in _DESTRUCTIVE_OK:
            return DESTRUCTIVE_NEEDS_OWN
        if ev.authorization_type is AuthorizationType.direct_explicit and not ev.explicit_operation_request:
            return DESTRUCTIVE_NEEDS_OWN

    # --- rules 9 + 10
    if ev.superseded_by_authorization_id is not None:
        return SUPERSEDED
    if ev.invalidated_at is not None:
        return INVALIDATED
    if ev.created_at > now:
        # A record dated in the future is either clock skew or a forgery; neither may execute.
        return NOT_YET_VALID
    if ev.expires_at <= now:
        return EXPIRED
    if ev.consumed_at is not None and (attempt_key is None or ev.consumed_by_attempt != attempt_key):
        # Rule 11. A redelivered webhook, a repeated "yes", or a second operation reaching for the same
        # consent all land here — the only re-presentation that survives is the identical attempt retrying.
        return CONSUMED

    return ALLOW
