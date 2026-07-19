"""Student query layer over the canonical academic graph (P1 north-star queries).

Answers the four questions the whole product is organized around — "what do I have due?", "what changed?",
"what am I missing?", "what should I do next?" — over the PERSISTED canonical objects, so every answer is
grounded: each item carries its provider id, original URL, source + last-sync timestamps, evidence link,
change history, and the capability state it was produced under. No item is model prose or an unsourced
guess; if it's in an answer, you can open the real thing.

Provider-neutral by construction: this module imports the canonical contract + the store only. It never
imports ``canvas_fake`` or names Canvas — swap in a Google Classroom / OneRoster connector and these
queries are unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime

from uuid import UUID

from . import school_connector as sc
from . import school_store


def _now(now: datetime.datetime | None) -> datetime.datetime:
    return now or datetime.datetime.now(datetime.timezone.utc)


def _days_between(a: datetime.datetime, b: datetime.datetime) -> int:
    return abs((a - b).days)


# ---------------------------------------------------------------------------------------------------
# result items — each carries the full canonical object, so provenance travels with every answer
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class DueQueryItem:
    """One assignment in a due-style answer, tagged with the bucket + a human reason. ``provenance`` is
    the canonical object's own provenance block (provider id, URL, timestamps, evidence, change history,
    capability state) — present on every item, never stripped."""

    bucket: str                    # upcoming | overdue | undated | do_next
    reason: str
    assignment: sc.Assignment

    @property
    def provenance(self) -> sc.Provenance:
        return self.assignment.provenance


@dataclasses.dataclass
class ChangeQueryItem:
    """One change in a "what changed?" answer. For a create/update the current canonical ``object`` and
    its ``provenance`` (with full change history) are present; for a delete the object is gone, so we
    honestly return only its provider id + original URL + the delete record — never a fabricated object."""

    object_type: str
    provider_id: str
    change_type: sc.ChangeType
    reason: str
    detected_at: datetime.datetime
    changed_fields: list[str]
    source_url: str | None
    object: sc.Course | sc.Assignment | sc.Announcement | None
    provenance: sc.Provenance | None


# ---------------------------------------------------------------------------------------------------
# 1. what do I have due?
# ---------------------------------------------------------------------------------------------------


async def what_is_due(user_id: UUID, *, now: datetime.datetime | None = None,
                      within_days: int | None = None, provider: str = "canvas") -> list[DueQueryItem]:
    """Upcoming, not-yet-done assignments with a real due date, soonest first. Optionally capped to a
    window (``within_days``). Undated work is NOT force-fit here — it surfaces in ``what_am_i_missing``."""
    now = _now(now)
    assignments = await school_store.list_assignments(user_id, provider=provider)
    items: list[DueQueryItem] = []
    for a in sc.upcoming(assignments, now):
        assert a.due_at is not None  # upcoming() guarantees a due date
        days = _days_between(a.due_at, now)
        if within_days is not None and (a.due_at - now).days > within_days:
            continue
        reason = "due today" if days == 0 else (f"due in {days} day" + ("s" if days != 1 else ""))
        items.append(DueQueryItem(bucket="upcoming", reason=reason, assignment=a))
    return items


# ---------------------------------------------------------------------------------------------------
# 2. what changed at school?
# ---------------------------------------------------------------------------------------------------


# "What changed at school?" means the content a student cares about — new/updated assignments, new
# announcements, course changes — not dimension churn (a term or instructor record being refreshed).
_CONTENT_OBJECT_TYPES = ("course", "assignment", "announcement")


async def what_changed(user_id: UUID, *, since: datetime.datetime | None = None,
                       provider: str = "canvas") -> list[ChangeQueryItem]:
    """Classified changes to courses/assignments/announcements (newest first), optionally since a timestamp.
    Loads the current canonical object for creates/updates so its full provenance + change history rides
    along; deletes carry the original URL + delete record only (the object no longer exists — stated
    honestly, not invented)."""
    changes = [c for c in await school_store.changes_since(user_id, since=since, provider=provider)
               if c.object_type in _CONTENT_OBJECT_TYPES]
    # index current objects by (type, provider_id) so each change resolves to its live object
    by_key: dict[tuple[str, str], sc.Course | sc.Assignment | sc.Announcement] = {}
    for c in await school_store.list_courses(user_id, provider=provider):
        by_key[("course", c.provider_id)] = c
    for a in await school_store.list_assignments(user_id, provider=provider):
        by_key[("assignment", a.provider_id)] = a
    for an in await school_store.list_announcements(user_id, provider=provider):
        by_key[("announcement", an.provider_id)] = an

    out: list[ChangeQueryItem] = []
    for ch in changes:
        obj = by_key.get((ch.object_type, ch.provider_id))
        if ch.change_type is sc.ChangeType.created:
            reason = f"new {ch.object_type}"
        elif ch.change_type is sc.ChangeType.updated:
            reason = f"updated {ch.object_type}" + (f" ({', '.join(ch.changed_fields)})" if ch.changed_fields else "")
        else:
            reason = f"removed {ch.object_type}"
        out.append(ChangeQueryItem(
            object_type=ch.object_type, provider_id=ch.provider_id, change_type=ch.change_type, reason=reason,
            detected_at=ch.detected_at, changed_fields=ch.changed_fields, source_url=ch.source_url,
            object=obj, provenance=obj.provenance if obj is not None else None))
    return out


# ---------------------------------------------------------------------------------------------------
# 3. what am I missing?
# ---------------------------------------------------------------------------------------------------


async def what_am_i_missing(user_id: UUID, *, now: datetime.datetime | None = None,
                            provider: str = "canvas") -> list[DueQueryItem]:
    """The honest gap: overdue-and-unsubmitted work first, then undated-and-unsubmitted work. Graded and
    submitted assignments are never here (they aren't missing); a past-due date alone is not "missing" if
    it was turned in. Most-overdue first."""
    now = _now(now)
    assignments = await school_store.list_assignments(user_id, provider=provider)
    items: list[DueQueryItem] = []
    for a in sc.overdue(assignments, now):
        assert a.due_at is not None
        days = _days_between(now, a.due_at)
        reason = f"overdue by {days} day" + ("s" if days != 1 else "") + " and not submitted"
        items.append(DueQueryItem(bucket="overdue", reason=reason, assignment=a))
    for a in sc.undated(assignments):
        if sc.is_unsubmitted(a):
            items.append(DueQueryItem(bucket="undated", reason="no due date and not submitted", assignment=a))
    return items


# ---------------------------------------------------------------------------------------------------
# 4. what should I do next?
# ---------------------------------------------------------------------------------------------------


async def what_should_i_do_next(user_id: UUID, *, now: datetime.datetime | None = None,
                                provider: str = "canvas", limit: int | None = None) -> list[DueQueryItem]:
    """A single prioritized to-do list: overdue (most urgent) -> due soonest -> undated. One canonical,
    provider-neutral ranking so the answer is stable regardless of which provider the data came from."""
    now = _now(now)
    assignments = await school_store.list_assignments(user_id, provider=provider)

    ranked: list[DueQueryItem] = []
    for a in sc.overdue(assignments, now):
        assert a.due_at is not None
        days = _days_between(now, a.due_at)
        ranked.append(DueQueryItem(bucket="do_next", reason=f"overdue by {days}d — do this first", assignment=a))
    for a in sc.upcoming(assignments, now):
        assert a.due_at is not None
        days = _days_between(a.due_at, now)
        when = "due today" if days == 0 else f"due in {days}d"
        ranked.append(DueQueryItem(bucket="do_next", reason=f"{when}", assignment=a))
    for a in sc.undated(assignments):
        if sc.is_unsubmitted(a):
            ranked.append(DueQueryItem(bucket="do_next", reason="no due date — fit in when you can", assignment=a))

    return ranked[:limit] if limit is not None else ranked


# ---------------------------------------------------------------------------------------------------
# capability transparency — surface what the provider could/couldn't answer for a query
# ---------------------------------------------------------------------------------------------------


def unsupported_note(summary: school_store.SyncSummary) -> str | None:
    """A plain-language note about any capability the provider declined during the last sync, so the UI can
    say "your school doesn't expose X" instead of silently showing nothing. None when nothing was declined."""
    if not summary.unsupported:
        return None
    return "Some data is unavailable from your school provider: " + ", ".join(sorted(set(summary.unsupported)))
