"""Retrieval — the deterministic first stage, and an honest seam where the semantic stage will bolt on.

WHY THE FIRST STAGE IS NOT SEMANTIC. A vector search over a student's whole memory is the wrong shape for
the thing being asked. Nearly every real question is already scoped by something exact — the entity the
turn resolved to, the domain the mission is running in, what is live right now — and those scopes are
index lookups, not similarity. Starting with similarity means the guard rails (is this the right student,
is this record still true, did they ask me to forget it) become filters applied AFTER a ranking that has
already decided what matters, which is precisely the arrangement in which one of them eventually gets
dropped and nobody notices until a forgotten fact turns up in a reply.

So the order is fixed: scope, then liveness, then recency, then — if #122 supplies one — meaning.

THE SEMANTIC STAGE IS A PARAMETER, NOT A STUB. `facts(rerank=...)` takes a `Reranker`. There is no
placeholder implementation here pretending to rank, because a stub that returns its input unchanged is
indistinguishable from a working reranker in every test that does not specifically look for it, and it is
the kind of thing that ships. Without a reranker this module returns the deterministic shortlist, which
is a correct and useful answer. A reranker may REORDER and DROP; `_apply_rerank` refuses anything it did
not hand out, so the semantic stage can never widen the scope the deterministic stage decided on.

CROSS-USER RETRIEVAL IS NOT EXPRESSIBLE. Three independent mechanisms, because this is the one failure
with no acceptable rate:
  1. `MemoryRetriever.user_id` is a required constructor argument and the dataclass is frozen. There is
     no method on this class that accepts a user id, so a caller holding a retriever for student A has no
     API through which to name student B.
  2. Every query opens `db.user_session(self.user_id)`, which sets `app.user_id` transaction-locally, and
     every read filters `user_id == self.user_id` as well.
  3. `memory_records` runs FORCE ROW LEVEL SECURITY with the `tenant_isolation` policy (0028), so even a
     query that lost its WHERE clause returns only that student's rows, and the app role cannot bypass it.

FORGOTTEN AND SUPERSEDED ARE NOT REACHABLE. `memory_record.live_predicate()` is ANDed into every read,
superseded and contradicted rows are excluded, and a forgotten row has had its content erased — so even a
query that somehow reached one raises `RedactedMemory` in hydration rather than returning a hollow fact.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import sqlalchemy as sa

from . import memory_provenance as prov
from .db import user_session
from .memory_record import (
    FACTUAL, MEMORY_RECORDS, SELF, Freshness, MemoryKind, MemoryRecord, RedactedMemory, entity_key,
    from_row, live_predicate,
)

log = logging.getLogger("bruce.memory")   # CONTENT-FREE: counts, ids, domains — never subject or value

DEFAULT_LIMIT = 20
_OVERFETCH = 3
"""Expiry depends on the retention policy's half-life, which lives in Python (`memory_record.HALF_LIFE`).
Rather than restate those intervals in SQL — where they would drift the first time one is tuned — the
shortlist over-fetches, drops expired rows in Python, and trims. The cost is bounded and the benefit is
that there is exactly one definition of how fast a kind of memory goes stale."""


@dataclass(frozen=True)
class MemoryContext:
    """One memory, ready to be put in front of the model, with the things the model needs to use it
    responsibly: how sure Bruce is, where it came from, how old it is, and why it is here at all."""

    fact: str
    confidence: float
    source: prov.Provenance
    freshness: Freshness
    reason_it_matters_now: str
    """Captured at WRITE time (`MemoryProposal.reason_it_matters`) rather than generated at read time. A
    reason invented at retrieval is a second model output dressed up as a justification; this one is the
    caller's original claim about what the memory was for, and it can be checked against what happened."""

    @staticmethod
    def of(record: MemoryRecord, *, reason_it_matters: str, now: datetime) -> "MemoryContext":
        if record.kind not in FACTUAL:
            # The structural half of "style memory cannot surface in a factual query". Even if a query
            # lost its `kind IN FACTUAL` filter, a style record has no way to become a MemoryContext.
            raise ValueError(f"{record.kind.value} is not a factual memory and has no MemoryContext")
        return MemoryContext(
            fact=render_fact(record), confidence=record.confidence,
            source=prov.explain(record, now=now), freshness=record.freshness_at(now),
            reason_it_matters_now=reason_it_matters)


@dataclass(frozen=True)
class StyleSignal:
    """How the student writes. A separate return type from `MemoryContext` on purpose — the two never
    travel in the same list, so a caller cannot accidentally hand a style observation to something that
    treats its input as facts about a person."""

    relation: str
    value: str
    freshness: Freshness
    observed_at: datetime


def render_fact(record: MemoryRecord) -> str:
    """A stable canonical form: "you — teacher: ms delgado". Deliberately not a sentence. Turning a
    memory into English is presentation's job and it has a styling contract to obey
    (`conversation_style`); doing it here would put fact-bearing copy outside that contract."""
    subject = "you" if record.subject == SELF else record.subject
    relation = record.predicate.split(".", 1)[-1].replace("_", " ")
    return f"{subject} — {relation}: {record.value}"


Reranker = Callable[[str, Sequence[MemoryContext]], Sequence[MemoryContext]]
"""#122's seam. Given the turn's query text and the deterministic shortlist, return a subset in a new
order. It may drop and reorder; it may not add, and `_apply_rerank` enforces that."""


def _apply_rerank(query: str, shortlist: list[MemoryContext], rerank: Reranker) -> list[MemoryContext]:
    allowed = {id(c) for c in shortlist}
    ranked = list(rerank(query, tuple(shortlist)))
    smuggled = [c for c in ranked if id(c) not in allowed]
    if smuggled:
        # A reranker that can introduce rows has escaped the tenant scope, the liveness filter and the
        # forget check all at once. Refuse the whole result rather than serve a mixed one.
        raise ValueError("a reranker may reorder and drop, never add")
    return ranked


@dataclass(frozen=True)
class MemoryRetriever:
    """Everything one student can be told about themselves. Bound to that student at construction and
    unable to name another — see the module docstring."""

    user_id: UUID

    async def facts(self, *, entities: Sequence[str] = (), domains: Sequence[str] = (),
                    query: str = "", rerank: Reranker | None = None, limit: int = DEFAULT_LIMIT,
                    now: datetime | None = None) -> list[MemoryContext]:
        """The deterministic shortlist: scoped by entity and domain, live, most-recently-confirmed first.

        `entities` are subjects as the student refers to them; they are normalized through the same
        `entity_key` the writer used, so "Ms. Delgado" and "ms delgado" are one entity rather than two.
        """
        now = now or datetime.now(timezone.utc)
        rows = await self._shortlist(kinds=FACTUAL, entities=entities, domains=domains,
                                     limit=limit * _OVERFETCH)
        out: list[MemoryContext] = []
        for row in rows:
            try:
                rec = from_row(row)
            except RedactedMemory:
                # Unreachable through `live_predicate`; if it ever fires, a read path lost its filter and
                # this is the last thing standing between a forgotten memory and the model.
                log.warning("memory_redacted_row_reached_hydration id=%s", row.id)
                continue
            if not rec.is_live_at(now):
                continue
            out.append(MemoryContext.of(rec, reason_it_matters=row.reason_it_matters or "", now=now))
            if len(out) >= limit:
                break
        if rerank is not None:
            out = _apply_rerank(query, out, rerank)
        log.info("memory_shortlist n=%d domains=%s reranked=%s", len(out), list(domains),
                 rerank is not None)
        return out

    async def style(self, *, limit: int = DEFAULT_LIMIT,
                    now: datetime | None = None) -> list[StyleSignal]:
        """Style observations only. Returns `StyleSignal`, never `MemoryContext`."""
        now = now or datetime.now(timezone.utc)
        rows = await self._shortlist(kinds=frozenset({MemoryKind.style}), entities=(), domains=(),
                                     limit=limit)
        out: list[StyleSignal] = []
        for row in rows:
            try:
                rec = from_row(row)
            except RedactedMemory:
                continue
            if not rec.is_live_at(now):
                continue
            out.append(StyleSignal(relation=rec.predicate.split(".", 1)[-1], value=rec.value,
                                   freshness=rec.freshness_at(now), observed_at=rec.observed_at))
        return out

    async def record(self, memory_id: UUID) -> MemoryRecord | None:
        """One record by id, for correction and for answering "where did that come from". Returns None
        for a forgotten row: from the student's side there is nothing there any more."""
        async with user_session(self.user_id) as s:
            row = (await s.execute(sa.select(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.id == memory_id,
                MEMORY_RECORDS.c.user_id == self.user_id))).first()
        if row is None:
            return None
        try:
            return from_row(row)
        except RedactedMemory:
            return None

    async def _shortlist(self, *, kinds: frozenset[MemoryKind], entities: Sequence[str],
                         domains: Sequence[str], limit: int):
        """The single query. Every read in this module goes through it, so the tenant scope, the liveness
        filter and the supersession filter are written once and cannot be omitted by a new caller."""
        c = MEMORY_RECORDS.c
        where = [c.user_id == self.user_id,
                 live_predicate(),
                 c.superseded_by.is_(None),
                 c.contradicted_by.is_(None),
                 c.kind.in_([k.value for k in kinds])]
        if entities:
            where.append(c.entity_key.in_([entity_key(e) for e in entities]))
        if domains:
            where.append(c.domain.in_(list(domains)))
        stmt = (sa.select(MEMORY_RECORDS).where(sa.and_(*where))
                .order_by(sa.func.coalesce(c.last_confirmed_at, c.observed_at).desc(),
                          # A total order, so the same store answers the same question identically twice.
                          # Without the id tie-break, two facts confirmed in the same second reorder
                          # between calls and a shortlist cut at `limit` silently changes contents.
                          c.id.asc())
                .limit(limit))
        async with user_session(self.user_id) as s:
            return (await s.execute(stmt)).all()
