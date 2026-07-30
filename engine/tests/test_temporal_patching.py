"""A DATE-ONLY CHANGE KEEPS THE CLOCK — the merge rule, and where it has to live.

"Move it to friday" changes the DAY. It says nothing about the hour, and a 4pm rehearsal moved to friday
is a 4pm rehearsal on friday. `temporal.resolve` correctly reports a date-only phrase as a whole day, and
writing both halves of that over a start and an end the student had already pinned turned their event into
an all-day block — silently, against something they never said, and with a green reply on top.

WHY IT IS A MERGE RULE AND NOT A RESOLVER RULE. Turning "friday" into a date happens in
`goal_handler.resolve_temporal`, before the goal is loaded, so the value it is about to overwrite is not
visible to it. `goal_slots.reconcile_temporal` runs where both halves are in hand, and `ensure_goal` is
the only caller. Putting it in the resolver would have meant passing the old moment down to a function
whose whole job is not to need it — and, measurably, the acceptance path calls the resolver WITHOUT the
goal, so the bug would have survived the fix.

Three rules, and they are `calendar_mutation.recompute`'s, so the goal lane and the legacy calendar lane
answer "what did the student actually change" the same way. Each is asserted in BOTH directions: the
partner to "a date-only change keeps the clock" is "an explicit time replaces it", or the rule would be
indistinguishable from ignoring the student.
"""

from __future__ import annotations

import datetime

import pytest

from bruce_engine import goal_handler, goal_slots
from bruce_engine.goal_slots import GoalKind, SlotValue, Source

TZ = "America/Chicago"
NOW = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.timezone.utc)   # a Tuesday


def _known(start="2026-08-04T16:00:00", end="2026-08-04T17:00:00"):
    """A rehearsal the student has already pinned: 4pm to 5pm on the Tuesday."""
    out = {"title": SlotValue("rehearsal", Source.user_stated, turn_index=1)}
    if start:
        out["start"] = SlotValue(start, Source.user_stated, turn_index=1)
    if end:
        out["end"] = SlotValue(end, Source.user_stated, turn_index=1)
    return out


def _turn(phrase: str, *, timezone_name: str = TZ, now=NOW):
    """One amendment turn, through the production call `goal_handler` makes."""
    return goal_handler.resolve_temporal(
        GoalKind.schedule_event, {"start": SlotValue(phrase, Source.user_stated, turn_index=2)},
        timezone_name=timezone_name, now=now)


def _apply(phrase: str, known=None, **kw):
    """The whole path a turn takes: phrase -> moment -> reconciled against what the goal already holds ->
    merged. `ensure_goal` does exactly this and nothing else."""
    known = _known() if known is None else known
    incoming = _turn(phrase, **kw)
    reconciled = goal_slots.reconcile_temporal(GoalKind.schedule_event, known, incoming)
    return goal_slots.merge_slots(known, reconciled)


def _dt(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


# --- the rule, and its partner -------------------------------------------------------------------------

def test_a_date_only_change_keeps_the_clock_the_student_already_set():
    """THE DEFECT. Before this, "move it to friday" produced the date-only span 2026-08-07/2026-08-08 —
    a whole-day block where a 4pm rehearsal used to be."""
    merged = _apply("move it to friday")
    start = merged["start"].value
    assert len(str(start)) > 10, f"the event was turned into an all-day block: {start!r}"
    assert _dt(start).hour == 16, "the hour the student set was not kept"
    assert _dt(start).weekday() == 4, "the day did not move"


def test_an_explicit_new_time_replaces_the_clock():
    """THE PARTNER. If a stated time did not win, the rule above would be indistinguishable from ignoring
    the student — and an assistant that cannot be told a new time is worse than one that forgets."""
    merged = _apply("move it to friday at 5pm")
    assert _dt(merged["start"].value).hour == 17
    assert _dt(merged["start"].value).weekday() == 4


def test_the_duration_survives_a_date_only_change():
    """A two-hour event moved to another day is still two hours. Silently reshaping it to an hour is a
    change the student never asked for and would only notice from the calendar."""
    known = _known(end="2026-08-04T18:00:00")            # 4pm-6pm
    merged = _apply("move it to friday", known)
    span = _dt(merged["end"].value) - _dt(merged["start"].value)
    assert span == datetime.timedelta(hours=2), f"a 2h event became {span}"


def test_the_end_always_moves_with_the_start():
    """`slot_patch` is replacement-shaped and can name only ONE slot, so a start moved on its own would
    leave the previous end beside it — an event that starts on friday and ends on tuesday."""
    merged = _apply("move it to friday")
    assert _dt(merged["end"].value).date() == _dt(merged["start"].value).date()
    assert _dt(merged["end"].value) > _dt(merged["start"].value)


def test_a_change_is_resolved_in_the_students_own_timezone():
    """Not the process's. The same phrase at the same instant is a different day for a Central student and
    a Pacific one, and a resolver that guessed would place every event hours off with no visible error."""
    at = datetime.datetime(2026, 8, 1, 5, 30, tzinfo=datetime.timezone.utc)   # Aug 1 CT, Jul 31 PT
    central = _apply("move it to tomorrow", timezone_name="America/Chicago", now=at)
    pacific = _apply("move it to tomorrow", timezone_name="America/Los_Angeles", now=at)
    assert central["start"].value.startswith("2026-08-02")
    assert pacific["start"].value.startswith("2026-08-01")
    # and the clock is kept on BOTH sides, so the zone changed the day and nothing else.
    assert _dt(central["start"].value).hour == _dt(pacific["start"].value).hour == 16


# --- what the rule must NOT do --------------------------------------------------------------------------

def test_an_all_day_event_stays_all_day():
    """There is no clock to keep, and inventing one would put a whole-day block at midnight."""
    known = {"title": SlotValue("spring break", Source.user_stated, turn_index=1),
             "start": SlotValue("2026-08-04", Source.user_stated, turn_index=1),
             "end": SlotValue("2026-08-05", Source.user_stated, turn_index=1)}
    merged = _apply("move it to friday", known)
    assert merged["start"].value == "2026-08-07", f"an all-day event grew a clock: {merged['start'].value}"


def test_a_goal_with_no_moment_yet_is_left_alone():
    """The first turn of a calendar goal has nothing to preserve, so the resolver's answer stands."""
    merged = _apply("friday at 4pm", {"title": SlotValue("rehearsal", Source.user_stated, turn_index=1)})
    assert _dt(merged["start"].value).hour == 16 and _dt(merged["start"].value).weekday() == 4


def test_an_unreadable_phrase_changes_nothing_so_bruce_asks():
    """An invented moment is worse than a question. The phrase is left exactly as the student typed it."""
    merged = _apply("whenever works")
    assert merged["start"].value == "2026-08-04T16:00:00", "an unreadable phrase moved the event"
    assert merged["end"].value == "2026-08-04T17:00:00"


def test_a_kind_with_no_moment_is_untouched():
    """`send_email` declares no datetime slot. The rule is found off the SCHEMA, so an email goal needs no
    branch and gains nothing."""
    known = {"subject": SlotValue("hi", Source.user_stated, turn_index=1)}
    incoming = {"subject": SlotValue("hello", Source.user_stated, turn_index=2)}
    assert goal_slots.reconcile_temporal(GoalKind.send_email, known, incoming) == incoming


@pytest.mark.parametrize("end", [None, "2026-08-04T15:00:00", "2026-08-04T16:00:00", "not-a-date"])
def test_a_missing_or_broken_end_falls_back_to_an_hour_rather_than_a_negative_span(end):
    """A backwards or unreadable end must not produce an event that ends before it starts."""
    merged = _apply("move it to friday", _known(end=end))
    span = _dt(merged["end"].value) - _dt(merged["start"].value)
    assert span == datetime.timedelta(hours=1), f"{end!r} produced {span}"


def test_the_reconciliation_keeps_the_turns_own_provenance():
    """The rewritten end has to look like THIS turn's value, or `merge_slots` prefers the stale one it was
    written to replace."""
    known = _known()
    incoming = _turn("move it to friday")
    out = goal_slots.reconcile_temporal(GoalKind.schedule_event, known, incoming)
    assert out["end"].turn_index == incoming["start"].turn_index
    assert out["end"].source is incoming["start"].source
    assert goal_slots.merge_slots(known, out)["end"].value == out["end"].value


def test_the_date_only_rule_agrees_with_the_reference_implementation():
    """`calendar_mutation.recompute` is the same rule on the legacy calendar path. Two lanes with two
    answers to "what did the student change" is how one of them quietly becomes wrong, so the case this
    change is about is pinned against the reference rather than merely against itself."""
    from bruce_engine import calendar_mutation

    entity = {"start": "2026-08-04T16:00:00", "end": "2026-08-04T18:00:00", "timezone": TZ}
    local = NOW.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
    reference_start, reference_end, _tz = calendar_mutation.recompute(entity, "move it to friday",
                                                                      now=local)
    merged = _apply("move it to friday", _known(end="2026-08-04T18:00:00"))
    assert merged["start"].value == reference_start
    assert merged["end"].value == reference_end


def test_the_one_place_the_two_lanes_still_differ_is_recorded_rather_than_hidden():
    """WHEN THE STUDENT STATES A NEW TIME, the two lanes derive a different END.

    `recompute` keeps the event's existing duration ("move it to friday at 5pm" on a 2h event -> 5-7pm).
    The goal lane takes `temporal.resolve`'s answer, which defaults to an hour when the phrase states no
    range (-> 5-6pm). Neither is wrong: the student named a new start and neither named an end.

    It is not reconciled because it cannot be, cheaply and honestly. The two would only agree if the merge
    could tell a DEFAULTED end from a STATED one ("move it to 5-6pm" really is an hour), and `Resolved`
    does not carry that distinction. Guessing it from the span — "exactly 60 minutes must be the default" —
    would stretch a genuine one-hour range to two. So the difference is asserted here, in the direction it
    actually goes, and a change that closes it will fail this test and be looked at.
    """
    from bruce_engine import calendar_mutation

    entity = {"start": "2026-08-04T16:00:00", "end": "2026-08-04T18:00:00", "timezone": TZ}
    local = NOW.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))
    _s, reference_end, _tz = calendar_mutation.recompute(entity, "move it to friday at 5pm", now=local)
    merged = _apply("move it to friday at 5pm", _known(end="2026-08-04T18:00:00"))

    assert reference_end == "2026-08-07T19:00:00", "the reference stopped preserving duration"
    assert merged["end"].value == "2026-08-07T18:00:00", "the goal lane stopped using the resolver's end"
    # the START — the part the student actually stated — is the same on both lanes.
    assert merged["start"].value == _s
