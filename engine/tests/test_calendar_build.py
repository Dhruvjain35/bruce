"""Offline tests for calendar creation (#4): no network, no LLM, deterministic output."""

from bruce_engine.calendar_build import (
    _norm_time,
    detect_conflicts,
    intake_to_events,
    to_ics,
)
from bruce_engine.models import (
    CalendarEvent,
    ExtractedDeadline,
    ExtractedIntake,
    IntakeSourceKind,
)


def _intake(deadlines, location="Room 204"):
    return ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        title="Summer Program",
        location=location,
        deadlines=deadlines,
    )


# --------------------------------------------------------------------------- intake_to_events


def test_intake_to_events_resolvable_date_becomes_event():
    intake = _intake(
        [ExtractedDeadline(label="Application due", date="2026-05-15", source_span="due May 15", confidence=0.9)]
    )
    events = intake_to_events(intake)
    assert len(events) == 1
    ev = events[0]
    assert ev.title == "Application due"
    assert ev.start.startswith("2026-05-15T")
    assert ev.location == "Room 204"
    assert ev.tentative is True
    assert ev.prep_minutes == 30
    assert ev.source == "Summer Program"


def test_intake_to_events_skips_none_date():
    intake = _intake(
        [
            ExtractedDeadline(label="Interview", date=None, source_span="interviews soon", confidence=0.4),
            ExtractedDeadline(label="Deposit", date="2026-06-01", source_span="by June 1", confidence=0.9),
        ]
    )
    events = intake_to_events(intake)
    assert len(events) == 1  # the date=None deadline is skipped
    assert events[0].title == "Deposit"


def test_intake_to_events_skips_unparseable_date():
    intake = _intake(
        [ExtractedDeadline(label="Bad", date="not-a-date", source_span="whenever", confidence=0.5)]
    )
    assert intake_to_events(intake) == []


def test_intake_to_events_uses_deadline_time_when_present():
    intake = _intake(
        [ExtractedDeadline(label="Info session", date="2026-05-15", time="5:00 PM", source_span="5pm", confidence=0.9)]
    )
    events = intake_to_events(intake)
    assert events[0].start == "2026-05-15T17:00:00"


def test_intake_to_events_custom_prep_minutes():
    intake = _intake(
        [ExtractedDeadline(label="X", date="2026-05-15", source_span="s", confidence=0.9)]
    )
    events = intake_to_events(intake, default_prep_minutes=60)
    assert events[0].prep_minutes == 60


def test_norm_time_variants():
    assert _norm_time("17:00") == "17:00:00"
    assert _norm_time("5:00 PM") == "17:00:00"
    assert _norm_time("12:00 AM") == "00:00:00"
    assert _norm_time("12 pm") == "12:00:00"
    assert _norm_time("garbage") is None
    assert _norm_time(None) is None


# --------------------------------------------------------------------------- to_ics


def _sample_events():
    return [
        CalendarEvent(
            title="Robotics Club, room 204",
            start="2026-05-15T09:00:00",
            location="Bldg A; wing 2",
            tentative=True,
            source="Summer Program",
        )
    ]


def test_to_ics_has_required_structure():
    ics = to_ics(_sample_events())
    assert "BEGIN:VCALENDAR" in ics
    assert "END:VCALENDAR" in ics
    assert "BEGIN:VEVENT" in ics
    assert "END:VEVENT" in ics
    assert "DTSTART:20260515T090000" in ics
    assert "DTEND:20260515T100000" in ics  # missing end -> start + 1h
    assert "SUMMARY:" in ics
    assert "DTSTAMP:20260101T000000Z" in ics
    assert "UID:" in ics


def test_to_ics_uses_crlf_line_endings():
    ics = to_ics(_sample_events())
    assert "\r\n" in ics
    assert ics.endswith("\r\n")
    # No bare LF that isn't part of a CRLF.
    assert "\n" not in ics.replace("\r\n", "")


def test_to_ics_escapes_special_chars():
    ics = to_ics(_sample_events())
    assert "SUMMARY:Robotics Club\\, room 204" in ics  # comma escaped
    assert "Bldg A\\; wing 2" in ics  # semicolon escaped in LOCATION


def test_to_ics_is_deterministic_across_calls():
    events = _sample_events()
    assert to_ics(events) == to_ics(events)


def test_to_ics_uid_depends_on_dtstamp():
    events = _sample_events()
    a = to_ics(events, dtstamp="20260101T000000Z")
    b = to_ics(events, dtstamp="20260202T000000Z")
    assert a != b  # UID (and DTSTAMP) shift with dtstamp


def test_to_ics_all_day_event():
    ev = [CalendarEvent(title="Fair", start="2026-05-15", tentative=False)]
    ics = to_ics(ev)
    assert "DTSTART;VALUE=DATE:20260515" in ics
    assert "DTEND;VALUE=DATE:20260516" in ics  # missing end -> next day


def test_to_ics_folds_long_lines():
    long_title = "x" * 200
    ics = to_ics([CalendarEvent(title=long_title, start="2026-05-15T09:00:00")])
    # Folded continuation lines begin with a space after CRLF.
    assert "\r\n " in ics


# --------------------------------------------------------------------------- detect_conflicts


def test_detect_conflicts_finds_overlap():
    events = [
        CalendarEvent(title="A", start="2026-05-15T09:00:00", end="2026-05-15T10:30:00"),
        CalendarEvent(title="B", start="2026-05-15T10:00:00", end="2026-05-15T11:00:00"),
    ]
    assert detect_conflicts(events) == [(0, 1)]


def test_detect_conflicts_none_when_disjoint():
    events = [
        CalendarEvent(title="A", start="2026-05-15T09:00:00", end="2026-05-15T10:00:00"),
        CalendarEvent(title="B", start="2026-05-15T11:00:00", end="2026-05-15T12:00:00"),
    ]
    assert detect_conflicts(events) == []


def test_detect_conflicts_touching_is_not_overlap():
    # Half-open intervals: A ends exactly when B starts -> no conflict.
    events = [
        CalendarEvent(title="A", start="2026-05-15T09:00:00", end="2026-05-15T10:00:00"),
        CalendarEvent(title="B", start="2026-05-15T10:00:00", end="2026-05-15T11:00:00"),
    ]
    assert detect_conflicts(events) == []


def test_detect_conflicts_missing_end_uses_one_hour():
    # A has no end -> treated as 09:00-10:00; B starts 09:30 -> overlap.
    events = [
        CalendarEvent(title="A", start="2026-05-15T09:00:00"),
        CalendarEvent(title="B", start="2026-05-15T09:30:00", end="2026-05-15T09:45:00"),
    ]
    assert detect_conflicts(events) == [(0, 1)]


def test_detect_conflicts_ignores_unparseable_start():
    events = [
        CalendarEvent(title="A", start="not-a-date"),
        CalendarEvent(title="B", start="2026-05-15T09:00:00"),
    ]
    assert detect_conflicts(events) == []
