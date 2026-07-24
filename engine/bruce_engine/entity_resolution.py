"""Reference resolution (R7) — turn "move chess class", "delete that", "the one tomorrow" into a canonical
CalendarEventEntity. Owner-scoped; NEVER crosses users. Fails CLOSED when two candidates remain genuinely
ambiguous (returns ambiguous with the candidates so the caller asks one precise question) rather than
guessing and mutating the wrong event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from . import entity_store

# A generic pointer with no title ("that event", "it", "the one", "that thing").
_GENERIC_REF = re.compile(r"\b(that|this|it|the)\s+(event|one|thing|meeting|class|appointment)\b|\b(it|that)\b",
                          re.IGNORECASE)


@dataclass
class ResolutionResult:
    status: str                       # "resolved" | "ambiguous" | "not_found"
    entity: dict | None = None
    candidates: list[dict] = field(default_factory=list)


def _score(entity_tokens: set[str], msg_tokens: set[str]) -> tuple[int, float]:
    overlap = entity_tokens & msg_tokens
    return len(overlap), (len(overlap) / len(entity_tokens) if entity_tokens else 0.0)


async def resolve(user_id: UUID, text: str | None) -> ResolutionResult:
    """Best entity for a reference in `text`, or ambiguous/not_found. Title-token overlap first; a bare
    generic pointer falls back to the single active event (ambiguous if there are several)."""
    events = await entity_store.active_events(user_id)
    if not events:
        return ResolutionResult("not_found")
    msg = set(entity_store.normalize_title(text).split())

    scored = []
    for e in events:
        et = set((e.get("normalized_title") or "").split())
        n, ratio = _score(et, msg)
        if n:
            scored.append((e, n, ratio))
    if scored:
        scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
        top = (scored[0][1], scored[0][2])
        tied = [e for e, n, r in scored if (n, r) == top]
        if len(tied) == 1:
            return ResolutionResult("resolved", entity=tied[0])
        return ResolutionResult("ambiguous", candidates=tied)     # fail closed: which one?

    if _GENERIC_REF.search(text or ""):
        if len(events) == 1:
            return ResolutionResult("resolved", entity=events[0])
        return ResolutionResult("ambiguous", candidates=events[:4])
    return ResolutionResult("not_found")


async def resolve_most_recent(user_id: UUID) -> ResolutionResult:
    """The most-recently-created active event — the referent of a bare correction ("not today, i said
    4 days from now") that points at the operation just performed."""
    events = await entity_store.active_events(user_id, limit=1)
    if not events:
        return ResolutionResult("not_found")
    return ResolutionResult("resolved", entity=events[0])
