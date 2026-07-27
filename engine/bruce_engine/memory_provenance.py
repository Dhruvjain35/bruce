"""Provenance — every stored memory answers "where did that come from" and "why did you think that".

WHY THIS IS A MODULE AND NOT A LOG LINE. A student who is told something wrong about themselves has
exactly one useful question: where did you get that. If the answer has to be reconstructed from an audit
table, a message archive and a model trace, then in practice it is never answered — the reconstruction is
slow, it happens offline, and it is unavailable at the moment the student is actually asking. Worse, a
memory whose origin can only be found by joining three tables is a memory that CANNOT BE CORRECTED
safely, because nothing can tell whether two rows came from the same claim or two different ones.

So provenance is a property of the record itself, computed from fields the record already carries and
nothing else. `explain` opens no session and takes no ids: hand it a `MemoryRecord` and it can answer.
That constraint is the point — it is what guarantees the answer survives a table being renamed, a log
being rotated, or a message the student deleted from their phone.

WHAT IT WILL NOT DO. It never quotes the corroborating material. A forwarded email in
`Evidence.corroboration` is named ("something you forwarded") and never reproduced, because the student
is entitled to know Bruce saw it without Bruce reciting someone else's message back at them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .memory_record import Basis, Freshness, MemoryRecord, SourceType

_SOURCE_NAMES: dict[SourceType, str] = {
    SourceType.trusted_user_text: "something you told me",
    SourceType.quoted: "text quoted inside a message",
    SourceType.forwarded: "a message you forwarded",
    SourceType.attachment: "a file or screenshot you sent",
    SourceType.provider: "a synced account",
    SourceType.model: "my own reading of a conversation",
}

_FRESHNESS_NOTES: dict[Freshness, str] = {
    Freshness.current: "recent enough that I'd act on it",
    Freshness.aging: "a while ago — worth checking if it still holds",
    Freshness.stale: "old; I'd ask before relying on it",
    Freshness.expired: "too old to use, kept only so you can see what I used to think",
}


@dataclass(frozen=True)
class Provenance:
    """One memory's full account of itself. Everything here is derived from the record; nothing is looked
    up, so a `Provenance` is as durable as the row it came from."""

    memory_id: str
    origin: str
    """Plain-language answer to "where did that come from"."""

    stated_span: str
    """The student's own words the memory rests on. Their words, not a paraphrase — a paraphrase is
    another chance to be wrong about what they said."""

    source_message_id: str | None
    observed_at: datetime
    last_confirmed_at: datetime | None
    basis: Basis
    hedged: bool
    confidence: float
    freshness: Freshness
    corroboration: tuple[str, ...]
    """Material that was PRESENT, named and never quoted. Present here so the student can see the whole
    picture, and so it is visibly separate from the thing that actually justified the memory."""

    superseded_by: str | None
    contradicted_by: str | None

    def where_did_that_come_from(self) -> str:
        when = self.observed_at.date().isoformat()
        line = f'{self.origin} on {when}: "{self.stated_span}"'
        if self.last_confirmed_at is not None:
            line += f" (you confirmed it again on {self.last_confirmed_at.date().isoformat()})"
        if self.corroboration:
            line += f" — you were also showing me {', '.join(self.corroboration)}"
        return line

    def why_did_you_think_that(self) -> str:
        if self.basis is Basis.corrected:
            reason = "you corrected me, and this is what you said instead"
        elif self.basis is Basis.confirmed:
            reason = "you told me, and later confirmed it still holds"
        elif self.hedged:
            reason = "you said it, but not flatly — so I kept it as a maybe, not a fact"
        else:
            reason = "you said it directly"
        note = _FRESHNESS_NOTES[self.freshness]
        line = f"{reason}. I'm {self.confidence:.0%} sure, and it's {note}"
        if self.superseded_by:
            line += f". This has since been replaced by {self.superseded_by}"
        elif self.contradicted_by:
            line += f". Something you said later contradicts it ({self.contradicted_by})"
        return line


def explain(record: MemoryRecord, *, now: datetime | None = None) -> Provenance:
    """The record's account of itself. No I/O, by design — see the module docstring."""
    now = now or datetime.now(timezone.utc)
    ev = record.evidence
    return Provenance(
        memory_id=str(record.memory_id),
        origin=_SOURCE_NAMES[record.source_type],
        stated_span=ev.stated_span,
        source_message_id=record.source_message_id,
        observed_at=record.observed_at,
        last_confirmed_at=record.last_confirmed_at,
        basis=ev.basis,
        hedged=ev.hedged,
        confidence=record.confidence,
        freshness=record.freshness_at(now),
        corroboration=tuple(_SOURCE_NAMES[c.source_type] for c in ev.corroboration),
        superseded_by=str(record.superseded_by) if record.superseded_by else None,
        contradicted_by=str(record.contradicted_by) if record.contradicted_by else None,
    )
