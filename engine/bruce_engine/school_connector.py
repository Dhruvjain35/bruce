"""Provider-neutral SchoolConnector contract + canonical academic objects (P1 primitives 2 & 3).

This is the interface every school provider (Canvas today; Google Classroom / OneRoster / an ICS feed
later) implements, and the canonical shapes it must return. The rule this file exists to enforce is the
same one the messaging boundary enforces: a provider payload STOPS at the adapter. Nothing below a
connector — not the store, not the query layer, not a mission — may ever see a Canvas-shaped dict. The
adapter normalizes into these canonical objects, or it declares the capability unsupported. There is no
third option where a fabricated value leaks through.

Every canonical object that represents provider data carries a ``Provenance`` block:
  * the ORIGINAL provider id and provider URL (so a student can open the real thing),
  * the provider's own source timestamp AND Bruce's last-sync time (so staleness is visible),
  * an ``evidence`` link back to the stored source/source_span it was grounded in,
  * the ``capability_state`` it was produced under (supported vs limited vs a best-effort scrape),
  * and ``changes`` — the change history for that object (created/updated/deleted over syncs).

That block is non-negotiable: it is what lets the query layer answer "what's due / what changed / what
am I missing" with a citation for every item, never model prose or an unsourced guess.

Provider-neutral by construction: this module imports only ``school_capability``. It never imports
``canvas_fake`` (or any adapter). The dependency arrow points adapter -> contract, never the reverse.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
from typing import Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, Field

from .school_capability import CapabilityState, ProviderCapabilityMatrix, SchoolCapability

# ---------------------------------------------------------------------------------------------------
# provenance + change history — carried by every canonical object below
# ---------------------------------------------------------------------------------------------------


class ChangeType(str, enum.Enum):
    created = "created"
    updated = "updated"
    deleted = "deleted"


class ChangeRecord(BaseModel):
    """One entry in an object's change history: what changed, when, and under which sync cursor.

    ``changed_fields`` is empty for a create/delete and lists the canonical field names that differed
    for an update — enough for "what changed at school?" to say *how* it changed, not merely *that* it did.
    """

    change_type: ChangeType
    detected_at: datetime.datetime
    cursor_value: str | None = None                    # the sync cursor this change was observed under
    changed_fields: list[str] = Field(default_factory=list)


class SourceRef(BaseModel):
    """Evidence link: which stored source (and optional verbatim span) grounds a canonical object.

    Mirrors the existing sources -> source_spans lineage the intake pipeline uses, so a school object is
    auditable the same way an extracted deadline is: you can always get back to the exact provider
    payload and the snippet it came from.
    """

    source_id: str                        # id of the persisted SchoolSource (provider payload anchor)
    source_url: str | None = None         # convenience copy of the original provider URL
    span_id: str | None = None            # id of a SchoolSourceSpan, when a verbatim span was captured
    span_text: str | None = None          # the verbatim snippet (grounding), when available


class Provenance(BaseModel):
    """The mandatory provenance block on every provider-derived canonical object.

    If a field here is unknown, it is ``None`` — never a plausible-looking placeholder. A missing
    ``source_url`` means the provider genuinely gave no link, not that we invented one.
    """

    provider: str                                       # e.g. "canvas"
    provider_id: str                                    # ORIGINAL provider object id
    source_url: str | None = None                       # ORIGINAL provider URL for the object
    source_timestamp: datetime.datetime | None = None   # provider's own created/updated time
    last_synced_at: datetime.datetime                   # when Bruce last synced this object
    evidence: SourceRef | None = None                   # link to the stored source/source_span
    capability_state: CapabilityState = CapabilityState.supported  # state it was produced under
    changes: list[ChangeRecord] = Field(default_factory=list)      # change history for this object


# ---------------------------------------------------------------------------------------------------
# canonical academic objects — the graph the whole product reasons over
# ---------------------------------------------------------------------------------------------------


class SchoolSource(BaseModel):
    """A provider object's raw source anchor (the "source" canonical object).

    The durable evidence root: which provider, which object type, its id + URL, the provider timestamp,
    Bruce's last-sync time, and a content hash used for change detection. The minimized ``payload`` keeps
    just enough of the normalized provider fields to re-verify a claim — never a blob of the whole API
    response, matching the schema's "structured columns + minimized JSONB" rule.
    """

    id: str | None = None
    provider: str
    object_type: str                                    # "course" | "assignment" | "announcement" | ...
    provider_id: str
    source_url: str | None = None
    source_timestamp: datetime.datetime | None = None
    last_synced_at: datetime.datetime
    content_hash: str                                   # sha256 of the canonical payload (change detection)
    capability_state: CapabilityState = CapabilityState.supported
    payload: dict = Field(default_factory=dict)         # minimized normalized fields (not the raw response)


class SchoolSourceSpan(BaseModel):
    """A verbatim span of a provider object (the "source_span" canonical object) — grounding for a fact.

    E.g. the exact "Due Mar 14 at 11:59pm" text an assignment's due date was read from, so the due date
    is auditable rather than asserted.
    """

    id: str | None = None
    source_id: str
    span_text: str
    label: str | None = None                            # what this span grounds (e.g. "due_at")
    ordinal: int | None = None


class Institution(BaseModel):
    """The school/district the data belongs to."""

    provider_id: str
    name: str
    provenance: Provenance


class AcademicTerm(BaseModel):
    """A grading term/semester. ``is_current`` lets "current courses" be answered honestly."""

    provider_id: str
    name: str
    start_at: datetime.datetime | None = None
    end_at: datetime.datetime | None = None
    is_current: bool = False
    provenance: Provenance


class Instructor(BaseModel):
    """A teacher/TA on a course. Email is ``None`` when the provider does not expose it (never guessed)."""

    provider_id: str
    name: str
    email: str | None = None
    role: str | None = None                             # "teacher" | "ta" | ...
    provenance: Provenance


class Section(BaseModel):
    """A course section (period/block). A thin grouping — persisted as part of its course."""

    provider_id: str
    name: str
    course_provider_id: str
    provenance: Provenance


class Course(BaseModel):
    """A course the student is enrolled in — the spine of the academic graph."""

    provider_id: str
    name: str
    course_code: str | None = None
    workflow_state: str | None = None                   # "available" | "completed" | "unpublished"
    term_provider_id: str | None = None
    term_name: str | None = None
    institution_provider_id: str | None = None
    instructors: list[Instructor] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    url: str | None = None
    provenance: Provenance


class SubmissionStateKind(str, enum.Enum):
    """The student's submission state for an assignment — the axis the due-buckets pivot on."""

    none = "none"                # nothing submitted, not graded
    submitted = "submitted"      # submitted, not yet graded
    graded = "graded"            # a score/grade exists
    unknown = "unknown"          # provider could not report state (honest — not assumed "none")


class SubmissionState(BaseModel):
    """Lightweight submission state attached to an assignment (Canvas's ``include[]=submission`` shape).

    Enough to compute the unsubmitted/graded/overdue buckets without a second fetch. Full detail
    (comments, rubric assessment) comes from ``get_submission``.
    """

    kind: SubmissionStateKind = SubmissionStateKind.unknown
    submitted_at: datetime.datetime | None = None
    score: float | None = None
    grade: str | None = None                            # letter/points display, provider-formatted
    late: bool = False
    missing: bool = False


class Assignment(BaseModel):
    """An assignment — the object the north-star student queries revolve around.

    ``due_at`` is ``None`` for a genuinely undated assignment (surfaced as the ``undated`` bucket, never
    back-filled with a fake date). ``submission`` carries the current state so buckets are pure derivations.
    """

    provider_id: str
    course_provider_id: str
    name: str
    description: str | None = None
    due_at: datetime.datetime | None = None
    unlock_at: datetime.datetime | None = None
    lock_at: datetime.datetime | None = None
    points_possible: float | None = None
    submission_types: list[str] = Field(default_factory=list)
    url: str | None = None
    submission: SubmissionState = Field(default_factory=SubmissionState)
    provenance: Provenance


class Material(BaseModel):
    """Course material (file / page / external link)."""

    provider_id: str
    course_provider_id: str
    title: str
    kind: str                                           # "file" | "page" | "link"
    url: str | None = None
    provenance: Provenance


class Announcement(BaseModel):
    """A course announcement."""

    provider_id: str
    course_provider_id: str
    title: str
    message: str | None = None
    posted_at: datetime.datetime | None = None
    url: str | None = None
    provenance: Provenance


class Grade(BaseModel):
    """A grade on an assignment. A facet of the submission's assessment (persisted with the submission)."""

    assignment_provider_id: str
    score: float | None = None
    points_possible: float | None = None
    grade: str | None = None                            # letter / display grade
    graded_at: datetime.datetime | None = None
    provenance: Provenance


class RubricCriterion(BaseModel):
    description: str
    points: float | None = None
    awarded: float | None = None                        # points awarded on this criterion, if assessed


class Rubric(BaseModel):
    """A rubric attached to an assignment (+ any per-criterion assessment). Facet of the submission."""

    assignment_provider_id: str
    title: str | None = None
    points_possible: float | None = None
    criteria: list[RubricCriterion] = Field(default_factory=list)
    provenance: Provenance


class Feedback(BaseModel):
    """A feedback comment on a submission. Facet of the submission."""

    submission_provider_id: str
    author: str | None = None
    comment: str
    created_at: datetime.datetime | None = None
    provenance: Provenance


class Submission(BaseModel):
    """Full submission detail for an assignment: state + grade + rubric assessment + feedback comments."""

    provider_id: str
    assignment_provider_id: str
    state: SubmissionStateKind = SubmissionStateKind.unknown
    submitted_at: datetime.datetime | None = None
    attempt: int | None = None
    late: bool = False
    missing: bool = False
    url: str | None = None
    grade: Grade | None = None
    rubric: Rubric | None = None
    feedback: list[Feedback] = Field(default_factory=list)
    provenance: Provenance


class ScheduleEvent(BaseModel):
    """A calendar/schedule event exposed by the provider (class meeting, exam, office hours)."""

    provider_id: str
    title: str
    start_at: datetime.datetime | None = None
    end_at: datetime.datetime | None = None
    location: str | None = None
    course_provider_id: str | None = None
    url: str | None = None
    provenance: Provenance


# ---------------------------------------------------------------------------------------------------
# sync cursor contract + change sets
# ---------------------------------------------------------------------------------------------------


class SyncCursor(BaseModel):
    """A restart-safe incremental-sync position, one per (provider, resource).

    ``cursor_value`` is an OPAQUE provider token (Canvas uses an ISO ``updated_since`` timestamp; another
    provider might use a page token). Callers never interpret it — they persist it and hand it back on the
    next sync. That is what makes a sync resumable across a process restart.
    """

    provider: str
    resource: str                                       # "assignments" | "announcements" | "courses" | ...
    cursor_value: str | None = None
    updated_at: datetime.datetime | None = None


class ResourceChange(BaseModel):
    """One classified change from a sync: an object plus whether it was created/updated/deleted.

    For a delete the provider often gives only an id, so ``object`` may be ``None`` and ``provider_id`` is
    the anchor. For create/update, ``object`` is the full canonical object (a ``dict`` model dump, since a
    ``ResourceChange`` spans object types).
    """

    resource: str
    provider_id: str
    change_type: ChangeType
    object: dict | None = None                          # canonical object model_dump (None for delete)


class SyncPage(BaseModel):
    """What a connector returns from ``sync``: the changes since the given cursor + the next cursor.

    The connector reports its OWN view of created/updated/deleted (Canvas via ``updated_since`` + a
    tombstone list). The store then re-verifies against persisted content hashes — two honest layers, so a
    provider that under-reports a change is still caught at persist time.
    """

    resource: str
    changes: list[ResourceChange] = Field(default_factory=list)
    next_cursor: SyncCursor


# ---------------------------------------------------------------------------------------------------
# the honest result envelope — every connector method returns one of these
# ---------------------------------------------------------------------------------------------------

T = TypeVar("T")


@dataclasses.dataclass(frozen=True)
class ConnectorResult(Generic[T]):
    """A connector method's outcome, carrying the capability it answered and the honest state it did so in.

    When ``state`` is ``unsupported`` (or ``unknown``), ``data`` is ``None`` and ``reason`` says why — the
    caller gets an explicit gap, never a fabricated empty result masquerading as "no items". ``limited``
    means real data with a caveat (``reason`` describes it). Only ``supported``/``limited`` are ``ok``.
    """

    capability: SchoolCapability
    state: CapabilityState
    data: T | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.state in (CapabilityState.supported, CapabilityState.limited) and self.data is not None

    @classmethod
    def supported(cls, capability: SchoolCapability, data: T) -> ConnectorResult[T]:
        return cls(capability=capability, state=CapabilityState.supported, data=data)

    @classmethod
    def limited(cls, capability: SchoolCapability, data: T, reason: str) -> ConnectorResult[T]:
        return cls(capability=capability, state=CapabilityState.limited, data=data, reason=reason)

    @classmethod
    def unsupported(cls, capability: SchoolCapability, reason: str) -> ConnectorResult[T]:
        return cls(capability=capability, state=CapabilityState.unsupported, data=None, reason=reason)


# ---------------------------------------------------------------------------------------------------
# the Protocol every provider implements
# ---------------------------------------------------------------------------------------------------


@runtime_checkable
class SchoolConnector(Protocol):
    """Every school provider implements exactly this. Callers depend on the Protocol, never a class.

    All methods are read-only and return ``ConnectorResult`` envelopes over CANONICAL objects. A provider
    that cannot answer a capability returns an ``unsupported`` result — it does not raise, and it does not
    invent. ``capabilities()`` is the single place a caller checks support before building on top.
    """

    provider: str

    def capabilities(self) -> ProviderCapabilityMatrix: ...
    def supports(self, capability: SchoolCapability) -> bool: ...

    async def get_institution(self) -> ConnectorResult[Institution]: ...
    async def list_terms(self) -> ConnectorResult[list[AcademicTerm]]: ...
    async def list_courses(self) -> ConnectorResult[list[Course]]: ...
    async def list_instructors(self, *, course_id: str | None = None) -> ConnectorResult[list[Instructor]]: ...
    async def list_assignments(
        self, *, course_id: str | None = None,
        since: datetime.datetime | None = None, until: datetime.datetime | None = None,
    ) -> ConnectorResult[list[Assignment]]: ...
    async def get_assignment(self, assignment_id: str) -> ConnectorResult[Assignment | None]: ...
    async def get_submission(self, assignment_id: str) -> ConnectorResult[Submission | None]: ...
    async def list_materials(self, *, course_id: str | None = None) -> ConnectorResult[list[Material]]: ...
    async def list_announcements(self, *, course_id: str | None = None) -> ConnectorResult[list[Announcement]]: ...
    async def list_schedule_events(
        self, *, since: datetime.datetime | None = None, until: datetime.datetime | None = None,
    ) -> ConnectorResult[list[ScheduleEvent]]: ...
    async def sync(self, resource: str, cursor: SyncCursor | None = None) -> ConnectorResult[SyncPage]: ...


# ---------------------------------------------------------------------------------------------------
# provider-neutral due-state buckets — pure derivations over canonical assignments
# ---------------------------------------------------------------------------------------------------
#
# These are the framework's single, provider-neutral definition of "upcoming / overdue / undated /
# unsubmitted / graded". Both the Canvas contract test and the student query layer use THESE — so no
# adapter re-implements bucketing, and the buckets mean the same thing for every provider.


def is_undated(a: Assignment) -> bool:
    return a.due_at is None


def is_graded(a: Assignment) -> bool:
    return a.submission.kind is SubmissionStateKind.graded


def is_unsubmitted(a: Assignment) -> bool:
    """Nothing submitted and not graded. ``unknown`` state is NOT treated as unsubmitted (fail open,
    honestly) — we only claim work is missing when the provider actually says it wasn't turned in."""
    return a.submission.kind is SubmissionStateKind.none


def is_upcoming(a: Assignment, now: datetime.datetime) -> bool:
    """Has a due date in the future and is not already done (submitted/graded)."""
    if a.due_at is None:
        return False
    if a.submission.kind in (SubmissionStateKind.submitted, SubmissionStateKind.graded):
        return False
    return a.due_at >= now


def is_overdue(a: Assignment, now: datetime.datetime) -> bool:
    """Past its due date and still unsubmitted+ungraded — a genuine miss, not just a passed date."""
    if a.due_at is None:
        return False
    return a.due_at < now and is_unsubmitted(a)


def upcoming(assignments: list[Assignment], now: datetime.datetime) -> list[Assignment]:
    return sorted((a for a in assignments if is_upcoming(a, now)), key=lambda a: a.due_at)  # type: ignore[arg-type]


def overdue(assignments: list[Assignment], now: datetime.datetime) -> list[Assignment]:
    return sorted((a for a in assignments if is_overdue(a, now)), key=lambda a: a.due_at)  # type: ignore[arg-type]


def undated(assignments: list[Assignment]) -> list[Assignment]:
    return [a for a in assignments if is_undated(a)]


def unsubmitted(assignments: list[Assignment]) -> list[Assignment]:
    return [a for a in assignments if is_unsubmitted(a)]


def graded(assignments: list[Assignment]) -> list[Assignment]:
    return [a for a in assignments if is_graded(a)]
