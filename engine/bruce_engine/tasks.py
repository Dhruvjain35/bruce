"""Unified task list (#3): turn extracted intakes into canonical ``Task`` objects and
organize them for a student.

Everything Bruce tracks — an application deadline, a form to sign, an event, an outreach
follow-up — becomes a ``Task`` (see ``models.Task``). This module is the deterministic,
offline glue that:

  * converts an ``ExtractedIntake`` (from #2) into ``Task`` objects (``intake_to_tasks``),
  * buckets tasks relative to a caller-supplied ``today`` (``bucketize``),
  * estimates rough workload minutes per task (``estimate_workload``), and
  * tallies tasks by status (``status_counts``).

Grounding/determinism note: ``today`` is ALWAYS passed in — this module never calls
``date.today()`` — so bucketing is reproducible and testable. No dates are invented: a task
whose ``due`` is missing or unparseable lands in ``no_date`` rather than being guessed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from uuid import uuid4

from .models import ExtractedIntake, RequiredItem, Task, TaskKind, TaskStatus

# Rough per-kind base effort (minutes). Deliberately coarse — this is a planning hint the
# student sees, not a promise. Tuned so applications/assignments read as "real work" and
# forms/quick-links read as "knock it out now".
_WORKLOAD_MINUTES: dict[TaskKind, int] = {
    TaskKind.deadline: 30,
    TaskKind.opportunity: 20,
    TaskKind.application: 90,
    TaskKind.assignment: 60,
    TaskKind.event: 60,
    TaskKind.form: 15,
    TaskKind.outreach: 20,
    TaskKind.other: 30,
}

# Buckets are always present in the returned dict, in this display order, even when empty.
_BUCKET_KEYS: tuple[str, ...] = ("overdue", "today", "tomorrow", "this_week", "later", "no_date")


def _copy_items(items: list[RequiredItem]) -> list[RequiredItem]:
    """Deep-copy required items so a task owns its own list (mutating one never leaks)."""
    return [item.model_copy() for item in items]


def intake_to_tasks(intake: ExtractedIntake, source: str | None = None) -> list[Task]:
    """Convert one extracted intake into canonical tasks.

    One ``Task`` per extracted deadline (``kind=deadline``, ``title`` from the deadline label,
    ``due`` from the deadline's ISO date). If the intake carries required items but no deadline
    at all, emit a single umbrella ``application`` task so the requirements are still tracked.
    The intake's required items are copied onto each generated task, and a rough workload
    estimate is attached. ``source`` (a url/file/'email'/mission id) is recorded as provenance.
    """
    tasks: list[Task] = []

    for deadline in intake.deadlines:
        task = Task(
            task_id=uuid4().hex,
            kind=TaskKind.deadline,
            title=deadline.label,
            due=deadline.date,
            required_items=_copy_items(intake.required_items),
            source=source,
            notes=f"at {deadline.time}" if deadline.time else None,
        )
        task.workload_minutes = estimate_workload(task)
        tasks.append(task)

    # Required items with no deadline anywhere -> a single umbrella task so nothing is lost.
    if not intake.deadlines and intake.required_items:
        task = Task(
            task_id=uuid4().hex,
            kind=TaskKind.application,
            title=intake.title or "Application / required items",
            due=None,
            required_items=_copy_items(intake.required_items),
            source=source,
            notes=intake.summary,
        )
        task.workload_minutes = estimate_workload(task)
        tasks.append(task)

    return tasks


def _parse_due(due: str | None) -> date | None:
    """Parse ``Task.due`` (ISO date OR datetime) to a ``date``; return None if unparseable.

    Robust to a bare ``YYYY-MM-DD`` date, a full ISO datetime (with optional ``Z``/offset), and
    a datetime string whose time portion is malformed (falls back to the leading date portion).
    """
    if not due:
        return None
    s = due.strip()
    if not s:
        return None

    # Bare date.
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass

    # Full datetime (accept trailing 'Z' which older fromisoformat can't take directly).
    candidate = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        pass

    # Last resort: the leading YYYY-MM-DD of an otherwise malformed datetime string.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def bucketize(tasks: list[Task], today: date) -> dict[str, list[Task]]:
    """Group tasks by due-date proximity to ``today`` (a required, caller-supplied date).

    Keys (always present, possibly empty): ``overdue`` (due before today), ``today``,
    ``tomorrow``, ``this_week`` (due within the next 7 days, i.e. day+2..day+7), ``later``
    (further out), and ``no_date`` (missing/unparseable due). Input order is preserved within
    each bucket.
    """
    result: dict[str, list[Task]] = {key: [] for key in _BUCKET_KEYS}
    tomorrow = today + timedelta(days=1)
    week_end = today + timedelta(days=7)

    for task in tasks:
        due = _parse_due(task.due)
        if due is None:
            result["no_date"].append(task)
        elif due < today:
            result["overdue"].append(task)
        elif due == today:
            result["today"].append(task)
        elif due == tomorrow:
            result["tomorrow"].append(task)
        elif due <= week_end:
            result["this_week"].append(task)
        else:
            result["later"].append(task)

    return result


def estimate_workload(task: Task) -> int:
    """Rough minutes of effort a task needs: a per-kind base plus 10 min per pending item.

    Deterministic heuristic (no clock, no model). "Pending" = a required item not yet provided;
    each adds a little gather-the-doc overhead on top of the kind's base estimate.
    """
    base = _WORKLOAD_MINUTES.get(task.kind, _WORKLOAD_MINUTES[TaskKind.other])
    pending = sum(1 for item in task.required_items if not item.provided)
    return base + 10 * pending


def status_counts(tasks: list[Task]) -> dict[str, int]:
    """Tally tasks by ``TaskStatus``. Every status is present as a key (0 when none)."""
    counts: dict[str, int] = {status.value: 0 for status in TaskStatus}
    for task in tasks:
        counts[task.status.value] += 1
    return counts
