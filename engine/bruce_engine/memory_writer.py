"""The write policy — the decision about whether something is worth remembering AT ALL.

A memory store's failure mode is not forgetting. It is remembering too much, and remembering the wrong
kind of thing. Every row written here is served back to the model on future turns, so a store that
accepts whatever passed through the conversation becomes a slow-growing pile of half-true statements that
the model cannot distinguish from the true ones, and that the student never agreed to keep.

So writing is a REFUSAL by default. Five things must all hold, and the module is built so that the ones
that matter most cannot be waived by a caller in a hurry:

    useful later      the caller must state which future question this answers, and that sentence is
                      stored — it becomes `MemoryContext.reason_it_matters_now`. A memory nobody could
                      say the use of has no use.
    reasonably stable a `profile`/`relationships` fact may not be given a transient lifetime, and an
                      `episodic` one may not claim to be durable. A layer and a lifetime that disagree
                      means one of them is wrong.
    user-specific     anchored to the student (`SELF`) or to a resolved thing in their world.
    evidence-backed   the exact span of the student's own trusted words, VERIFIED to be present in that
                      text and absent from the pasted/forwarded material around it.
    not filler        a namespaced `domain.relation` predicate and a value short enough to be a fact.
                      Chatter cannot supply either, which is a cheaper filter than trying to recognise it.

THE FOUR HARD RULES, and where each one actually lives:

  1. ONLY TRUSTED TEXT MAKES A MEMORY. Enforced three times, deliberately: `MemoryRecord.__post_init__`
     refuses to construct one, the `ck_memory_trusted_source` CHECK refuses to store one, and `assess`
     refuses before either. Quoted / forwarded / attachment / provider / model content is recordable as
     `EvidenceRef` and can never be the source.
  2. A GUESS IS NEVER A FACT. `Basis` has no `inferred` member, and `assess` requires a span. An
     inference has no span to point at, so it has no way to be written. Uncertainty about something the
     student DID say is different and is kept: `hedged=True`, confidence capped at `HEDGED_CEILING`.
  3. CORRECTIONS SUPERSEDE. This module only ever INSERTs. Supersession is `memory_correction`'s job and
     the table's UPDATE trigger allows only the four lifecycle columns to change.
  4. IDENTITY IS NEVER INFERRED. `remember` writes facts and `record_style_signal` writes style, and
     there is no argument to either that crosses them. A stylistic observation is forced to
     `MemoryKind.style`, which no factual query can reach — so "she uses this slang, therefore she is X"
     has no expressible destination. `SENSITIVE_TRAITS` is a second, independent net for a mislabelled
     ordinary write, and it is a taxonomy of traits rather than a list of words.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa

from . import decision_resolver
from . import memory_record as mr
from .db import user_session
from .memory_record import (
    FACTUAL, MEMORY_RECORDS, SELF, SENSITIVE_TRAIT_STEMS, SENSITIVE_TRAITS, TRUSTED_FOR_MEMORY, Basis,
    DURABLE_LAYERS, Evidence, EvidenceRef, MemoryKind, MemoryRecord, RetentionPolicy, Sensitivity,
    SourceType,
)

log = logging.getLogger("bruce.memory")   # CONTENT-FREE: ids, kinds, verdicts — never subject or value


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

REFUSALS = frozenset({UNTRUSTED_SOURCE, NO_SPAN, SPAN_NOT_IN_TRUSTED_TEXT, SPAN_FROM_UNTRUSTED_CONTENT,
                      SENSITIVE_TRAIT, STYLE_IS_NOT_A_FACT, NOT_USER_SPECIFIC, UNSTABLE_FOR_LAYER,
                      NO_REASON_IT_MATTERS, FILLER, SOURCE_WAS_FORGOTTEN})

HEDGED_CEILING = 0.7
"""What a hedge is worth. "i think it's ms delgado" is a real observation and is kept, but it may never
be served at the same confidence as a flat statement, because the presentation layer reads `confidence`
to decide whether to say a thing or ask about it."""


class MemoryWriteError(Exception):
    """Raised when a CALLER asks for a write that could never be legitimate — a style signal submitted
    through the fact door, a proposal for another user. Not used for ordinary refusals, which are typed
    verdicts: a refusal is a normal outcome, a bad call is a programming error."""


# --- the proposal ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryProposal:
    """A candidate memory plus everything needed to judge it. Nothing here is taken on faith except the
    caller's claim about which words it rests on, and that claim is checked."""

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


# --- grounding ---------------------------------------------------------------------------------------


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


def names_sensitive_trait(predicate: str | None) -> bool:
    """Does this predicate claim one of the identity traits Bruce does not store?

    Token-wise on the namespaced predicate, so `profile.religion`, `profile.religious_practice` and
    `world.household_immigration` all land. It reads the predicate — the CLAIM being made — and never the
    student's words, which is why it cannot be defeated by phrasing, slang or the language they type in.
    """
    tokens = set(mr.normalize(predicate).replace(".", " ").replace("_", " ").split())
    return bool(tokens & SENSITIVE_TRAITS) or any(
        t.startswith(stem) for t in tokens for stem in SENSITIVE_TRAIT_STEMS)


# --- the policy ----------------------------------------------------------------------------------------


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
        # leak it. If a feature ever genuinely needs one it gets its own consented surface, and it will
        # be obvious in review that it is doing so — which it would not be if this door were left ajar.
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
    """The proposal as a record. Raises if `assess` refused — every field that could be forged is derived
    here rather than accepted, so there is no path from a refused proposal to a stored row.

    `basis` is a parameter only so `memory_correction` can mark a repair as such. It cannot be widened
    into an inference: `Basis` has no member that means one.
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


# --- persistence -----------------------------------------------------------------------------------------


async def remember(p: MemoryProposal, *, reason_it_matters: str | None = None,
                   now: datetime | None = None) -> MemoryRecord | None:
    """Store the fact, or return None with the refusal logged. The only write path for factual memory.

    Also refuses to re-learn from a source the student has already forgotten. "forget that" does not mean
    "forget it until you re-read the message it came from" — see `memory_correction.MemoryForget`.
    """
    decision = assess(p)
    if not decision.stores:
        log.info("memory_refused reason=%s kind=%s domain=%s", decision.verdict, p.kind.value,
                 mr.domain_of(p.predicate))
        return None

    rec = build(p, now=now)
    async with user_session(p.user_id) as s:
        if p.source_message_id and await _source_was_forgotten(s, p.user_id, p.source_message_id):
            log.info("memory_refused reason=%s kind=%s", SOURCE_WAS_FORGOTTEN, p.kind.value)
            return None
        await s.execute(sa.insert(MEMORY_RECORDS).values(
            **mr.to_row(rec), reason_it_matters=(reason_it_matters or p.reason_it_matters).strip(),
            created_at=sa.func.now()))
    log.info("memory_stored id=%s kind=%s domain=%s confidence=%.2f", rec.memory_id, rec.kind.value,
             rec.domain, rec.confidence)
    return rec


async def _source_was_forgotten(session, user_id: UUID, source_message_id: str) -> bool:
    """Has the student forgotten anything derived from this message? The tombstone keeps the message id
    (content-free lineage, exactly what `retention.py` preserves after erasing raw text) precisely so
    this question is answerable after the content is gone."""
    found = (await session.execute(sa.select(MEMORY_RECORDS.c.memory_id).where(
        MEMORY_RECORDS.c.user_id == user_id,
        MEMORY_RECORDS.c.source_message_id == source_message_id,
        MEMORY_RECORDS.c.forgotten_at.isnot(None)).limit(1))).first()
    return found is not None


async def record_style_signal(*, user_id: UUID, relation: str, value: str, trusted_text: str,
                              stated_span: str, source_message_id: str | None = None,
                              observed_at: datetime | None = None,
                              now: datetime | None = None) -> MemoryRecord | None:
    """The ONLY door for observations about how the student writes.

    `kind` is hard-coded, not a parameter. There is no argument to this function that produces a factual
    record, and no argument to `remember` that produces a style one — so an observation about vocabulary,
    slang, punctuation or name cannot become a claim about who someone is, no matter what a caller
    intended. That absence of a path is the enforcement; `SENSITIVE_TRAITS` is only the backstop.

    Style is re-observed constantly, so `last_confirmed_at` (not a new row per message) is what keeps it
    current — see `memory_correction.confirm`.
    """
    grounding = _grounded(stated_span, trusted_text, None)
    if grounding != STORE:
        log.info("style_refused reason=%s", grounding)
        return None
    now = now or datetime.now(timezone.utc)
    rec = MemoryRecord(
        memory_id=uuid4(), user_id=user_id, kind=MemoryKind.style, subject=SELF,
        predicate=f"style.{relation}", value=value.strip(),
        evidence=Evidence(stated_span=stated_span, basis=Basis.stated),
        source_message_id=source_message_id, source_type=SourceType.trusted_user_text,
        confidence=1.0, observed_at=observed_at or now, last_confirmed_at=None,
        retention_policy=RetentionPolicy.season, sensitivity=Sensitivity.ordinary,
        user_editable=True, contradicted_by=None, superseded_by=None)
    async with user_session(user_id) as s:
        await s.execute(sa.insert(MEMORY_RECORDS).values(
            **mr.to_row(rec),
            reason_it_matters="how this student writes, so replies sound like the person they are talking to",
            created_at=sa.func.now()))
    log.info("style_stored id=%s relation=%s", rec.memory_id, relation)
    return rec
