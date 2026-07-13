"""Offline, deterministic tests for the daily briefing composer (no network, no LLM).

Every test passes a fixed ``today`` and a fixed Task list, so the composed lines are stable.
"""

from datetime import date

from bruce_engine.briefing import NOTHING_DUE, compose_brief
from bruce_engine.models import Task, TaskKind, TaskStatus

TODAY = date(2026, 7, 13)


def _task(task_id: str, title: str, due: str | None = None, status: TaskStatus = TaskStatus.open) -> Task:
    return Task(task_id=task_id, kind=TaskKind.assignment, title=title, due=due, status=status)


def test_morning_lists_due_today_and_closing_soon():
    tasks = [
        _task("1", "Physics pset", due="2026-07-13"),            # due today
        _task("2", "Scholarship app", due="2026-07-15"),         # closing soon (2 days out)
        _task("3", "Robotics regionals", due="2026-08-30"),      # far off, should not surface
    ]
    brief = compose_brief(tasks, "morning", TODAY)

    assert brief.kind == "morning"
    assert brief.date == "2026-07-13"
    assert any("Due today" in ln and "Physics pset" in ln for ln in brief.lines)
    assert any("Closing soon" in ln and "Scholarship app" in ln for ln in brief.lines)
    # the far-off task must not appear anywhere in the brief
    joined = " ".join(brief.lines)
    assert "Robotics regionals" not in joined
    assert len(brief.lines) <= 5


def test_morning_priority_and_overdue():
    tasks = [
        _task("1", "Late lab report", due="2026-07-10"),                 # overdue
        _task("2", "Read chapter 4", due="2026-07-13"),                  # due today
        _task("3", "Pick summer program", status=TaskStatus.awaiting_decision),
    ]
    brief = compose_brief(tasks, "morning", TODAY)

    # most-urgent / recommended priority is the earliest-due (overdue) item
    assert any(ln.startswith("Priority:") and "Late lab report" in ln for ln in brief.lines)
    assert any("Overdue" in ln and "Late lab report" in ln for ln in brief.lines)
    assert any("awaiting your decision" in ln and "Pick summer program" in ln for ln in brief.lines)


def test_afterschool_work_order_by_due_date():
    tasks = [
        _task("1", "Essay draft", due="2026-07-16"),
        _task("2", "Math homework", due="2026-07-14"),
        _task("3", "Slides", due="2026-07-15"),
    ]
    brief = compose_brief(tasks, "afterschool", TODAY)

    order_line = next(ln for ln in brief.lines if "Suggested order" in ln)
    # ordered by due date: Math (14) -> Slides (15) -> Essay (16)
    assert order_line.index("Math homework") < order_line.index("Slides") < order_line.index("Essay draft")


def test_night_lists_done_today_and_overdue_at_risk():
    tasks = [
        _task("1", "History essay", due="2026-07-13", status=TaskStatus.done),   # done today
        _task("2", "FAFSA form", due="2026-07-10", status=TaskStatus.open),      # overdue -> at risk
        _task("3", "Bio quiz prep", due="2026-07-14", status=TaskStatus.open),   # due tomorrow
    ]
    brief = compose_brief(tasks, "night", TODAY)

    assert brief.kind == "night"
    assert any("Done today" in ln and "History essay" in ln for ln in brief.lines)
    assert any("at risk" in ln.lower() and "FAFSA form" in ln for ln in brief.lines)
    assert any("tomorrow" in ln.lower() and "Bio quiz prep" in ln for ln in brief.lines)


def test_empty_tasks_gives_single_honest_line():
    for kind in ("morning", "afterschool", "night"):
        brief = compose_brief([], kind, TODAY)
        assert brief.lines == [NOTHING_DUE]
        assert brief.kind == kind


def test_all_done_gives_honest_line_for_morning():
    # done/dismissed/expired tasks are inactive -> nothing to surface in a morning brief
    tasks = [
        _task("1", "Finished worksheet", due="2026-07-13", status=TaskStatus.done),
        _task("2", "Cancelled club", due="2026-07-13", status=TaskStatus.dismissed),
    ]
    brief = compose_brief(tasks, "morning", TODAY)
    assert brief.lines == [NOTHING_DUE]


def test_datetime_due_string_is_parsed():
    tasks = [_task("1", "Meeting notes", due="2026-07-13T09:30:00")]
    brief = compose_brief(tasks, "morning", TODAY)
    assert any("Due today" in ln and "Meeting notes" in ln for ln in brief.lines)


def test_unknown_kind_raises():
    try:
        compose_brief([], "lunchtime", TODAY)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown kind")
