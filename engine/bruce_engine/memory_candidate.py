"""What a caller is allowed to PROPOSE, which is deliberately not what gets stored.

#121b let a caller hand `memory_writer.remember` a fully-formed proposal whose fields the writer largely
took at face value: the caller chose the kind, the sensitivity, the retention and the source type, and
the writer's job was to find a reason to say no. That is the wrong shape for a default-deny boundary. A
policy that can only subtract from what the caller claimed inherits every field the caller got wrong, and
the interesting failures are exactly the ones where the caller was confident and wrong.

So the proposal and the record are now different types. `MemoryCandidate` is an OBSERVATION — a thing
someone noticed, with an honest account of where it came from and how sure anyone is. It carries no
authority: nothing here decides what kind the record will be, whether it is sensitive, how long it lives
or whether it is stored at all. `memory_policy` decides all of that from the registry, and
`MemoryWriter.evaluate` is the only thing that turns a decision into a row.

TWO FIELDS CARRY THE WHOLE BOUNDARY, and they are separate on purpose:

    subject_type      WHO or WHAT this claim is about. `user` means "a fact about the student", and that
                      is a structural fact about the candidate rather than a reading of its wording. It
                      is what makes the euphemism problem go away: "they've been going through it" and
                      "they have depression" are the same candidate shape — subject_type=user with a
                      predicate that is not in the profile registry — so both are refused by the same
                      rule, and neither refusal depends on recognising a phrase.

    provenance_class  WHOSE WORDS these are, at finer grain than `SourceType`. A student stating a fact,
                      correcting one, and expressing a preference are three different warrants that
                      `SourceType.trusted_user_text` collapses into one; a registry entry that accepts
                      preferences must not thereby accept arbitrary assertions.

WHY `inferred` IS A FIELD AND NOT A DERIVATION. `Basis` has no `inferred` member (see `memory_record`),
which stops a guess from being spelled as a stored fact but says nothing about a caller who believes its
inference is an observation. Making the caller declare it is not a trust decision — the policy also
treats `provenance_class=model_inference` as inferred regardless of what the flag says, so a caller that
lies about it gains nothing. The flag exists so an honest caller can be honest, and so the two signals
can be cross-checked against each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from .memory_record import MemoryKind, RetentionPolicy, Sensitivity, SourceType


class SubjectType(str, Enum):
    """What the claim is ABOUT. The single most load-bearing field on a candidate.

    Separate from `MemoryKind` because kind is where a record would be FILED and subject type is who it
    is about, and conflating them is how "the user's coach emailed about practice" becomes a fact about
    the user. A candidate names its subject type; the policy decides which kinds that subject type is
    allowed to reach, and for `user` the answer is a closed registry rather than a filter.
    """

    user = "user"
    """The student themselves. The only subject type governed by an allowlist, because it is the only one
    where a wrong row is a claim about a person rather than a claim about their timetable."""

    relationship = "relationship"
    """A person in the student's life, and what they are TO the student."""

    world = "world"
    """The student's environment — how their school runs, when a season starts. May be ATTRIBUTED to
    untrusted material ("practice is at six, per an email you forwarded") without becoming a fact about
    the student, which is the distinction #122's removal of `ck_memory_trusted_source` made storable."""

    provider_entity = "provider_entity"
    """A thing a connected provider is authoritative about: a Canvas course, a calendar event."""

    conversation = "conversation"
    """Something that happened in a Bruce workflow. Episodic by nature."""

    style = "style"
    """How the student writes. Never a fact about them; the policy gives this subject type exactly one
    reachable kind and no path into the factual ones."""


class ProvenanceClass(str, Enum):
    """Whose words, at the grain the policy actually needs to reason about.

    `SourceType` answers "is this the student's own text", which is the right question for the write ban
    #121b enforced and too coarse for a registry. Splitting the trusted side into three lets a predicate
    accept `explicit_user_preference` without also accepting every flat assertion the student makes, and
    splitting the untrusted side by medium keeps the confidence a source deserves attached to the source
    rather than chosen by the caller.
    """

    trusted_user_statement = "trusted_user_statement"
    """The student stating something in their own trusted text."""

    trusted_user_correction = "trusted_user_correction"
    """The student repairing something Bruce already believed. Inherits the standing of a statement."""

    explicit_user_preference = "explicit_user_preference"
    """The student saying what they WANT rather than what is true. The only warrant the preference
    predicates accept, so a preference registry entry cannot be used to store an assertion."""

    provider_verified = "provider_verified"
    """Synced from a connected provider. Authoritative about the PROVIDER's own entities and about
    nothing else — never about the student."""

    quoted_content = "quoted_content"
    forwarded_content = "forwarded_content"
    attachment_content = "attachment_content"
    """Someone else's words that passed through the student's message. Recordable, attributable, and
    structurally incapable of producing a fact about the student — the 30-of-38 defect."""

    model_inference = "model_inference"
    """Bruce's own reading. Always treated as inferred whatever `MemoryCandidate.inferred` says."""

    system_derived = "system_derived"
    """Computed by Bruce from state it already holds. Not an observation of the student."""


TRUSTED_PROVENANCE: frozenset[ProvenanceClass] = frozenset({
    ProvenanceClass.trusted_user_statement,
    ProvenanceClass.trusted_user_correction,
    ProvenanceClass.explicit_user_preference,
})
"""The student's own trusted words, in their three warrants. Everything else is evidence.

Defined as an explicit set rather than as "not the untrusted ones" so that adding a provenance class
later defaults to UNTRUSTED. A new member of this enum that nobody classified is refused, which is the
direction a default-deny boundary has to fail in — the previous lane's inverted check was exactly the
other one, and 13 of 16 euphemisms passed through it."""

UNTRUSTED_PROVENANCE: frozenset[ProvenanceClass] = frozenset(ProvenanceClass) - TRUSTED_PROVENANCE

INFERRED_PROVENANCE: frozenset[ProvenanceClass] = frozenset({
    ProvenanceClass.model_inference, ProvenanceClass.system_derived,
})
"""Provenance classes that ARE an inference regardless of the caller's `inferred` flag. Cross-checking
the two is what makes the flag safe to accept from a caller at all."""


PROVENANCE_TO_SOURCE: dict[ProvenanceClass, SourceType] = {
    ProvenanceClass.trusted_user_statement: SourceType.trusted_user_text,
    ProvenanceClass.trusted_user_correction: SourceType.trusted_user_text,
    ProvenanceClass.explicit_user_preference: SourceType.trusted_user_text,
    ProvenanceClass.provider_verified: SourceType.provider,
    ProvenanceClass.quoted_content: SourceType.quoted,
    ProvenanceClass.forwarded_content: SourceType.forwarded,
    ProvenanceClass.attachment_content: SourceType.attachment,
    ProvenanceClass.model_inference: SourceType.model,
    ProvenanceClass.system_derived: SourceType.model,
}
"""How a provenance class is spelled in the `source_type` COLUMN, which predates it and is what
`memory_retrieval._SOURCE_TRUST` scores on.

Lossy in one direction by necessity: three trusted classes collapse to `trusted_user_text`, and
`system_derived` has no `SourceType` of its own and is filed as `model` — the conservative choice, since
`_SOURCE_TRUST` scores `model` lowest and a derivation Bruce made about itself should not outrank the
student. The unlossy spelling is kept in the evidence blob (`MemoryWriter._evidence_json`), so provenance
is recoverable even though the column cannot express it. A `provenance_class` column would remove the
need for both; see the note in `memory_policy`."""


@dataclass(frozen=True)
class MemoryCandidate:
    """One proposed memory. Frozen, and inert: constructing one commits to nothing.

    Note what this type does NOT do. It runs no validation beyond the structural minimum, because a
    candidate that cannot be CONSTRUCTED cannot be counted, and the euphemism harness needs to count
    refusals. A candidate that will obviously be rejected is a legitimate object; being rejected is the
    outcome, not an exception.
    """

    user_id: UUID
    subject_type: SubjectType
    subject_id: str
    """The thing the claim is about, in the caller's terms. Folded to `memory_record.SELF` by the writer
    when `subject_type` is `user`, so "me", "myself" and a display name cannot become three students."""

    kind: MemoryKind
    predicate: str
    proposed_value: str
    normalized_value: str
    evidence_text: str
    """The words this rests on. For trusted provenance the policy requires it to be non-empty — an
    observation nobody can point at is an inference wearing an observation's label."""

    source_type: SourceType
    source_id: str | None
    provenance_class: ProvenanceClass
    explicitly_stated_by_user: bool
    inferred: bool
    confidence: float
    expected_stability: RetentionPolicy
    usefulness_reason: str
    sensitivity_class: Sensitivity
    retention_recommendation: RetentionPolicy
    observed_at: datetime
    candidate_id: UUID = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.candidate_id is None:
            object.__setattr__(self, "candidate_id", uuid4())

    @property
    def is_trusted(self) -> bool:
        """Trusted means the STUDENT's own words, and it is computed from provenance rather than read
        from a caller-set flag. `source_type` is checked as well: a candidate claiming trusted provenance
        while carrying a forwarded source type is inconsistent, and the safe reading of an inconsistency
        is the untrusting one."""
        return (self.provenance_class in TRUSTED_PROVENANCE
                and self.source_type is SourceType.trusted_user_text)

    @property
    def is_inferred(self) -> bool:
        """Inferred if the caller says so OR if the provenance is one Bruce produced. The OR is the point:
        a caller cannot launder an inference by leaving the flag false."""
        return bool(self.inferred) or self.provenance_class in INFERRED_PROVENANCE

    @property
    def is_about_the_user(self) -> bool:
        """The one question the euphemism harness turns on, answered structurally.

        Being about the user is a property of `subject_type`, not of what the value says. No phrase list
        is consulted here and none exists to be consulted, which is why an indirect health claim and a
        direct one are refused identically.
        """
        return self.subject_type is SubjectType.user
