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
import difflib

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


# ---------------------------------------------------------------------------------------------------
# 5. did I submit this?
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class SubmitCheckItem:
    """The answer to "did I submit this?" for one referenced assignment, grounded in the persisted
    ``SubmissionState``. ``answer`` is deliberately three-valued — ``"yes"`` / ``"no"`` / ``"unknown"`` —
    because the honest opposite of "submitted" is *not* "no" when the provider never reported a state:
    ``unknown`` stays ``unknown`` and is never collapsed to "no". When an assignment matched, its full
    ``provenance`` rides along (via the assignment); when nothing matched, we say so rather than guess."""

    query: str                                   # the reference the student asked about (id or title)
    matched: bool                                # did a persisted assignment match the reference?
    answer: str                                  # "yes" | "no" | "unknown"
    submission_state: sc.SubmissionStateKind | None
    reason: str
    match_kind: str | None                       # how it matched: id | title | title-contains | nearest-title
    assignment: sc.Assignment | None

    @property
    def provenance(self) -> sc.Provenance | None:
        return self.assignment.provenance if self.assignment is not None else None


def _answer_for_state(kind: sc.SubmissionStateKind) -> tuple[str, str]:
    """Map a canonical submission state to a three-valued answer + a human reason. ``unknown`` is honest:
    it is NOT reported as "no" — the provider simply did not tell us whether it was turned in."""
    if kind is sc.SubmissionStateKind.graded:
        return "yes", "submitted and graded"
    if kind is sc.SubmissionStateKind.submitted:
        return "yes", "submitted, not yet graded"
    if kind is sc.SubmissionStateKind.none:
        return "no", "not submitted"
    return "unknown", "your school didn't report a submission state for this — unknown, not 'no'"


def _match_assignment(assignments: list[sc.Assignment], *, assignment_id: str | None,
                      title: str | None) -> tuple[sc.Assignment | None, str | None]:
    """Resolve an assignment reference to a single persisted assignment. An ``assignment_id`` is matched
    exactly on the provider id; a ``title`` is matched nearest-first — exact (case-insensitive), then a
    unique substring, then a fuzzy closest match — mirroring how a student would name the thing."""
    if assignment_id is not None:
        wanted = str(assignment_id)
        for a in assignments:
            if a.provider_id == wanted:
                return a, "id"
        return None, None
    norm = (title or "").strip().lower()
    if not norm:
        return None, None
    for a in assignments:                                        # exact, case-insensitive
        if a.name.strip().lower() == norm:
            return a, "title"
    subs = [a for a in assignments if norm in a.name.strip().lower()]
    if len(subs) == 1:                                           # unambiguous substring
        return subs[0], "title-contains"
    pool = subs or assignments
    close = difflib.get_close_matches(norm, [a.name.strip().lower() for a in pool], n=1, cutoff=0.5)
    if close:
        for a in pool:                                           # fuzzy nearest
            if a.name.strip().lower() == close[0]:
                return a, "nearest-title"
    return None, None


async def did_i_submit_this(user_id: UUID, *, assignment_id: str | None = None, title: str | None = None,
                            provider: str = "canvas") -> SubmitCheckItem:
    """Answer "did I submit <assignment>?" from the PERSISTED submission state, never a guess. Give either
    an ``assignment_id`` or a ``title`` (nearest-match). A submitted/graded assignment is "yes"; a genuinely
    unsubmitted one is "no"; an assignment whose state the provider could not report is "unknown" (NOT "no");
    a reference that matches nothing is honestly reported as unmatched. The matched assignment's full
    provenance travels on the result."""
    if assignment_id is None and title is None:
        raise ValueError("did_i_submit_this requires an assignment_id or a title to match")
    query = str(assignment_id) if assignment_id is not None else str(title)
    assignments = await school_store.list_assignments(user_id, provider=provider)
    match, how = _match_assignment(assignments, assignment_id=assignment_id, title=title)
    if match is None:
        return SubmitCheckItem(
            query=query, matched=False, answer="unknown", submission_state=None,
            reason="no assignment matched that reference — can't say whether it was submitted",
            match_kind=None, assignment=None)
    kind = match.submission.kind
    answer, why = _answer_for_state(kind)
    return SubmitCheckItem(
        query=query, matched=True, answer=answer, submission_state=kind,
        reason=f"{match.name}: {why}", match_kind=how, assignment=match)


# ---------------------------------------------------------------------------------------------------
# 6. catch me up
# ---------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class CatchUpItem:
    """One prioritized line in a catch-up. ``category`` classifies it for a student ("grade_posted",
    "new_assignment", …) and ``priority`` orders it (lower = surface first). For a create/update of a
    course/assignment/announcement the live ``object`` + full ``provenance`` are present; a material change
    (the store exposes no live material read-back yet) or a delete carries the change record's openable
    ``source_url`` but leaves ``object``/``provenance`` ``None`` rather than fabricate them."""

    category: str
    priority: int
    headline: str
    object_type: str
    provider_id: str
    change_type: sc.ChangeType
    detected_at: datetime.datetime
    changed_fields: list[str]
    source_url: str | None
    object: sc.Course | sc.Assignment | sc.Announcement | None
    provenance: sc.Provenance | None


@dataclasses.dataclass
class CatchUp:
    """A summarized, prioritized catch-up over the persisted graph — the narrative "what have I missed?"
    answer, not a raw diff dump. ``items`` are ordered most-important-first; ``counts`` summarizes by
    category; ``last_synced_at``/``latest_change_at`` carry source freshness so staleness is visible."""

    since: datetime.datetime | None
    generated_at: datetime.datetime
    items: list[CatchUpItem]
    counts: dict[str, int]
    last_synced_at: datetime.datetime | None     # freshest provider sync across surfaced items
    latest_change_at: datetime.datetime | None   # newest change detected in the window


# Priority bands (lower = surface first): a posted grade is the thing a student most wants to know, then
# newly-assigned work, then submission-state moves, then edits/announcements/materials, then removals.
def _categorize_change(ci: ChangeQueryItem) -> tuple[str, int, str]:
    ot = ci.object_type
    fields = set(ci.changed_fields or [])
    name = getattr(ci.object, "name", None) or getattr(ci.object, "title", None) or ci.provider_id
    if ci.change_type is sc.ChangeType.deleted:
        return f"removed_{ot}", 60, f"Removed {ot}: {name}"
    if ot == "assignment":
        if ci.change_type is sc.ChangeType.created:
            due = getattr(ci.object, "due_at", None)
            when = f" (due {due.date().isoformat()})" if due is not None else ""
            return "new_assignment", 20, f"New assignment: {name}{when}"
        if fields & {"grade", "score"}:
            grade = getattr(getattr(ci.object, "submission", None), "grade", None)
            return "grade_posted", 10, f"Grade posted: {name}" + (f" — {grade}" if grade else "")
        if fields & {"submission_state", "submitted_at", "late", "missing"}:
            return "submission_update", 25, f"Submission updated: {name}"
        if "due_at" in fields:
            return "updated_assignment", 30, f"Due date changed: {name}"
        return "updated_assignment", 40, f"Updated assignment: {name}"
    if ot == "announcement":
        if ci.change_type is sc.ChangeType.created:
            return "new_announcement", 35, f"New announcement: {name}"
        return "updated_announcement", 45, f"Updated announcement: {name}"
    if ot == "course":
        if ci.change_type is sc.ChangeType.created:
            return "new_course", 50, f"New course: {name}"
        return "updated_course", 55, f"Updated course: {name}"
    verb = "New" if ci.change_type is sc.ChangeType.created else "Updated"
    return f"{ci.change_type.value}_{ot}", 55, f"{verb} {ot}: {name}"


def _categorize_material(ch: school_store.ChangedItem) -> tuple[str, int, str]:
    if ch.change_type is sc.ChangeType.deleted:
        return "removed_material", 65, "Course material removed"
    if ch.change_type is sc.ChangeType.created:
        return "new_material", 40, "New course material posted"
    return "updated_material", 50, "Course material updated"


async def catch_me_up(user_id: UUID, *, since: datetime.datetime | None = None,
                      now: datetime.datetime | None = None, provider: str = "canvas") -> CatchUp:
    """Narrative catch-up over the persisted graph since ``since``: new/updated assignments, posted grades
    and submission moves, new announcements, and newly-added materials — summarized and prioritized, not a
    raw diff. Reuses ``what_changed``'s field-level machinery to resolve each course/assignment/announcement
    change to its LIVE object + full provenance, adds materials best-effort from the change log, and reports
    source freshness (newest provider sync + newest change) so the student can see how current this is."""
    now = _now(now)
    resolved = await what_changed(user_id, since=since, provider=provider)
    raw = await school_store.changes_since(user_id, since=since, provider=provider)

    items: list[CatchUpItem] = []
    for ci in resolved:
        category, priority, headline = _categorize_change(ci)
        items.append(CatchUpItem(
            category=category, priority=priority, headline=headline, object_type=ci.object_type,
            provider_id=ci.provider_id, change_type=ci.change_type, detected_at=ci.detected_at,
            changed_fields=ci.changed_fields, source_url=ci.source_url, object=ci.object,
            provenance=ci.provenance))
    for ch in raw:
        # materials aren't in ``what_changed``'s content types and the store has no live material read-back,
        # so surface them honestly from the change record: an openable source_url, but object/provenance None.
        if ch.object_type != "material":
            continue
        category, priority, headline = _categorize_material(ch)
        items.append(CatchUpItem(
            category=category, priority=priority, headline=headline, object_type=ch.object_type,
            provider_id=ch.provider_id, change_type=ch.change_type, detected_at=ch.detected_at,
            changed_fields=list(ch.changed_fields), source_url=ch.source_url, object=None, provenance=None))

    items.sort(key=lambda it: (it.priority, -it.detected_at.timestamp()))
    counts: dict[str, int] = {}
    for it in items:
        counts[it.category] = counts.get(it.category, 0) + 1
    last_synced_at = max((it.provenance.last_synced_at for it in items if it.provenance is not None),
                         default=None)
    latest_change_at = max((ch.detected_at for ch in raw), default=None)
    return CatchUp(since=since, generated_at=now, items=items, counts=counts,
                   last_synced_at=last_synced_at, latest_change_at=latest_change_at)
