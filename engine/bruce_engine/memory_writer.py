"""THE ONE DOOR into `memory_records`. Every stored belief in the system is inserted by this module.

A memory store's failure mode is not forgetting. It is remembering too much, and remembering the wrong
kind of thing. Every row written here is served back to the model on future turns, so a store that
accepts whatever passed through the conversation becomes a slow-growing pile of half-true statements the
model cannot distinguish from the true ones, and that the student never agreed to keep.

WHY THIS MODULE IS A CHOKE POINT AND NOT A HELPER. #121b's policy was a function callers were expected to
call. That is a convention, and a convention is not a boundary: a second module that builds its own row
is one commit away and looks like ordinary code in review. Here the SQL that creates a memory exists
exactly once, in `_insert`, and `tests/test_memory_write_policy.py` walks the AST of every module in the
package to prove no other one constructs an insert against that table. The proof is on the syntax tree,
not on the source text — grepping for the table name matches the docstrings that discuss it, which has
already produced two false failures in this repo.

WHAT THE WRITER DECIDES, WHICH IS ALMOST NOTHING. `memory_policy.decide` is pure and total and makes
every judgement: the kind's floor, the registry, the retention, the sensitivity, the confidence ceiling.
This module's remaining jobs are the three that need I/O or identity, and they are the three that cannot
be decided from the candidate alone:

    claim lineage      `claim_key` and `claim_root_id`, so every version of one claim is addressable
                       together and `uq_memory_active_claim` can make two active contradictory versions
                       a database error rather than a race (#122 forgot only the newest row and left the
                       previous value readable through provenance).
    duplicate claims   an active row already exists for this claim key. Refused rather than inserted:
                       replacing a belief is `memory_correction`'s job, and it writes an audit row. A
                       writer that quietly superseded would make corrections unattributable.
    forgotten sources  "forget that" does not mean "forget it until you re-read the message it came
                       from".

WHY THE ROW IS BUILT HERE RATHER THAN VIA `MemoryRecord`. `MemoryRecord.__post_init__` refuses to be
constructed with any source but the student's own text — correct for the facts it models, and it makes
the type unable to express the attributed records #122 deliberately enabled when it dropped
`ck_memory_trusted_source`. "Practice is at six, per an email you forwarded" is a legitimate world fact
with forwarded provenance. It is not a fact about the student, and the thing that guarantees that is
`memory_policy.SUBJECT_KIND_MATRIX`, not the source column.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from . import decision_resolver
from . import memory_policy as policy
from . import memory_record as mr
from .db import user_session
from .memory_candidate import PROVENANCE_TO_SOURCE, MemoryCandidate, ProvenanceClass, SubjectType
from .memory_policy import Outcome, names_sensitive_trait
from .memory_record import (
    DURABLE_LAYERS, FACTUAL, HALF_LIFE, MEMORY_RECORDS, SELF, TRUSTED_FOR_MEMORY, Basis, Evidence,
    EvidenceRef, MemoryKind, MemoryRecord, RetentionPolicy, Sensitivity, SourceType,
)

log = logging.getLogger("bruce.memory")   # CONTENT-FREE: ids, kinds, verdicts — never subject or value

__all__ = [
    "MemoryWriter", "WriteReceipt", "MemoryWriteError", "MemoryProposal", "WriteDecision",
    "assess", "build", "remember", "record_style_signal", "names_sensitive_trait",
]


# --- verdicts --------------------------------------------------------------------------------------
# Typed and exhaustive, like the authorization denials: a refusal to remember has to be countable and
# explainable without grepping a log line, because "why didn't you remember that" is a question the
# student will actually ask.

STORE = "store"
UNTRUSTED_SOURCE = "untrusted_source"
NO_SPAN = "no_grounding_span"
SPAN_NOT_IN_TRUSTED_TEXT = "span_not_in_trusted_text"
SPAN_FROM_UNTRUSTED_CONTENT = "span_from_untrusted_content"
SENSITIVE_TRAIT = "sensitive_identity_trait"
STYLE_IS_NOT_A_FACT = "style_is_not_a_fact"
NOT_USER_SPECIFIC = "not_user_specific"
UNSTABLE_FOR_LAYER = "lifetime_contradicts_layer"
NO_REASON_IT_MATTERS = "no_reason_it_matters"
FILLER = "conversational_filler"
SOURCE_WAS_FORGOTTEN = "source_was_forgotten"
DUPLICATE_CLAIM = "claim_already_active"

REFUSALS = frozenset({UNTRUSTED_SOURCE, NO_SPAN, SPAN_NOT_IN_TRUSTED_TEXT, SPAN_FROM_UNTRUSTED_CONTENT,
                      SENSITIVE_TRAIT, STYLE_IS_NOT_A_FACT, NOT_USER_SPECIFIC, UNSTABLE_FOR_LAYER,
                      NO_REASON_IT_MATTERS, FILLER, SOURCE_WAS_FORGOTTEN, DUPLICATE_CLAIM})

HEDGED_CEILING = 0.7
"""What a hedge is worth. "i think it's ms delgado" is a real observation and is kept, but it may never
be served at the same confidence as a flat statement, because the presentation layer reads `confidence`
to decide whether to say a thing or ask about it."""


class MemoryWriteError(Exception):
    """Raised when a CALLER asks for a write that could never be legitimate — a candidate for another
    user, a style signal submitted through the fact door. Not used for ordinary refusals, which are typed
    outcomes: a refusal is a normal result, a bad call is a programming error."""


# --- the receipt -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteReceipt:
    """What `evaluate` did. Returned for every candidate including the refused ones, because a caller
    that cannot tell "refused" from "stored" will eventually assume the second."""

    outcome: Outcome
    reason: str
    candidate_id: UUID | None = None
    memory_id: UUID | None = None
    claim_key: str | None = None
    confidence: float = 0.0

    @property
    def stored(self) -> bool:
        """Stored AND believed. False for a quarantined row, which exists but is unreachable by every
        retrieval path — a caller checking this flag cannot mistake one for knowledge."""
        return self.outcome is Outcome.store

    @property
    def persisted(self) -> bool:
        return self.outcome in (Outcome.store, Outcome.quarantine)


# --- the writer ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryWriter:
    """Bound to one student for its whole life, like `MemoryRetriever` and for the same reason.

    There is no method here that accepts another user's id, so a cross-user write is not a thing this
    type can express. `db.user_session` sets `app.user_id` transaction-locally and `memory_records` runs
    FORCE ROW LEVEL SECURITY underneath, so the guarantee does not rest on this class alone.
    """

    user_id: UUID

    async def evaluate(self, candidate: MemoryCandidate) -> WriteReceipt:
        """THE write path. Decide, then persist what the decision permits — never what the candidate asked
        for.

        Every stored term is taken from `PolicyDecision` rather than from the candidate: confidence is the
        provenance-capped number, retention and sensitivity come from the registry. A caller's opinion
        about those three is advisory and can only make them stricter, which is what stops a confident
        extractor from choosing its own terms.

        NOT ENFORCED HERE, and it needs a schema change rather than a check: "a style memory must be a
        behavioural aggregate over several messages". A `MemoryCandidate` describes ONE observation and
        carries no occurrence count, so a first sighting and a fiftieth are the same object. What is
        enforced is the half that is expressible — a style value may not be a verbatim copy of the
        student's words (`memory_policy._style_gate`) — and the kind separation means even a premature
        style row cannot become a claim about who someone is.
        """
        if candidate.user_id != self.user_id:
            raise MemoryWriteError("a writer bound to one student cannot write another's memory")

        decision = policy.decide(candidate)
        if decision.outcome is Outcome.reject:
            log.info("memory_refused reason=%s kind=%s subject_type=%s domain=%s", decision.reason,
                     candidate.kind.value, candidate.subject_type.value,
                     mr.domain_of(candidate.predicate))
            return WriteReceipt(Outcome.reject, decision.reason, candidate_id=candidate.candidate_id)

        subject = _subject_of(candidate)
        key = mr.claim_key(kind=candidate.kind, subject=subject, predicate=candidate.predicate)
        memory_id = uuid4()
        status = "active" if decision.outcome is Outcome.store else "quarantined"
        values = _row_values(candidate, decision, memory_id=memory_id, subject=subject, claim_key=key,
                             status=status)

        async with user_session(self.user_id) as s:
            if candidate.source_id and await _source_was_forgotten(s, self.user_id, candidate.source_id):
                log.info("memory_refused reason=%s kind=%s", SOURCE_WAS_FORGOTTEN, candidate.kind.value)
                return WriteReceipt(Outcome.reject, SOURCE_WAS_FORGOTTEN,
                                    candidate_id=candidate.candidate_id)
            if status == "active" and await _claim_is_active(s, self.user_id, key):
                # Replacing a belief is `memory_correction`'s job — it supersedes in one transaction and
                # writes an audit row. Silently superseding here would make the change unattributable.
                log.info("memory_refused reason=%s kind=%s", DUPLICATE_CLAIM, candidate.kind.value)
                return WriteReceipt(Outcome.reject, DUPLICATE_CLAIM, candidate_id=candidate.candidate_id)
            try:
                await _insert(s, values)
            except IntegrityError:
                # `uq_memory_active_claim` firing means another transaction won the same claim between
                # the check above and this insert. The database is the authority, not the read.
                log.info("memory_refused reason=%s kind=%s", DUPLICATE_CLAIM, candidate.kind.value)
                return WriteReceipt(Outcome.reject, DUPLICATE_CLAIM, candidate_id=candidate.candidate_id)

        log.info("memory_%s id=%s kind=%s domain=%s confidence=%.2f",
                 "stored" if status == "active" else "quarantined", memory_id, candidate.kind.value,
                 mr.domain_of(candidate.predicate), decision.confidence)
        return WriteReceipt(decision.outcome, decision.reason, candidate_id=candidate.candidate_id,
                            memory_id=memory_id, claim_key=key, confidence=decision.confidence)


async def _insert(session, values: dict) -> None:
    """THE ONLY INSERT INTO `memory_records` IN THE ENGINE.

    Kept as its own one-statement function so the AST proof in `tests/test_memory_write_policy.py` has a
    single, obvious thing to point at, and so a future edit that adds a second write path has to look
    like what it is.
    """
    await session.execute(sa.insert(MEMORY_RECORDS).values(**values))


def _subject_of(candidate: MemoryCandidate) -> str:
    """`SELF` for anything about the student, their own subject otherwise.

    Folded here rather than trusted from the caller so "me", "myself" and a display name cannot become
    three different students inside one account — and so `claim_key` is stable across the spellings.
    """
    if candidate.subject_type is SubjectType.user or candidate.kind is MemoryKind.style:
        return SELF
    return mr.normalize(candidate.subject_id)


def _row_values(candidate: MemoryCandidate, decision, *, memory_id: UUID, subject: str, claim_key: str,
                status: str) -> dict:
    """The candidate and the decision as columns. Nothing the caller set is copied without a decision
    behind it, except the value, the subject and the evidence — the three things only the caller knows."""
    value = candidate.proposed_value.strip()
    normalized = (candidate.normalized_value or "").strip() or mr.normalize(value)
    return {
        "memory_id": memory_id,
        "user_id": candidate.user_id,
        "kind": candidate.kind.value,
        "subject": subject,
        "predicate": candidate.predicate,
        "value_json": {"value": value},
        "normalized_value": normalized[:300],
        "evidence_text": _evidence_json(candidate),
        "source_message_id": candidate.source_id,
        "source_type": PROVENANCE_TO_SOURCE[candidate.provenance_class].value,
        "confidence": decision.confidence,
        "observed_at": candidate.observed_at,
        "last_confirmed_at": None,
        "expires_at": _expires_at(candidate.observed_at, decision.retention),
        "freshness_class": "fresh",
        "retention_policy": decision.retention.value,
        "sensitivity": decision.sensitivity.value,
        "user_editable": True,
        "status": status,
        "contradicted_by_id": None,
        "superseded_by_id": None,
        "entity_key": mr.entity_key(subject),
        "domain": mr.domain_of(candidate.predicate),
        # A brand-new claim is its own root. Every later version copies this id, so "forget that" reaches
        # the superseded spellings as well as the current one.
        "claim_root_id": memory_id,
        "claim_key": claim_key,
        "reason_it_matters": candidate.usefulness_reason.strip()[:300],
        "created_at": sa.func.now(),
    }


def _evidence_json(candidate: MemoryCandidate) -> str:
    """The evidence blob, carrying the FULL provenance class alongside the span.

    `source_type` cannot express the nine provenance classes — three trusted ones collapse into
    `trusted_user_text` and `system_derived` has no spelling at all (see
    `memory_candidate.PROVENANCE_TO_SOURCE`). Recording it here keeps provenance recoverable for
    `memory_provenance` without a schema change. `Evidence.from_json` ignores keys it does not know, so
    this round-trips harmlessly through the existing reader.
    """
    return json.dumps({
        "stated_span": candidate.evidence_text,
        "basis": Basis.stated.value,
        "hedged": bool(candidate.is_inferred),
        "corroboration": [],
        "provenance_class": candidate.provenance_class.value,
        "explicitly_stated_by_user": bool(candidate.explicitly_stated_by_user),
    })


def _expires_at(observed_at: datetime, retention: RetentionPolicy) -> datetime | None:
    """When retrieval should stop returning this, computed to agree with `compute_freshness`.

    Twice the half-life, which is where `Freshness.expired` begins. Derived from that one table rather
    than a second constant, because a read-time expiry that disagreed with the freshness a record reports
    would make "why did Bruce stop using this" unanswerable.
    """
    half_life = HALF_LIFE[retention]
    if half_life is None:
        return None
    return observed_at + half_life * 2


async def _claim_is_active(session, user_id: UUID, key: str) -> bool:
    """Is a version of this claim already believed? The partial unique index is the real guarantee; this
    read exists so the ordinary case returns a typed refusal instead of an IntegrityError."""
    found = (await session.execute(sa.select(MEMORY_RECORDS.c.memory_id).where(
        MEMORY_RECORDS.c.user_id == user_id, MEMORY_RECORDS.c.claim_key == key,
        MEMORY_RECORDS.c.status == "active").limit(1))).first()
    return found is not None


async def _source_was_forgotten(session, user_id: UUID, source_message_id: str) -> bool:
    """Has the student forgotten anything derived from this message? The tombstone keeps the message id
    (content-free lineage, exactly what `retention.py` preserves after erasing raw text) precisely so
    this question is answerable after the content is gone."""
    found = (await session.execute(sa.select(MEMORY_RECORDS.c.memory_id).where(
        MEMORY_RECORDS.c.user_id == user_id,
        MEMORY_RECORDS.c.source_message_id == source_message_id,
        MEMORY_RECORDS.c.forgotten_at.isnot(None)).limit(1))).first()
    return found is not None


# --- the legacy proposal API ---------------------------------------------------------------------------
# `MemoryProposal` and `assess` are the #121b surface. `assess` is kept because it is a pure, total
# statement of the older rules that is still worth having under test. The two coroutines below are kept
# because they are the named entry points — and both now go through `MemoryWriter.evaluate`, building a
# candidate and handing it over. They are ADAPTERS, not a second door, which is what makes the "one door"
# claim in the module docstring true rather than aspirational.


@dataclass(frozen=True)
class MemoryProposal:
    """A candidate memory plus everything needed to judge it, in the #121b shape. Nothing here is taken
    on faith except the caller's claim about which words it rests on, and that claim is checked."""

    user_id: UUID
    kind: MemoryKind
    subject: str
    predicate: str
    value: str
    reason_it_matters: str
    """What future question this answers, in the student's terms. Stored, and surfaced at retrieval."""

    trusted_text: str
    """The student's own message. `_grounded` re-strips it through `decision_resolver` rather than
    trusting the caller to have done so — a caller that passes the raw message including a forwarded
    block is a mistake this module has to survive, not one it gets to assume away."""

    stated_span: str
    source_message_id: str | None = None
    source_type: SourceType = SourceType.trusted_user_text
    observed_at: datetime | None = None
    confidence: float = 1.0
    hedged: bool = False
    retention_policy: RetentionPolicy = RetentionPolicy.durable
    sensitivity: Sensitivity = Sensitivity.ordinary
    user_editable: bool = True
    corroboration: tuple[EvidenceRef, ...] = ()
    """Where a forwarded email or an attached screenshot goes. Recorded, never trusted."""

    untrusted_content: str | None = None
    """The pasted / forwarded / OCR'd material that was around the message. Never read for meaning — it
    exists so `_grounded` can prove the span did NOT come from it."""

    effective_confidence: float = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_confidence",
                           min(self.confidence, HEDGED_CEILING) if self.hedged else self.confidence)


@dataclass(frozen=True)
class WriteDecision:
    verdict: str
    reason: str = ""

    @property
    def stores(self) -> bool:
        return self.verdict == STORE


def _grounded(span: str | None, trusted_text: str | None, untrusted: str | None) -> str:
    """Are these the STUDENT's words? Returns STORE or the specific reason they are not.

    The same provenance check `authorization_evidence._grounded` makes before minting consent, for the
    same reason and against the same defect: 30 of the 38 adversarial-corpus failures were an affirmative
    that lived in forwarded or attached material being read as the student's own. A memory built that way
    is worse than a wrong action, because it is re-served on every later turn.

    Not a phrase table — it knows no vocabulary. It verifies a claim the caller made.
    """
    if not (span or "").strip():
        return NO_SPAN
    needle = mr.normalize(span)
    trusted = mr.normalize(decision_resolver.trusted_reply_text(trusted_text))
    if not needle or needle not in trusted:
        return SPAN_NOT_IN_TRUSTED_TEXT
    if untrusted and needle in mr.normalize(untrusted):
        # The words exist, but they also exist in someone else's message. Provenance is unprovable.
        return SPAN_FROM_UNTRUSTED_CONTENT
    return STORE


def assess(p: MemoryProposal) -> WriteDecision:
    """Pure, total: should this be remembered? No I/O, no model, no network.

    Ordered so the reported reason is the most serious true one — provenance before content, because a
    record sourced from someone else's words is a different kind of wrong from a record that is merely
    useless, and the log should say so.
    """
    if p.source_type not in TRUSTED_FOR_MEMORY:
        return WriteDecision(UNTRUSTED_SOURCE,
                             f"{p.source_type.value} is evidence about a conversation, not a user fact")

    if p.kind is MemoryKind.style:
        # The fact door will not accept style, and `record_style_signal` will not accept anything else.
        # The two channels have no shared entry point, which is what makes rule 4 structural.
        return WriteDecision(STYLE_IS_NOT_A_FACT, "style observations go through record_style_signal")
    if p.kind not in FACTUAL:
        raise MemoryWriteError(f"unknown memory kind {p.kind!r}")

    if names_sensitive_trait(p.predicate):
        # Refused whatever the source, including the student stating it flatly. Bruce has no feature that
        # needs a student's religion or health on file, and the only thing storing one buys is a way to
        # leak it. `memory_policy.PROFILE_REGISTRY` is the primary defence now; this is the backstop for
        # a caller with a genuine write who LABELS it as a trait.
        log.info("memory_refused reason=%s domain=%s", SENSITIVE_TRAIT, mr.domain_of(p.predicate))
        return WriteDecision(SENSITIVE_TRAIT, "identity traits are not stored by the general writer")

    if not (p.reason_it_matters or "").strip():
        return WriteDecision(NO_REASON_IT_MATTERS, "state the future question this answers")

    grounding = _grounded(p.stated_span, p.trusted_text, p.untrusted_content)
    if grounding != STORE:
        log.info("memory_refused reason=%s kind=%s", grounding, p.kind.value)
        return WriteDecision(grounding, "the memory does not rest on the student's own words")

    subject = mr.normalize(p.subject)
    if not subject:
        return WriteDecision(NOT_USER_SPECIFIC, "a memory needs a subject")
    if p.kind is MemoryKind.profile and subject != SELF:
        return WriteDecision(NOT_USER_SPECIFIC, "a profile fact is about the student, subject must be SELF")
    if p.kind is not MemoryKind.profile and subject == SELF:
        return WriteDecision(NOT_USER_SPECIFIC, f"{p.kind.value} is about someone or something else")
    if subject == mr.normalize(p.value):
        return WriteDecision(FILLER, "a tautology is not a fact")

    if not mr.is_namespaced_predicate(p.predicate):
        return WriteDecision(FILLER, "predicate must be a namespaced domain.relation")
    value = (p.value or "").strip()
    if not value or len(value) > mr.MAX_VALUE:
        return WriteDecision(FILLER, "a memory is a fact, not a transcript")

    if p.kind in DURABLE_LAYERS and p.retention_policy is RetentionPolicy.transient:
        return WriteDecision(UNSTABLE_FOR_LAYER, f"{p.kind.value} facts are not transient")
    if p.kind is MemoryKind.episodic and p.retention_policy is RetentionPolicy.durable:
        return WriteDecision(UNSTABLE_FOR_LAYER, "what happened on tuesday is not durable")

    return WriteDecision(STORE)


def build(p: MemoryProposal, *, basis: Basis = Basis.stated,
          now: datetime | None = None) -> MemoryRecord:
    """The proposal as an in-memory record. Raises if `assess` refused.

    NOT A WRITE PATH — it returns a value, and only `MemoryWriter.evaluate` reaches the table. Kept
    because the tests and `memory_provenance` need a way to talk about a record without one existing.
    """
    decision = assess(p)
    if not decision.stores:
        raise MemoryWriteError(f"{decision.verdict}: {decision.reason}")
    now = now or datetime.now(timezone.utc)
    return MemoryRecord(
        memory_id=uuid4(), user_id=p.user_id, kind=p.kind, subject=mr.normalize(p.subject),
        predicate=p.predicate, value=p.value.strip(),
        evidence=Evidence(stated_span=p.stated_span, basis=basis, hedged=p.hedged,
                          corroboration=tuple(p.corroboration)),
        source_message_id=p.source_message_id, source_type=SourceType.trusted_user_text,
        confidence=p.effective_confidence, observed_at=p.observed_at or now, last_confirmed_at=None,
        retention_policy=p.retention_policy, sensitivity=p.sensitivity, user_editable=p.user_editable,
        contradicted_by=None, superseded_by=None)


_KIND_TO_SUBJECT_TYPE: dict[MemoryKind, SubjectType] = {
    MemoryKind.profile: SubjectType.user,
    MemoryKind.relationships: SubjectType.relationship,
    MemoryKind.world: SubjectType.world,
    MemoryKind.entity: SubjectType.provider_entity,
    MemoryKind.episodic: SubjectType.conversation,
    MemoryKind.style: SubjectType.style,
}
"""The inverse of `memory_policy.SUBJECT_KIND_MATRIX`, used ONLY to lift a legacy `MemoryProposal` —
which predates subject types — into a candidate. Deriving the subject type from the kind is exactly the
conflation the two-field split exists to stop; it is acceptable here and nowhere else because `assess`
has already established that the proposal is grounded in the student's own trusted words. New callers
state the subject type themselves."""


async def remember(p: MemoryProposal, *, reason_it_matters: str | None = None,
                   now: datetime | None = None) -> MemoryRecord | None:
    """Store the fact, or return None with the refusal logged.

    Runs the #121b `assess` rules first, so a caller written against the old contract still gets the old
    refusals, then submits a candidate that the registry and the kind floors judge again. Both must pass:
    the adapter can only be stricter than either policy alone.
    """
    decision = assess(p)
    if not decision.stores:
        log.info("memory_refused reason=%s kind=%s domain=%s", decision.verdict, p.kind.value,
                 mr.domain_of(p.predicate))
        return None

    rec = build(p, now=now)
    observed = p.observed_at or now or datetime.now(timezone.utc)
    receipt = await MemoryWriter(p.user_id).evaluate(MemoryCandidate(
        user_id=p.user_id, subject_type=_KIND_TO_SUBJECT_TYPE[p.kind], subject_id=rec.subject,
        kind=p.kind, predicate=p.predicate, proposed_value=rec.value,
        normalized_value=mr.normalize(rec.value), evidence_text=p.stated_span,
        source_type=SourceType.trusted_user_text, source_id=p.source_message_id,
        provenance_class=ProvenanceClass.trusted_user_statement,
        explicitly_stated_by_user=True, inferred=False, confidence=p.effective_confidence,
        expected_stability=p.retention_policy,
        usefulness_reason=(reason_it_matters or p.reason_it_matters),
        sensitivity_class=p.sensitivity, retention_recommendation=p.retention_policy,
        observed_at=observed))
    if not receipt.stored:
        return None
    object.__setattr__(rec, "memory_id", receipt.memory_id)
    return rec


async def record_style_signal(*, user_id: UUID, relation: str, value: str, trusted_text: str,
                              stated_span: str, source_message_id: str | None = None,
                              observed_at: datetime | None = None,
                              now: datetime | None = None) -> MemoryRecord | None:
    """The ONLY door for observations about how the student writes.

    `kind` is hard-coded, not a parameter. There is no argument to this function that produces a factual
    record, and no argument to `remember` that produces a style one — so an observation about vocabulary,
    slang, punctuation or name cannot become a claim about who someone is, no matter what a caller
    intended. `memory_policy.SUBJECT_KIND_MATRIX` says the same thing a second time, from the other side.

    Style is re-observed constantly, so `last_confirmed_at` (not a new row per message) is what keeps it
    current — see `memory_correction.confirm`.
    """
    grounding = _grounded(stated_span, trusted_text, None)
    if grounding != STORE:
        log.info("style_refused reason=%s", grounding)
        return None
    now = now or datetime.now(timezone.utc)
    observed = observed_at or now
    receipt = await MemoryWriter(user_id).evaluate(MemoryCandidate(
        user_id=user_id, subject_type=SubjectType.style, subject_id=SELF, kind=MemoryKind.style,
        predicate=f"style.{relation}", proposed_value=value.strip(),
        normalized_value=mr.normalize(value), evidence_text=stated_span,
        source_type=SourceType.trusted_user_text, source_id=source_message_id,
        provenance_class=ProvenanceClass.trusted_user_statement, explicitly_stated_by_user=True,
        inferred=False, confidence=1.0, expected_stability=RetentionPolicy.season,
        usefulness_reason="how this student writes, so replies sound like the person they are talking to",
        sensitivity_class=Sensitivity.ordinary, retention_recommendation=RetentionPolicy.season,
        observed_at=observed))
    if not receipt.stored:
        return None
    return MemoryRecord(
        memory_id=receipt.memory_id, user_id=user_id, kind=MemoryKind.style, subject=SELF,
        predicate=f"style.{relation}", value=value.strip(),
        evidence=Evidence(stated_span=stated_span, basis=Basis.stated),
        source_message_id=source_message_id, source_type=SourceType.trusted_user_text,
        confidence=1.0, observed_at=observed, last_confirmed_at=None,
        retention_policy=RetentionPolicy.season, sensitivity=Sensitivity.ordinary,
        user_editable=True, contradicted_by=None, superseded_by=None)
