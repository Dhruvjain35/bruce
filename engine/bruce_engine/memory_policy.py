"""The write policy: what Bruce is ALLOWED to believe about a person, decided from a registry.

THE DEFECT THIS REPLACES, MEASURED. The previous lane's user-profile check was a denylist — a taxonomy of
sensitive traits (`memory_writer.names_sensitive_trait`) applied to the predicate. Run against sixteen
euphemistic candidates ("going through it", "on something for it", "doesn't eat during the day right
now"), three tripped the taxonomy and thirteen were stored. That is not a tuning problem and no amount of
vocabulary fixes it: a denylist has to recognise the claim, and the whole point of a euphemism is that it
is not recognisable. The claim "he's been going through it" and the claim "he has depression" differ only
in words, and words are exactly what a policy must not depend on.

SO THE DIRECTION IS INVERTED. A `subject_type=user` candidate is refused unless its predicate is one of
the ten in `PROFILE_REGISTRY`. Nothing reads the value. Nothing pattern-matches the evidence. A
euphemism, a slang diagnosis, a religious implication and a plain unregistered fact are all the same
shape — an unregistered user predicate — and they get the same refusal, so the harness in
`tests/test_memory_euphemism.py` can assert the property structurally instead of enumerating phrases.

The trait taxonomy survives as `names_sensitive_trait`, demoted to what it was always good for: catching a
caller who has a genuine write and LABELS it sensitively (`profile.religion`). It is a backstop, and
`test_memory_euphemism` does not depend on it firing.

THREE INDEPENDENT GATES, and a candidate must clear all of them:

    SUBJECT_KIND_MATRIX   which kinds a subject type may reach at all. One table, closed. This is what
                          makes "a style observation can never become profile identity" true by
                          construction rather than by a check — `SubjectType.style` has exactly one
                          reachable kind and it is not a factual one.
    PROFILE_REGISTRY      for `subject_type=user`, which predicates exist. Default deny.
    KIND_FLOORS           per kind: which provenance, what confidence, whether inference is permitted.
                          Exhaustive over `MemoryKind` (asserted in tests) so a seventh kind cannot be
                          added without deciding its floor.

WHY THE FLOOR IS PER KIND AND NOT ONE NUMBER. A single global threshold has to be set for the weakest
case and is then far too low for the strongest: 0.6 is a reasonable bar for "practice is at six, per a
forwarded email" and a reckless one for "your name is X". Confidence is also CAPPED by provenance before
the floor is applied (`PROVENANCE_CEILING`), so a caller cannot clear a floor by asserting 0.99 on an
attachment — the source's quality bounds the number, and the caller's opinion only lowers it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from . import memory_record as mr
from .memory_candidate import (
    INFERRED_PROVENANCE, TRUSTED_PROVENANCE, MemoryCandidate, ProvenanceClass, SubjectType,
)
from .memory_record import (
    DURABLE_LAYERS, SENSITIVE_TRAIT_STEMS, SENSITIVE_TRAITS, MemoryKind, RetentionPolicy, Sensitivity,
)


class Outcome(str, Enum):
    """What the policy decided. Three, not two, because "no" has two meanings that must not be merged.

    A rejected candidate leaves no row at all. A quarantined one is PERSISTED with `status='quarantined'`
    — outside every retrieval path (`memory_retrieval.RETRIEVABLE`), but reviewable. The distinction
    matters for the one case where refusing silently is itself a failure: a trusted, explicit statement
    about the student whose predicate nobody has registered yet is probably a gap in the registry, and
    throwing it away means the gap is never discovered.
    """

    store = "store"
    quarantine = "quarantine"
    reject = "reject"


# --- refusal reasons ---------------------------------------------------------------------------------
# Typed and countable, like the authorization denials. "Why didn't you remember that" is a question the
# student asks out loud, and it has to be answerable without grepping.

UNREGISTERED_USER_PREDICATE = "unregistered_user_predicate"
UNTRUSTED_SOURCE_FOR_USER = "untrusted_source_for_user_fact"
INFERENCE_NOT_PERMITTED = "inference_not_permitted_for_predicate"
BELOW_CONFIDENCE_FLOOR = "below_confidence_floor"
PROVENANCE_NOT_ACCEPTED = "provenance_not_accepted_for_kind"
SUBJECT_CANNOT_REACH_KIND = "subject_type_cannot_reach_kind"
STYLE_CANNOT_BECOME_FACT = "style_cannot_become_a_factual_claim"
SENSITIVE_WITHOUT_PRODUCT_NEED = "sensitive_without_registered_product_need"
SENSITIVE_TRAIT_LABEL = "sensitive_identity_trait"
NOT_EXPLICITLY_STATED = "predicate_requires_an_explicit_statement"
UNATTRIBUTED_UNTRUSTED = "untrusted_claim_without_attribution"
NO_EVIDENCE = "no_evidence_span"
NO_USEFULNESS_REASON = "no_usefulness_reason"
MALFORMED_PREDICATE = "predicate_is_not_namespaced"
EMPTY_VALUE = "value_is_empty_or_a_transcript"
UNSTABLE_FOR_KIND = "lifetime_contradicts_kind"
STYLE_IS_A_PHRASE_COPY = "style_value_copies_the_students_words"
STORED = "store"


# --- gate 1: which kinds a subject type may reach ------------------------------------------------------

SUBJECT_KIND_MATRIX: dict[SubjectType, frozenset[MemoryKind]] = {
    SubjectType.user: frozenset({MemoryKind.profile}),
    SubjectType.relationship: frozenset({MemoryKind.relationships}),
    SubjectType.world: frozenset({MemoryKind.world}),
    SubjectType.provider_entity: frozenset({MemoryKind.entity}),
    SubjectType.conversation: frozenset({MemoryKind.episodic}),
    SubjectType.style: frozenset({MemoryKind.style}),
}
"""The closed map from what a claim is ABOUT to where it may be filed.

Single-valued today, and written as sets because the constraint being expressed is "at most these", not
"exactly this" — a future `world` candidate that may also land as `entity` is an edit to one line here
rather than a new branch somewhere in the writer.

TWO RULES ARE ENTIRELY THIS TABLE, which is why neither needs a check of its own:

  * a style observation can never become profile identity, health, belief or demographic memory —
    `SubjectType.style` reaches only `MemoryKind.style`, and `MemoryKind.style` is excluded from
    `memory_record.FACTUAL` and from `memory_retrieval.shortlist` by an explicit `kind != 'style'`.
  * received or uploaded material cannot become a fact about the student — untrusted provenance is
    refused for `SubjectType.user` outright (`_user_gate`), so there is no predicate, registered or not,
    that a forwarded email can reach.
"""


# --- gate 2: the user-profile registry ------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileRule:
    """One thing Bruce is permitted to know about the student, and the terms on which it may learn it.

    Every field is a term the CALLER cannot set. Retention and sensitivity in particular are read from
    here rather than from the candidate: #121b took both from the proposal, which meant a caller could
    file a durable fact as transient (it silently evaporates and the student is never told) or an
    ordinary one as sensitive (it becomes unreadable). The candidate's own `retention_recommendation` is
    advisory and is only ever used to make retention SHORTER, never longer.
    """

    predicate: str
    accepted_provenance: frozenset[ProvenanceClass]
    min_confidence: float
    retention: RetentionPolicy
    sensitivity: Sensitivity
    inference_allowed: bool
    requires_explicit_confirmation: bool
    product_need: str
    """The feature that breaks without this fact. Required prose, not decoration: the registry is the
    place where "why does Bruce store this" has to be answerable, and an entry nobody can finish this
    sentence for is an entry that should not exist. A SENSITIVE entry additionally may not be stored
    without one — see `_sensitivity_gate`."""


_STATEMENT = frozenset({ProvenanceClass.trusted_user_statement, ProvenanceClass.trusted_user_correction})
_PREFERENCE = _STATEMENT | {ProvenanceClass.explicit_user_preference}


def _rule(predicate: str, *, accepts: frozenset[ProvenanceClass], min_confidence: float,
          retention: RetentionPolicy, product_need: str, sensitivity: Sensitivity = Sensitivity.ordinary,
          inference_allowed: bool = False, requires_explicit_confirmation: bool = False) -> ProfileRule:
    return ProfileRule(predicate=predicate, accepted_provenance=accepts, min_confidence=min_confidence,
                       retention=retention, sensitivity=sensitivity, inference_allowed=inference_allowed,
                       requires_explicit_confirmation=requires_explicit_confirmation,
                       product_need=product_need)


PROFILE_REGISTRY: dict[str, ProfileRule] = {
    r.predicate: r for r in (
        _rule("profile.preferred_name", accepts=_STATEMENT, min_confidence=0.9,
              retention=RetentionPolicy.durable,
              product_need="addressing the student by the name they use, in every reply"),
        _rule("profile.timezone", accepts=_STATEMENT, min_confidence=0.9,
              retention=RetentionPolicy.durable,
              # Derivable because the alternative is worse: a wrong timezone silently mis-times every
              # reminder, and the provider's own value is a better source than the student's recall.
              inference_allowed=True,
              product_need="scheduling reminders and deadlines at the right local hour"),
        _rule("profile.locale", accepts=_STATEMENT, min_confidence=0.9,
              retention=RetentionPolicy.durable, inference_allowed=True,
              product_need="formatting dates and numbers the way the student reads them"),
        _rule("profile.school", accepts=_STATEMENT, min_confidence=0.9,
              retention=RetentionPolicy.school_year,
              product_need="resolving course names, terms and the bell schedule"),
        _rule("profile.grade_level", accepts=_STATEMENT, min_confidence=0.9,
              retention=RetentionPolicy.school_year,
              product_need="knowing which year's deadlines and testing calendar apply"),
        _rule("profile.notification_preference", accepts=_PREFERENCE, min_confidence=0.85,
              retention=RetentionPolicy.durable,
              product_need="when and how often Bruce is allowed to message first"),
        _rule("profile.communication_preference", accepts=_PREFERENCE, min_confidence=0.85,
              retention=RetentionPolicy.durable,
              product_need="the channel and tone the student wants replies in"),
        _rule("profile.autonomy_preference", accepts=_PREFERENCE, min_confidence=0.9,
              retention=RetentionPolicy.durable,
              # How much Bruce may do without asking. Guessed wrong in the permissive direction this
              # authorizes unrequested action, so it may not be derived and may not be inferred from a
              # pattern of the student not objecting — absence of disagreement is not consent.
              requires_explicit_confirmation=True,
              product_need="how much Bruce may do without asking first"),
        _rule("profile.explicitly_connected_account", accepts=_STATEMENT, min_confidence=0.95,
              retention=RetentionPolicy.durable, requires_explicit_confirmation=True,
              product_need="which provider accounts the student has actually connected"),
        _rule("profile.explicitly_stated_schedule_preference", accepts=_PREFERENCE, min_confidence=0.85,
              retention=RetentionPolicy.school_year,
              product_need="the hours the student has said they do or do not want work scheduled in"),
    )
}
"""THE WHOLE SURFACE of what Bruce stores about a student, by name. Ten entries.

Read the list for what is ABSENT: no health, no diagnosis, no religion, no politics, no orientation, no
family structure, no financial state, no immigration status, no personality. Those are not missing
because a filter removes them — there is no predicate for them, so there is no candidate shape that
reaches one. That is the difference between this and a denylist, and it is the entire fix.

Every entry is a preference, an identifier, or a scheduling input, and each names the feature that breaks
without it. Adding an eleventh is a deliberate, reviewable act with a product need attached, which is
exactly the friction that should exist in front of "let's also remember...".
"""


# --- gate 3: confidence and provenance floors, per kind ---------------------------------------------------


@dataclass(frozen=True)
class KindFloor:
    """The minimum a candidate must clear to be filed under one kind."""

    kind: MemoryKind
    accepted_provenance: frozenset[ProvenanceClass]
    min_confidence: float
    inference_allowed: bool
    requires_attribution: bool
    """Untrusted provenance must carry a `source_id`. This is what "attributed" means operationally: an
    attributed world fact Bruce cannot name the source of is an unattributed one, and the difference
    between the two is the whole basis on which untrusted content is allowed to create world facts."""

    requires_source_freshness: bool
    """The candidate must carry `observed_at`. A world fact whose age is unknown cannot be aged out, and
    a fact that cannot go stale is a fact that is wrong forever once the season changes."""

    default_retention: RetentionPolicy


KIND_FLOORS: dict[MemoryKind, KindFloor] = {
    MemoryKind.profile: KindFloor(
        kind=MemoryKind.profile, accepted_provenance=TRUSTED_PROVENANCE, min_confidence=0.85,
        inference_allowed=False, requires_attribution=False, requires_source_freshness=False,
        default_retention=RetentionPolicy.durable),
    MemoryKind.relationships: KindFloor(
        # "Verified contact context" is `provider_verified` — a contact the provider actually holds.
        # Attributed untrusted material may create relationship EVIDENCE (a name in a forwarded thread),
        # which is why the untrusted classes are accepted here but `requires_attribution` is on.
        kind=MemoryKind.relationships,
        accepted_provenance=TRUSTED_PROVENANCE | {
            ProvenanceClass.provider_verified, ProvenanceClass.forwarded_content,
            ProvenanceClass.quoted_content, ProvenanceClass.attachment_content},
        min_confidence=0.5, inference_allowed=False, requires_attribution=True,
        requires_source_freshness=False, default_retention=RetentionPolicy.durable),
    MemoryKind.world: KindFloor(
        kind=MemoryKind.world,
        accepted_provenance=TRUSTED_PROVENANCE | {
            ProvenanceClass.provider_verified, ProvenanceClass.forwarded_content,
            ProvenanceClass.quoted_content, ProvenanceClass.attachment_content},
        min_confidence=0.4, inference_allowed=False, requires_attribution=True,
        requires_source_freshness=True, default_retention=RetentionPolicy.school_year),
    MemoryKind.entity: KindFloor(
        # Provider id preferred, and REQUIRED when the claim comes from a provider: an entity fact that
        # cannot be re-checked against the provider cannot refresh or expire with it.
        kind=MemoryKind.entity,
        accepted_provenance=TRUSTED_PROVENANCE | {
            ProvenanceClass.provider_verified, ProvenanceClass.forwarded_content,
            ProvenanceClass.quoted_content, ProvenanceClass.attachment_content},
        min_confidence=0.4, inference_allowed=False, requires_attribution=True,
        requires_source_freshness=True, default_retention=RetentionPolicy.season),
    MemoryKind.episodic: KindFloor(
        # An actually observed Bruce workflow event. `system_derived` is how the runtime records its own
        # work; no personality reading may enter, which is `inference_allowed=False` plus the matrix
        # keeping `SubjectType.user` out of this kind entirely.
        kind=MemoryKind.episodic,
        accepted_provenance=TRUSTED_PROVENANCE | {ProvenanceClass.system_derived},
        min_confidence=0.4, inference_allowed=False, requires_attribution=False,
        requires_source_freshness=True, default_retention=RetentionPolicy.episodic),
    MemoryKind.style: KindFloor(
        kind=MemoryKind.style, accepted_provenance=TRUSTED_PROVENANCE, min_confidence=0.5,
        inference_allowed=False, requires_attribution=False, requires_source_freshness=False,
        default_retention=RetentionPolicy.season),
}
"""Exhaustive over `MemoryKind` — asserted in `test_memory_write_policy`, so a seventh kind fails the
suite until someone decides what it accepts rather than silently inheriting the loosest floor."""


PROVENANCE_CEILING: dict[ProvenanceClass, float] = {
    ProvenanceClass.trusted_user_statement: 1.0,
    ProvenanceClass.trusted_user_correction: 1.0,
    ProvenanceClass.explicit_user_preference: 1.0,
    ProvenanceClass.provider_verified: 0.9,
    ProvenanceClass.forwarded_content: 0.6,
    ProvenanceClass.quoted_content: 0.6,
    ProvenanceClass.attachment_content: 0.5,
    ProvenanceClass.system_derived: 0.6,
    ProvenanceClass.model_inference: 0.4,
}
"""What a source is WORTH, capping what a caller may claim. "Confidence reflecting source quality" has to
be computed from the source, because the caller's number is the one thing in the candidate that a
confident-and-wrong extractor gets wrong most often. The cap only ever lowers: a caller who says 0.3
about their own flat statement is believed."""


def effective_confidence(candidate: MemoryCandidate) -> float:
    """The stored confidence: the caller's number, bounded by what the source can support."""
    ceiling = PROVENANCE_CEILING.get(candidate.provenance_class, 0.4)
    return max(0.0, min(float(candidate.confidence), ceiling))


# --- the sensitive-trait backstop -------------------------------------------------------------------------


def names_sensitive_trait(predicate: str | None) -> bool:
    """Does this predicate LABEL one of the identity traits Bruce does not store?

    Unchanged in behaviour from #121b and deliberately demoted in importance. It reads the claim's label,
    never the student's words, so it cannot be defeated by phrasing or language — but it also cannot
    catch a claim that declines to label itself, which is the measured 13-of-16 failure that made the
    registry necessary. Kept because it still catches the different mistake: a caller with a genuine
    write who files it as `profile.religion`.
    """
    tokens = set(mr.normalize(predicate).replace(".", " ").replace("_", " ").split())
    return bool(tokens & SENSITIVE_TRAITS) or any(
        t.startswith(stem) for t in tokens for stem in SENSITIVE_TRAIT_STEMS)


# --- the decision ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDecision:
    """What to do, why, and the terms the writer must use if it stores.

    `retention` and `sensitivity` are OUTPUTS. The writer takes them from here and never from the
    candidate, which is what stops a caller from choosing how long its own claim lives.
    """

    outcome: Outcome
    reason: str
    confidence: float = 0.0
    retention: RetentionPolicy = RetentionPolicy.transient
    sensitivity: Sensitivity = Sensitivity.ordinary
    rule: ProfileRule | None = None

    @property
    def stores(self) -> bool:
        """Stored AND retrievable. Quarantine is a row, not a belief — `stores` is false for it, so a
        caller that checks this flag cannot accidentally treat a quarantined claim as known."""
        return self.outcome is Outcome.store

    @property
    def persists(self) -> bool:
        return self.outcome in (Outcome.store, Outcome.quarantine)


def _shape_gate(c: MemoryCandidate) -> str | None:
    """The cheap structural requirements, before anything interesting is decided."""
    if not mr.is_namespaced_predicate(c.predicate):
        return MALFORMED_PREDICATE
    value = (c.proposed_value or "").strip()
    if not value or len(value) > mr.MAX_VALUE:
        return EMPTY_VALUE
    if not (c.usefulness_reason or "").strip():
        return NO_USEFULNESS_REASON
    if not (c.evidence_text or "").strip():
        # Applies to EVERY provenance, not only the trusted ones. An attributed world fact with no
        # evidence is not attributed to anything, and the attribution is the licence to store it.
        return NO_EVIDENCE
    return None


def _user_gate(c: MemoryCandidate) -> PolicyDecision | None:
    """Everything specific to a claim about the STUDENT. Returns None if the candidate clears it.

    Ordered so the reported reason is the most serious true one: provenance before registration, because
    "a forwarded email tried to describe you" and "we don't store that" are different events and the log
    should not confuse them.
    """
    if not c.is_trusted:
        # THE 30-of-38 DEFECT, at the only place it could still enter. No predicate, registered or not,
        # is reachable from received or uploaded material. Receiving a message about yourself, or
        # uploading a screenshot that describes you, is not you saying it.
        return PolicyDecision(Outcome.reject, UNTRUSTED_SOURCE_FOR_USER)

    rule = PROFILE_REGISTRY.get(c.predicate)
    if rule is None:
        # DEFAULT DENY, and the whole euphemism answer. Nothing here read the value.
        #
        # Quarantined rather than rejected only because the candidate is otherwise well-formed and
        # trusted: that is the shape of a registry gap worth discovering, and a quarantined row is
        # unreachable by every retrieval path. An untrusted or inferred one never gets this far.
        if c.is_inferred:
            return PolicyDecision(Outcome.reject, UNREGISTERED_USER_PREDICATE)
        return PolicyDecision(Outcome.quarantine, UNREGISTERED_USER_PREDICATE)

    if c.provenance_class not in rule.accepted_provenance:
        # A preference predicate accepts a preference; an identifier does not. Collapsing the trusted
        # classes would make `explicit_user_preference` a universal key to the registry.
        return PolicyDecision(Outcome.reject, PROVENANCE_NOT_ACCEPTED, rule=rule)
    if c.is_inferred and not rule.inference_allowed:
        return PolicyDecision(Outcome.reject, INFERENCE_NOT_PERMITTED, rule=rule)
    if rule.requires_explicit_confirmation and not c.explicitly_stated_by_user:
        # Withheld rather than dropped: the student may well have meant it, and this is the class of
        # claim where acting on a maybe is the actual harm.
        return PolicyDecision(Outcome.quarantine, NOT_EXPLICITLY_STATED, rule=rule)
    if effective_confidence(c) < rule.min_confidence:
        return PolicyDecision(Outcome.reject, BELOW_CONFIDENCE_FLOOR, rule=rule)
    return None


def _sensitivity_gate(c: MemoryCandidate, rule: ProfileRule | None) -> str | None:
    """A sensitive fact needs an explicit trusted disclosure AND a registered product need.

    Both halves are required and today the second one is unsatisfiable by construction: no entry in
    `PROFILE_REGISTRY` declares `sensitivity=sensitive`, so every sensitive user claim fails here even if
    the student stated it flatly. That is the intended posture — Bruce has no feature that needs one, and
    the only thing storing one buys is a way to leak it. The check is written in general form so that a
    future entry has to declare a product need to open the door, and so the door opening is visible in a
    diff of this file rather than implicit in a threshold somewhere.
    """
    sensitive = c.sensitivity_class is Sensitivity.sensitive or names_sensitive_trait(c.predicate)
    if not sensitive:
        return None
    if names_sensitive_trait(c.predicate):
        return SENSITIVE_TRAIT_LABEL
    if not (c.is_trusted and c.explicitly_stated_by_user):
        return SENSITIVE_WITHOUT_PRODUCT_NEED
    if rule is None or rule.sensitivity is not Sensitivity.sensitive or not rule.product_need.strip():
        return SENSITIVE_WITHOUT_PRODUCT_NEED
    return None


def _kind_gate(c: MemoryCandidate, floor: KindFloor, *, inference_allowed: bool) -> str | None:
    """The floor for the kind. `inference_allowed` is passed in rather than read from the floor because
    the floor is the DEFAULT and a registry entry is an exact-predicate exception to it — "no inference
    by default" is not the same rule as "no inference". `profile.timezone` is the case that forces the
    distinction: the kind forbids derivation, that one predicate permits it, and a floor that could not
    be overridden per predicate would make the registry's `inference_allowed` column dead."""
    if c.provenance_class not in floor.accepted_provenance:
        return PROVENANCE_NOT_ACCEPTED
    if c.is_inferred and not inference_allowed:
        return INFERENCE_NOT_PERMITTED
    if floor.requires_attribution and c.provenance_class not in TRUSTED_PROVENANCE and not (
            c.source_id or "").strip():
        return UNATTRIBUTED_UNTRUSTED
    if floor.requires_source_freshness and c.observed_at is None:
        return NO_EVIDENCE
    if effective_confidence(c) < floor.min_confidence:
        return BELOW_CONFIDENCE_FLOOR
    return None


def _style_gate(c: MemoryCandidate) -> str | None:
    """Style may describe a habit, never reproduce a sentence.

    A style record whose value is a verbatim span of the student's message is not an aggregate
    observation, it is a quote — and a quote filed as style is how a one-off remark becomes a standing
    description of a person. Checked structurally (is the value a substring of its own evidence) rather
    than by length or vocabulary.

    NOT ENFORCED HERE: "aggregate over several messages". A candidate describes one observation and
    carries no occurrence count, so the writer cannot tell a first sighting from a fiftieth. See the
    note in `MemoryWriter.evaluate`.
    """
    value = mr.normalize(c.proposed_value)
    evidence = mr.normalize(c.evidence_text)
    if value and evidence and value in evidence:
        return STYLE_IS_A_PHRASE_COPY
    return None


def decide(c: MemoryCandidate) -> PolicyDecision:
    """Pure and total: may this be remembered, and on what terms? No I/O, no model, no clock.

    Gate order is the reporting order, chosen so the reason names the most serious true problem:
    subject/kind mismatch, then the user gate, then sensitivity, then the kind floor. A style candidate
    aimed at `profile` is reported as a style violation, not as an unregistered predicate, because the
    two failures need different fixes.
    """
    shape = _shape_gate(c)
    if shape is not None:
        return PolicyDecision(Outcome.reject, shape)

    reachable = SUBJECT_KIND_MATRIX.get(c.subject_type, frozenset())
    if c.kind not in reachable:
        # Gate 1. A style observation aimed at any factual kind dies here, and so does provider content
        # aimed at `profile` — before any predicate or confidence is considered.
        reason = (STYLE_CANNOT_BECOME_FACT
                  if c.subject_type is SubjectType.style or c.kind is MemoryKind.style
                  else SUBJECT_CANNOT_REACH_KIND)
        return PolicyDecision(Outcome.reject, reason)

    rule = PROFILE_REGISTRY.get(c.predicate) if c.is_about_the_user else None
    if c.is_about_the_user:
        refusal = _user_gate(c)
        if refusal is not None and refusal.outcome is Outcome.reject:
            return refusal
        quarantined = refusal if refusal is not None else None
    else:
        quarantined = None

    sensitive = _sensitivity_gate(c, rule)
    if sensitive is not None:
        return PolicyDecision(Outcome.reject, sensitive, rule=rule)

    floor = KIND_FLOORS[c.kind]
    failed = _kind_gate(c, floor, inference_allowed=(rule.inference_allowed if rule is not None
                                                     else floor.inference_allowed))
    if failed is not None:
        return PolicyDecision(Outcome.reject, failed, rule=rule)

    if c.kind is MemoryKind.style:
        style_failure = _style_gate(c)
        if style_failure is not None:
            return PolicyDecision(Outcome.reject, style_failure)

    retention = _retention_for(c, rule, floor)
    if retention is None:
        return PolicyDecision(Outcome.reject, UNSTABLE_FOR_KIND, rule=rule)

    sensitivity = rule.sensitivity if rule is not None else _sensitivity_for(c)
    if quarantined is not None:
        return PolicyDecision(Outcome.quarantine, quarantined.reason, confidence=effective_confidence(c),
                              retention=retention, sensitivity=sensitivity, rule=rule)
    return PolicyDecision(Outcome.store, STORED, confidence=effective_confidence(c),
                          retention=retention, sensitivity=sensitivity, rule=rule)


def _retention_for(c: MemoryCandidate, rule: ProfileRule | None,
                   floor: KindFloor) -> RetentionPolicy | None:
    """The lifetime, taken from policy and only ever SHORTENED by the caller.

    Returns None when the candidate's own declared stability contradicts its kind — a durable layer asked
    to be transient, or an episodic record asked to be durable. That contradiction means one of the two
    is wrong, and guessing which is how a profile fact silently evaporates or a Tuesday becomes permanent.
    """
    if c.kind in DURABLE_LAYERS and c.expected_stability is RetentionPolicy.transient:
        return None
    if c.kind is MemoryKind.episodic and c.expected_stability is RetentionPolicy.durable:
        return None
    policy = rule.retention if rule is not None else floor.default_retention
    ranked = (RetentionPolicy.transient, RetentionPolicy.episodic, RetentionPolicy.season,
              RetentionPolicy.school_year, RetentionPolicy.durable)
    recommended = c.retention_recommendation
    if recommended in ranked and ranked.index(recommended) < ranked.index(policy):
        # A caller may ask Bruce to keep something for LESS time than policy allows. The reverse is the
        # thing that has to be impossible.
        return recommended
    return policy


def _sensitivity_for(c: MemoryCandidate) -> Sensitivity:
    """Non-user claims keep the caller's sensitivity, bounded away from `sensitive`.

    A `sensitive` classification is only meaningful for a registered user predicate, and none exist, so
    accepting the label here would create rows nobody can read for a reason nobody recorded.
    """
    return Sensitivity.private if c.sensitivity_class is Sensitivity.sensitive else c.sensitivity_class
