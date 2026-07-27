"""The typed-memory contract — what a remembered thing must carry before it is allowed to exist.

WHY MEMORY GETS A SCHEMA AND NOT A PROMPT. Every wrong belief Bruce holds is re-served. A bad action is
one bad action; a bad memory is a generator of them, because retrieval hands it to the model again on
every future turn and the model has no way to tell a stored fact from a stored mistake. The two failures
this programme has already measured both arrive here in a worse form:

  * SOMEONE ELSE'S WORDS READ AS THE STUDENT'S. The 238-case adversarial corpus that #120 was built
    against failed 38 times, and 30 of those were the same mistake: an affirmative living in a forwarded
    email or an attached screenshot was treated as the student speaking. `user_action_boundary` and
    `authorization_evidence` stop that from authorizing a write for one turn. Nothing stopped it from
    being LEARNED. "my teacher is Ms. Delgado", forwarded from a parent, is a sentence about someone
    else's conversation, and a memory row built from it is wrong forever.
  * A READING PRESENTED AS A FACT. The model is right about 0.94 of the open set. A store that accepts
    the model's conclusions as facts inherits that error rate permanently rather than per-turn.

So the shape below is deliberately hostile to both. `MemoryRecord` cannot be CONSTRUCTED without the
student's own trusted text as its source, cannot be constructed without the span it rests on, and cannot
be constructed as a fact by any derivation that means "Bruce worked it out". The refusal is in the type,
not in a check a caller can forget to call — and `0028_typed_memory` repeats it as a CHECK constraint so
a row written by hand, by a script, or by a future module still cannot say otherwise.

STYLE IS NOT A FACT. `MemoryKind.style` records HOW the student writes. It is a separate kind from the
five factual layers, `FACTUAL` excludes it, and `MemoryContext` (the only thing a factual query returns)
refuses to be built from it. That separation is the structural answer to inferring identity from
vocabulary: a stylistic observation has no path by which it can become a claim about who someone is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from . import decision_resolver

# --- the six layers --------------------------------------------------------------------------------


class MemoryKind(str, Enum):
    """The six memory layers. `kind` is the layer — a record is in exactly one, for its whole life.

    Kept as one enum rather than a layer field plus a kind field because a record that could change
    layers is a record whose retrieval scope can change after it was written, and consent was given
    against what the memory WAS.
    """

    profile = "profile"
    """Durable facts about the student themselves: their school, their grade, their instrument."""

    relationships = "relationships"
    """Who the people in their life are and what those people are TO them — coach, teacher, sister."""

    world = "world"
    """The student's environment rather than the student: how their school's block schedule runs, when
    their season starts. Still user-specific — general knowledge is not a memory, it is a lookup."""

    entity = "entity"
    """Facts attached to a resolved thing in their world (a course, a team, a recurring event), keyed by
    the same entity the calendar and school stacks resolve to."""

    episodic = "episodic"
    """What happened, and when. Short-lived by nature; this is the layer that is supposed to go stale."""

    style = "style"
    """How the student writes. NOT a fact about them, and structurally never returned as one."""


FACTUAL: frozenset[MemoryKind] = frozenset({
    MemoryKind.profile, MemoryKind.relationships, MemoryKind.world,
    MemoryKind.entity, MemoryKind.episodic,
})
"""The five layers a factual query may draw from. `style` is deliberately absent, and every factual
retrieval path filters on this set rather than on `!= style`, so adding a sixth factual layer later is an
edit here instead of a silent widening of what a style record can reach."""


# --- provenance types ------------------------------------------------------------------------------


class SourceType(str, Enum):
    """Where the words came from. Only the first one may create a memory; see `MemoryRecord`."""

    trusted_user_text = "trusted_user_text"
    """The student's own words, after `decision_resolver.trusted_reply_text` has removed quoted and
    forwarded regions. The only thing in this enum that can become a memory."""

    quoted = "quoted"
    forwarded = "forwarded"
    attachment = "attachment"
    provider = "provider"
    """Synced from Gmail/Calendar/a school connector. Authoritative about the PROVIDER, not about the
    student, and it already has its own tables — it enters memory only when the student says something
    about it in their own words."""

    model = "model"
    """Bruce's own reading. A proposal, never an observation."""


TRUSTED_FOR_MEMORY: frozenset[SourceType] = frozenset({SourceType.trusted_user_text})
EVIDENCE_ONLY: frozenset[SourceType] = frozenset(SourceType) - TRUSTED_FOR_MEMORY
"""Recordable as evidence, never as the source of a memory. A coach's email may sit in `Evidence.
corroboration` forever — it explains what the student was looking at when they told Bruce something. It
never explains why Bruce believes the thing."""


class Basis(str, Enum):
    """How the record came to be believed.

    NOTE WHAT IS MISSING: there is no `inferred`. A guess has no spelling here, which is what makes "a
    guess is never stored as a fact" a property of the type rather than a rule someone remembers to
    check. An uncertain reading of something the student DID say is still `stated` — it carries a
    confidence below 1.0 and `hedged=True`, and it still has to point at the words.
    """

    stated = "stated"
    corrected = "corrected"
    confirmed = "confirmed"


@dataclass(frozen=True)
class EvidenceRef:
    """Material that was PRESENT when a memory was written. Never a reason to believe it.

    This is where a forwarded email or an attached screenshot is allowed to live: "the student said this
    while looking at that". It is recorded so provenance can show the student the whole picture, and it
    is structurally inert — nothing reads `source_type` here to decide trust.
    """

    source_type: SourceType
    source_message_id: str | None = None
    excerpt: str | None = None

    def __post_init__(self) -> None:
        if self.source_type is SourceType.trusted_user_text:
            # The student's own words are the record's SOURCE, not corroboration for it. Allowing them
            # here would create a second, unchecked channel by which trusted text enters a record.
            raise ValueError("trusted_user_text is a source, not corroborating evidence")

    def to_json(self) -> dict:
        return {"source_type": self.source_type.value, "source_message_id": self.source_message_id,
                "excerpt": self.excerpt}


@dataclass(frozen=True)
class Evidence:
    """Why Bruce believes this. Required on every record — a memory with no evidence is a rumour."""

    stated_span: str
    """The substring of the student's OWN trusted text this memory rests on. Verified against that text
    at write time (`memory_writer._grounded`), not merely asserted."""

    basis: Basis = Basis.stated
    hedged: bool = False
    """The student said it, but not flatly ("i think it's ms delgado"). Drives confidence and is what
    `memory_provenance` shows when the student asks why Bruce is unsure."""

    corroboration: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if not (self.stated_span or "").strip():
            raise ValueError("evidence requires the span of the student's own words it rests on")

    def to_json(self) -> dict:
        return {"stated_span": self.stated_span, "basis": self.basis.value, "hedged": self.hedged,
                "corroboration": [c.to_json() for c in self.corroboration]}

    @staticmethod
    def from_json(blob: dict | str | None) -> "Evidence":
        d = json.loads(blob) if isinstance(blob, str) else (blob or {})
        return Evidence(
            stated_span=d.get("stated_span", ""),
            basis=Basis(d.get("basis", Basis.stated.value)),
            hedged=bool(d.get("hedged", False)),
            corroboration=tuple(
                EvidenceRef(SourceType(c["source_type"]), c.get("source_message_id"), c.get("excerpt"))
                for c in d.get("corroboration", []) or []),
        )


# --- sensitivity + retention -----------------------------------------------------------------------


class Sensitivity(str, Enum):
    ordinary = "ordinary"
    """Their chemistry teacher's name. Freely usable in a reply."""

    private = "private"
    """True and theirs, but not something to volunteer — a grade, a conflict with a friend. Retrieval
    returns it; presentation decides whether saying it out loud is warranted."""

    sensitive = "sensitive"
    """The identity traits in `SENSITIVE_TRAITS`. `memory_writer` will not write one at all, so this
    value exists to make the refusal legible and to keep the taxonomy in one place."""


class RetentionPolicy(str, Enum):
    """How long a memory is worth anything. Independent of forgetting, which is the student's call and
    always wins (`memory_correction.MemoryForget`)."""

    durable = "durable"
    school_year = "school_year"
    season = "season"
    episodic = "episodic"
    transient = "transient"


HALF_LIFE: dict[RetentionPolicy, timedelta | None] = {
    RetentionPolicy.durable: None,                    # a sibling's name does not expire
    RetentionPolicy.school_year: timedelta(days=300),
    RetentionPolicy.season: timedelta(days=120),
    RetentionPolicy.episodic: timedelta(days=30),
    RetentionPolicy.transient: timedelta(days=7),
}

DURABLE_LAYERS: frozenset[MemoryKind] = frozenset({MemoryKind.profile, MemoryKind.relationships})
"""Layers whose whole purpose is to outlive the conversation. A `transient` retention on one of these is
a contradiction the writer refuses, because a profile fact that evaporates in a week is worse than no
profile fact: the student is never told it went away."""


class Freshness(str, Enum):
    current = "current"
    aging = "aging"
    stale = "stale"
    expired = "expired"
    """Past twice its half-life. Excluded from retrieval — not deleted, because the student may still ask
    what Bruce used to think, and because expiry is a guess about relevance, not about truth."""


def compute_freshness(*, observed_at: datetime, last_confirmed_at: datetime | None,
                      retention_policy: RetentionPolicy, now: datetime) -> Freshness:
    """Age against the policy's half-life. Pure; `now` is a parameter so this is testable without sleeping.

    Measured from the LAST CONFIRMATION where there is one. A fact the student repeated in March is not
    six months old just because they first said it in September — treating it that way is how a memory
    system quietly stops trusting things the student keeps telling it.
    """
    half_life = HALF_LIFE[retention_policy]
    if half_life is None:
        return Freshness.current
    age = now - (last_confirmed_at or observed_at)
    if age < half_life / 2:
        return Freshness.current
    if age < half_life:
        return Freshness.aging
    if age < half_life * 2:
        return Freshness.stale
    return Freshness.expired


# --- sensitive identity traits -----------------------------------------------------------------------

SENSITIVE_TRAITS: frozenset[str] = frozenset({"race", "sex", "gender", "faith", "visa", "household"})
SENSITIVE_TRAIT_STEMS: tuple[str, ...] = (
    "gender", "sexual", "orientation", "religio", "racial", "ethnic", "health", "diagnos", "disab",
    "medicat", "immigrat", "citizen", "nationalit", "famil",
)
"""The traits `memory_writer` refuses outright, matched on the tokens of a namespaced predicate — exact
for the short ones, stems for the rest, so `profile.religion` and `profile.religious_observance` are the
same refusal and a caller cannot get through by rewording the label.

It over-blocks in places (`sports.race_time` is refused), and that direction is chosen: a refused memory
costs the student one re-ask, while a stored one is a trait on file that Bruce had no reason to keep.

THIS IS THE SECOND NET, NOT THE FIRST. The structural guarantee that a stylistic signal cannot produce a
claim about identity is the kind separation: `record_style_signal` is the only door style observations
come through, it hard-codes `MemoryKind.style`, and a style record can never be returned by a factual
query. No vocabulary list is load-bearing for that. This taxonomy catches the different mistake — a
caller with a genuine fact write who labels it `profile.religion` — and it is a taxonomy of TRAITS, so it
does not care what words the student used or which language they used them in.
"""


# --- the record ---------------------------------------------------------------------------------------

_PREDICATE = re.compile(r"^[a-z][a-z0-9_]{1,31}\.[a-z][a-z0-9_]{1,63}$")
"""Predicates are namespaced `domain.relation` ("school.teacher", "music.instrument"). The namespace is
the retrieval domain — one string, so a shortlist by domain cannot drift out of sync with a separate
column someone forgot to set — and requiring the shape is also the cheapest filler filter there is:
conversational noise has no relation to name."""

_WS = re.compile(r"\s+")
MAX_VALUE = 300
"""A memory is a fact, not a transcript. Anything longer is a paragraph someone wanted to keep, and
keeping paragraphs is what turns a memory store into an un-auditable second copy of the inbox."""


class RedactedMemory(Exception):
    """Raised when a forgotten row is hydrated. A forgotten record has no content left to return; this
    fires only if a query reached one, which means a liveness filter was dropped."""


@dataclass(frozen=True)
class MemoryRecord:
    """One thing Bruce believes, with everything needed to defend it.

    Frozen, and history is never edited: a correction writes a NEW record and points the old one at it
    (`superseded_by`). The only mutable columns in the table are the four lifecycle ones, and
    `0028_typed_memory` enforces that with a BEFORE UPDATE trigger — so "corrections supersede, never
    mutate" survives a caller who writes raw SQL.
    """

    memory_id: UUID
    user_id: UUID
    kind: MemoryKind
    subject: str
    predicate: str
    value: str
    evidence: Evidence
    source_message_id: str | None
    source_type: SourceType
    confidence: float
    observed_at: datetime
    last_confirmed_at: datetime | None
    retention_policy: RetentionPolicy
    sensitivity: Sensitivity
    user_editable: bool
    contradicted_by: UUID | None
    superseded_by: UUID | None
    freshness: Freshness = field(init=False)
    """Computed at hydration, never stored. A stored freshness is false the moment the clock moves, and a
    column that is usually wrong is worse than no column. `freshness_at` recomputes for a given `now`."""

    def __post_init__(self) -> None:
        if self.source_type not in TRUSTED_FOR_MEMORY:
            # The 30-of-38 defect, at the type. A forwarded "my teacher is Ms. Delgado" cannot be spelled
            # as a memory at all — there is no argument combination that produces this object.
            raise ValueError(f"{self.source_type.value} may be evidence, never the source of a memory")
        if not (self.subject or "").strip() or not (self.value or "").strip():
            raise ValueError("a memory needs a subject and a value")
        if not _PREDICATE.match(self.predicate or ""):
            raise ValueError(f"predicate must be namespaced domain.relation, got {self.predicate!r}")
        if len(self.value) > MAX_VALUE:
            raise ValueError(f"value exceeds {MAX_VALUE} chars — that is a transcript, not a memory")
        if not isinstance(self.evidence, Evidence):
            raise ValueError("evidence must be an Evidence, not a free-form blob")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence is a probability")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.superseded_by == self.memory_id:
            raise ValueError("a memory cannot supersede itself")
        object.__setattr__(self, "freshness", self.freshness_at(datetime.now(timezone.utc)))

    def freshness_at(self, now: datetime) -> Freshness:
        return compute_freshness(observed_at=self.observed_at, last_confirmed_at=self.last_confirmed_at,
                                 retention_policy=self.retention_policy, now=now)

    @property
    def is_factual(self) -> bool:
        return self.kind in FACTUAL

    @property
    def domain(self) -> str:
        return domain_of(self.predicate)

    @property
    def entity_key(self) -> str:
        return entity_key(self.subject)

    def is_live_at(self, now: datetime) -> bool:
        """Retrievable: not replaced, not contradicted out of existence, not past its usefulness. A
        forgotten record can never reach this — it has no content and cannot be hydrated at all."""
        return (self.superseded_by is None and self.contradicted_by is None
                and self.freshness_at(now) is not Freshness.expired)


def normalize(text: str | None) -> str:
    """The one normalizer, borrowed from `decision_resolver` so memory and the boundary agree on what two
    strings being "the same" means. Reimplementing it is how a smart quote becomes a duplicate fact."""
    return _WS.sub(" ", decision_resolver.normalize(text)).strip()


def entity_key(subject: str | None) -> str:
    """The deterministic shortlist key for a subject. Denormalized into its own indexed column because
    the first retrieval stage has to be an index scan, not a scan plus a Python normalize."""
    return normalize(subject)[:200]


def domain_of(predicate: str | None) -> str:
    return (predicate or "").split(".", 1)[0]


def is_namespaced_predicate(predicate: str | None) -> bool:
    return bool(_PREDICATE.match(predicate or ""))


SELF = "self"
"""The subject of every `profile` fact. A literal so "me", "myself" and "i" cannot become three
different students inside one account's memory."""


# --- storage -------------------------------------------------------------------------------------------
# A private MetaData, not `schema.Base`. This table is queried, never created from Python: DDL lives in
# 0028_typed_memory and nowhere else. Foreign keys are declared there too — repeating them here would
# just give SQLAlchemy a second, unresolvable opinion about a table it will never emit.

_METADATA = sa.MetaData()

MEMORY_RECORDS = sa.Table(
    "memory_records", _METADATA,
    sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
    sa.Column("user_id", PGUUID(as_uuid=True), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("subject", sa.String(200)),
    sa.Column("predicate", sa.String(100)),
    sa.Column("value", sa.String(MAX_VALUE)),
    sa.Column("evidence", JSONB),
    sa.Column("source_message_id", sa.String(255)),
    sa.Column("source_type", sa.String(32), nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_confirmed_at", sa.DateTime(timezone=True)),
    sa.Column("retention_policy", sa.String(16), nullable=False),
    sa.Column("sensitivity", sa.String(16), nullable=False),
    sa.Column("user_editable", sa.Boolean, nullable=False),
    sa.Column("contradicted_by", PGUUID(as_uuid=True)),
    sa.Column("superseded_by", PGUUID(as_uuid=True)),
    # The future question this memory answers, captured at write time because it cannot be recovered
    # later. It is what `MemoryContext.reason_it_matters_now` is filled from, and it is redacted on
    # forget along with the rest of the content — "so Bruce knows who her therapist is" is itself content.
    sa.Column("reason_it_matters", sa.String(300)),
    # Denormalized shortlist keys, derived from subject/predicate by the writer and by nothing else.
    sa.Column("entity_key", sa.String(200)),
    sa.Column("domain", sa.String(32)),
    # Forgetting. The content columns above are NULLed in the same statement that sets these, and a CHECK
    # constraint refuses any row that is forgotten and still has content — see the migration.
    sa.Column("forgotten_at", sa.DateTime(timezone=True)),
    sa.Column("forget_scope", sa.String(16)),
    sa.Column("created_at", sa.DateTime(timezone=True)),
)

_LIVE_COLUMNS = (MEMORY_RECORDS.c.forgotten_at.is_(None),)


def live_predicate():
    """The clause every read path ANDs in. Exported as one object so there is exactly one definition of
    "a row that still exists", and so a new query cannot accidentally ship without it."""
    return sa.and_(*_LIVE_COLUMNS)


def to_row(rec: MemoryRecord) -> dict:
    return {
        "id": rec.memory_id, "user_id": rec.user_id, "kind": rec.kind.value,
        "subject": rec.subject, "predicate": rec.predicate, "value": rec.value,
        "evidence": rec.evidence.to_json(), "source_message_id": rec.source_message_id,
        "source_type": rec.source_type.value, "confidence": rec.confidence,
        "observed_at": rec.observed_at, "last_confirmed_at": rec.last_confirmed_at,
        "retention_policy": rec.retention_policy.value, "sensitivity": rec.sensitivity.value,
        "user_editable": rec.user_editable, "contradicted_by": rec.contradicted_by,
        "superseded_by": rec.superseded_by, "entity_key": rec.entity_key, "domain": rec.domain,
    }


def from_row(row) -> MemoryRecord:
    """Hydrate. Raises `RedactedMemory` on a forgotten row rather than returning a hollow record — a
    half-empty memory that reads as "Bruce knows something here" is exactly what forgetting must not
    leave behind."""
    if row.forgotten_at is not None or row.value is None:
        raise RedactedMemory(f"memory {row.id} was forgotten")
    observed = row.observed_at
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return MemoryRecord(
        memory_id=row.id, user_id=row.user_id, kind=MemoryKind(row.kind), subject=row.subject,
        predicate=row.predicate, value=row.value, evidence=Evidence.from_json(row.evidence),
        source_message_id=row.source_message_id, source_type=SourceType(row.source_type),
        confidence=row.confidence, observed_at=observed, last_confirmed_at=row.last_confirmed_at,
        retention_policy=RetentionPolicy(row.retention_policy),
        sensitivity=Sensitivity(row.sensitivity), user_editable=row.user_editable,
        contradicted_by=row.contradicted_by, superseded_by=row.superseded_by)
