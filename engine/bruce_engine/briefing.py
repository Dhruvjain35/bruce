"""Daily briefing composer (#5): turn the unified Task list into ~5 useful lines.

DETERMINISTIC by design — no LLM, no model prose. Every line is composed directly from the
``Task`` records (titles, due dates, statuses), so a brief can never fabricate a deadline or
invent a task that isn't tracked. The caller passes ``today`` in explicitly (never
``datetime.now`` inside the logic), which keeps briefs stable and testable offline.

Three moments a student actually checks in:
  * morning     — what's due today, what's closing soon, what needs a decision, the one thing
                  to do first, and anything already overdue.
  * afterschool — what's still open, a suggested work order (by due date), and conflicts /
                  closing-soon pressure.
  * night       — what got done today, what's still at risk, and what's prepared for tomorrow.

If nothing qualifies, the brief is a single honest line rather than manufactured noise.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .models import DailyBrief, Task, TaskStatus

MAX_LINES = 5
SOON_DAYS = 3
NOTHING_DUE = "Nothing due — you're clear."

# Statuses that mean a task no longer needs active attention (excluded from "open"/"at risk").
_INACTIVE = {TaskStatus.done, TaskStatus.dismissed, TaskStatus.expired}


def _due_date(due: str | None) -> date | None:
    """Parse a Task.due ISO 8601 date/datetime string into a ``date`` (None if unparseable)."""
    if not due:
        return None
    s = due.strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])  # handles 'YYYY-MM-DD' and 'YYYY-MM-DDThh:mm:ss'
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _by_due(tasks: list[Task]) -> list[Task]:
    """Sort tasks by due date ascending; undated tasks sort last."""

    def key(t: Task) -> tuple[bool, date]:
        d = _due_date(t.due)
        return (d is None, d or date.max)

    return sorted(tasks, key=key)


def _dedupe(tasks: list[Task]) -> list[Task]:
    seen: set[str] = set()
    out: list[Task] = []
    for t in tasks:
        if t.task_id not in seen:
            seen.add(t.task_id)
            out.append(t)
    return out


def _due_suffix(task: Task, today: date) -> str:
    """A short human annotation of when a task is due, relative to ``today``."""
    d = _due_date(task.due)
    if d is None:
        return ""
    if d < today:
        return " (overdue)"
    if d == today:
        return " (due today)"
    if d == today + timedelta(days=1):
        return " (due tomorrow)"
    return f" (due {d.isoformat()})"


def _join_titles(tasks: list[Task], limit: int = 3) -> str:
    names = [t.title for t in tasks[:limit]]
    joined = ", ".join(names)
    extra = len(tasks) - limit
    if extra > 0:
        joined += f" (+{extra} more)"
    return joined


def _join_with_due(tasks: list[Task], today: date, limit: int = 3) -> str:
    parts = [f"{t.title}{_due_suffix(t, today)}" for t in tasks[:limit]]
    joined = "; ".join(parts)
    extra = len(tasks) - limit
    if extra > 0:
        joined += f" (+{extra} more)"
    return joined


def _active(tasks: list[Task]) -> list[Task]:
    return [t for t in tasks if t.status not in _INACTIVE]


def _overdue(tasks: list[Task], today: date) -> list[Task]:
    out = []
    for t in _active(tasks):
        d = _due_date(t.due)
        if d is not None and d < today:
            out.append(t)
    return _by_due(out)


def _due_on(tasks: list[Task], target: date) -> list[Task]:
    return [t for t in _active(tasks) if _due_date(t.due) == target]


def _closing_soon(tasks: list[Task], today: date, include_today: bool = False) -> list[Task]:
    lo = today if include_today else today + timedelta(days=1)
    hi = today + timedelta(days=SOON_DAYS)
    out = []
    for t in _active(tasks):
        d = _due_date(t.due)
        if d is not None and lo <= d <= hi:
            out.append(t)
    return _by_due(out)


def _most_urgent(tasks: list[Task], today: date) -> Task | None:
    """The single item to do first: the earliest-due active task (overdue ranks first)."""
    dated = [t for t in _active(tasks) if _due_date(t.due) is not None]
    if not dated:
        return None
    return _by_due(dated)[0]


def _same_day_conflicts(tasks: list[Task]) -> str:
    """Describe dates where 2+ active tasks land on the same day (scheduling pressure)."""
    groups: dict[date, list[Task]] = defaultdict(list)
    for t in tasks:
        d = _due_date(t.due)
        if d is not None:
            groups[d].append(t)
    msgs = []
    for d in sorted(groups):
        g = groups[d]
        if len(g) >= 2:
            msgs.append(f"{len(g)} due {d.isoformat()} ({_join_titles(g, limit=2)})")
    return "; ".join(msgs[:2])


def _morning(tasks: list[Task], today: date) -> list[str]:
    lines: list[str] = []
    due_today = _due_on(tasks, today)
    if due_today:
        lines.append(f"Due today: {_join_titles(due_today)}.")

    soon = _closing_soon(tasks, today)
    if soon:
        lines.append(f"Closing soon (<={SOON_DAYS} days): {_join_with_due(soon, today)}.")

    awaiting = [t for t in tasks if t.status == TaskStatus.awaiting_decision]
    if awaiting:
        lines.append(f"{len(awaiting)} awaiting your decision: {_join_titles(awaiting)}.")

    urgent = _most_urgent(tasks, today)
    if urgent is not None:
        lines.append(f"Priority: {urgent.title}{_due_suffix(urgent, today)}.")

    overdue = _overdue(tasks, today)
    if overdue:
        lines.append(f"Overdue: {_join_with_due(overdue, today)}.")

    return lines


def _afterschool(tasks: list[Task], today: date) -> list[str]:
    lines: list[str] = []
    active = _active(tasks)
    if active:
        lines.append(f"{len(active)} still open: {_join_titles(active)}.")

    order = _by_due([t for t in active if _due_date(t.due) is not None])
    if order:
        seq = " -> ".join(t.title for t in order[:4])
        if len(order) > 4:
            seq += " -> ..."
        lines.append(f"Suggested order (by due date): {seq}.")

    soon = _closing_soon(tasks, today, include_today=True)
    if soon:
        lines.append(f"Closing soon: {_join_with_due(soon, today)}.")

    conflicts = _same_day_conflicts(active)
    if conflicts:
        lines.append(f"Conflict — {conflicts}.")

    return lines


def _night(tasks: list[Task], today: date) -> list[str]:
    lines: list[str] = []
    done = [t for t in tasks if t.status == TaskStatus.done]
    if done:
        lines.append(f"Done today: {_join_titles(done)}.")

    awaiting = [t for t in tasks if t.status == TaskStatus.awaiting_decision]
    at_risk = _dedupe(_overdue(tasks, today) + awaiting)
    if at_risk:
        lines.append(f"Still at risk: {_join_with_due(at_risk, today)}.")

    tomorrow = _due_on(tasks, today + timedelta(days=1))
    if tomorrow:
        lines.append(f"Prepared for tomorrow: {_join_titles(tomorrow)}.")

    return lines


_COMPOSERS = {
    "morning": _morning,
    "afterschool": _afterschool,
    "night": _night,
}


def compose_brief(tasks: list[Task], kind: str, today: date) -> DailyBrief:
    """Compose a concise, deterministic ``DailyBrief`` from the Task list.

    Args:
        tasks: the unified task list to summarize.
        kind: one of ``"morning"``, ``"afterschool"``, ``"night"``.
        today: the reference date (required, passed in so briefs are stable/testable).

    Returns:
        A ``DailyBrief`` of at most ``MAX_LINES`` lines, or a single honest line when nothing
        qualifies. Raises ``ValueError`` for an unknown ``kind``.
    """
    key = (kind or "").strip().lower()
    composer = _COMPOSERS.get(key)
    if composer is None:
        raise ValueError(f"unknown brief kind: {kind!r} (expected morning | afterschool | night)")

    lines = composer(tasks, today)[:MAX_LINES]
    if not lines:
        lines = [NOTHING_DUE]
    return DailyBrief(kind=key, date=today.isoformat(), lines=lines)
