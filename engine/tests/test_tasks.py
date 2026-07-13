"""Offline, deterministic tests for the unified task list (#3).

No network, no model, no wall clock: every date-relative assertion pins a fixed ``today``.
"""

from datetime import date

from bruce_engine.models import (
    ExtractedDeadline,
    ExtractedIntake,
    IntakeSourceKind,
    RequiredItem,
    Task,
    TaskKind,
    TaskStatus,
)
from bruce_engine.tasks import (
    bucketize,
    estimate_workload,
    intake_to_tasks,
    status_counts,
)


# --------------------------------------------------------------------------- #
# intake_to_tasks
# --------------------------------------------------------------------------- #
def test_two_deadlines_become_two_tasks():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        title="Summer Program",
        deadlines=[
            ExtractedDeadline(
                label="Application deadline", date="2026-05-15", source_span="due May 15", confidence=0.9
            ),
            ExtractedDeadline(
                label="Recommendation deadline",
                date="2026-05-20",
                time="17:00",
                source_span="recs by May 20",
                confidence=0.8,
            ),
        ],
    )
    tasks = intake_to_tasks(intake, source="flyer.pdf")

    assert len(tasks) == 2
    assert all(t.kind is TaskKind.deadline for t in tasks)
    assert [t.title for t in tasks] == ["Application deadline", "Recommendation deadline"]
    assert [t.due for t in tasks] == ["2026-05-15", "2026-05-20"]
    assert all(t.source == "flyer.pdf" for t in tasks)
    # task ids are unique, non-empty hex strings
    ids = {t.task_id for t in tasks}
    assert len(ids) == 2
    assert all(t.task_id for t in tasks)
    # the deadline that carried a time keeps it in notes
    assert tasks[1].notes == "at 17:00"
    assert tasks[0].notes is None


def test_required_items_only_becomes_umbrella_task():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        title="Scholarship X",
        required_items=[
            RequiredItem(name="Transcript", kind="doc"),
            RequiredItem(name="Essay", kind="essay"),
        ],
    )
    tasks = intake_to_tasks(intake, source="email")

    assert len(tasks) == 1
    umbrella = tasks[0]
    assert umbrella.kind is TaskKind.application
    assert umbrella.title == "Scholarship X"
    assert umbrella.due is None
    assert [i.name for i in umbrella.required_items] == ["Transcript", "Essay"]
    assert umbrella.source == "email"


def test_umbrella_title_falls_back_when_untitled():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        required_items=[RequiredItem(name="Fee", kind="fee")],
    )
    tasks = intake_to_tasks(intake)
    assert len(tasks) == 1
    assert tasks[0].title == "Application / required items"


def test_deadlines_present_suppresses_umbrella():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        deadlines=[
            ExtractedDeadline(label="Due", date="2026-05-15", source_span="due", confidence=0.9)
        ],
        required_items=[RequiredItem(name="Essay", kind="essay")],
    )
    tasks = intake_to_tasks(intake)
    # one deadline task (no separate umbrella), and it carries the required items
    assert len(tasks) == 1
    assert tasks[0].kind is TaskKind.deadline
    assert [i.name for i in tasks[0].required_items] == ["Essay"]


def test_empty_intake_yields_no_tasks():
    assert intake_to_tasks(ExtractedIntake(source_kind=IntakeSourceKind.text)) == []


def test_copied_items_are_independent():
    item = RequiredItem(name="Essay", kind="essay", provided=False)
    intake = ExtractedIntake(source_kind=IntakeSourceKind.text, required_items=[item])
    tasks = intake_to_tasks(intake)
    tasks[0].required_items[0].provided = True
    # mutating the task's copy must not leak back into the original intake item
    assert item.provided is False


# --------------------------------------------------------------------------- #
# bucketize
# --------------------------------------------------------------------------- #
def _task(due: str | None, task_id: str = "x", kind: TaskKind = TaskKind.deadline) -> Task:
    return Task(task_id=task_id, kind=kind, title=f"t-{task_id}", due=due)


def test_bucketize_places_each_task_in_the_right_bucket():
    today = date(2026, 5, 15)
    tasks = [
        _task("2026-05-14", "yesterday"),   # overdue
        _task("2026-05-15", "today"),       # today
        _task("2026-05-16", "tomorrow"),    # tomorrow
        _task("2026-05-20", "in5"),         # this_week (today+5)
        _task("2026-06-14", "in30"),        # later (today+30)
        _task(None, "none"),                # no_date
    ]
    buckets = bucketize(tasks, today)

    assert {t.task_id for t in buckets["overdue"]} == {"yesterday"}
    assert {t.task_id for t in buckets["today"]} == {"today"}
    assert {t.task_id for t in buckets["tomorrow"]} == {"tomorrow"}
    assert {t.task_id for t in buckets["this_week"]} == {"in5"}
    assert {t.task_id for t in buckets["later"]} == {"in30"}
    assert {t.task_id for t in buckets["no_date"]} == {"none"}


def test_bucketize_always_has_all_keys():
    buckets = bucketize([], date(2026, 5, 15))
    assert set(buckets) == {"overdue", "today", "tomorrow", "this_week", "later", "no_date"}
    assert all(v == [] for v in buckets.values())


def test_bucketize_week_boundary_inclusive_then_later():
    today = date(2026, 5, 15)
    on_edge = _task("2026-05-22", "edge")   # today + 7 -> this_week (inclusive)
    just_past = _task("2026-05-23", "past")  # today + 8 -> later
    buckets = bucketize([on_edge, just_past], today)
    assert {t.task_id for t in buckets["this_week"]} == {"edge"}
    assert {t.task_id for t in buckets["later"]} == {"past"}


def test_bucketize_parses_datetime_and_bad_due():
    today = date(2026, 5, 15)
    dt_task = _task("2026-05-15T09:30:00", "dt")        # datetime on today's date -> today
    z_task = _task("2026-05-16T23:59:59Z", "z")         # datetime w/ Z -> tomorrow
    bad_task = _task("not-a-date", "bad")               # unparseable -> no_date
    buckets = bucketize([dt_task, z_task, bad_task], today)
    assert {t.task_id for t in buckets["today"]} == {"dt"}
    assert {t.task_id for t in buckets["tomorrow"]} == {"z"}
    assert {t.task_id for t in buckets["no_date"]} == {"bad"}


def test_bucketize_preserves_order_within_bucket():
    today = date(2026, 5, 15)
    a = _task("2026-06-01", "a")
    b = _task("2026-06-02", "b")
    buckets = bucketize([a, b], today)
    assert [t.task_id for t in buckets["later"]] == ["a", "b"]


# --------------------------------------------------------------------------- #
# estimate_workload
# --------------------------------------------------------------------------- #
def test_estimate_workload_base_by_kind():
    assert estimate_workload(Task(task_id="1", kind=TaskKind.form, title="f")) == 15
    assert estimate_workload(Task(task_id="2", kind=TaskKind.application, title="a")) == 90
    assert estimate_workload(Task(task_id="3", kind=TaskKind.deadline, title="d")) == 30


def test_estimate_workload_adds_for_pending_items():
    task = Task(
        task_id="1",
        kind=TaskKind.application,
        title="a",
        required_items=[
            RequiredItem(name="essay", provided=False),
            RequiredItem(name="fee", provided=True),  # provided -> no add
            RequiredItem(name="transcript", provided=False),
        ],
    )
    # base 90 + 10 per pending (2 pending) = 110
    assert estimate_workload(task) == 110


# --------------------------------------------------------------------------- #
# status_counts
# --------------------------------------------------------------------------- #
def test_status_counts_tallies_and_zero_fills():
    tasks = [
        Task(task_id="1", kind=TaskKind.deadline, title="a", status=TaskStatus.open),
        Task(task_id="2", kind=TaskKind.deadline, title="b", status=TaskStatus.open),
        Task(task_id="3", kind=TaskKind.deadline, title="c", status=TaskStatus.done),
    ]
    counts = status_counts(tasks)
    assert counts["open"] == 2
    assert counts["done"] == 1
    # every status key present, others zero
    assert set(counts) == {s.value for s in TaskStatus}
    assert counts["blocked"] == 0


def test_status_counts_empty():
    counts = status_counts([])
    assert set(counts) == {s.value for s in TaskStatus}
    assert all(v == 0 for v in counts.values())
