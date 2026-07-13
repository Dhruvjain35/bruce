"""Calendar creation (#4): turn a grounded intake into tentative calendar events and .ics.

Bruce never silently adds anything to a student's calendar. This module turns the *resolvable*
deadlines of an :class:`~bruce_engine.models.ExtractedIntake` into tentative
:class:`~bruce_engine.models.CalendarEvent` objects, renders them to a valid RFC 5545 (iCalendar)
document, and flags time conflicts. Deadlines whose date could not be pinned down (``date=None``)
are skipped here — they stay in the intake as ambiguities, never guessed into a wrong slot.

Determinism contract: ``to_ics`` takes ``dtstamp`` as a parameter and derives each VEVENT's UID
from a content hash of the event plus that ``dtstamp``, so identical inputs always produce byte-
identical output (stable diffs, stable tests — no hidden ``datetime.now()`` calls).
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta

from .models import CalendarEvent, ExtractedIntake

_DEFAULT_TIME = "09:00:00"
_PRODID = "-//Bruce//Engine//EN"

# 12-hour clock like "5:00 PM", "5 pm", "11:30am"
_TIME_12H = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*$", re.IGNORECASE)
# 24-hour clock like "17:00" or "17:00:00"
_TIME_24H = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


def _norm_time(raw: str | None) -> str | None:
    """Normalize a free-text time to ``HH:MM:SS`` (24h), or None if unparseable.

    Kept intentionally strict: anything we can't confidently parse falls back to the default
    time rather than guessing a wrong hour.
    """
    if not raw:
        return None
    s = raw.strip()
    m = _TIME_24H.match(s)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
        if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return None
    m = _TIME_12H.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        meridiem = m.group(3).lower()
        if not (1 <= hh <= 12 and 0 <= mm <= 59):
            return None
        if meridiem == "p" and hh != 12:
            hh += 12
        elif meridiem == "a" and hh == 12:
            hh = 0
        return f"{hh:02d}:{mm:02d}:00"
    return None


def intake_to_events(
    intake: ExtractedIntake, default_prep_minutes: int = 30
) -> list[CalendarEvent]:
    """Build tentative :class:`CalendarEvent`s from an intake's resolvable deadlines.

    Only deadlines with a parseable ISO 8601 ``date`` become events; ``date=None`` (relative or
    ambiguous dates the extractor refused to guess) are skipped. Each event starts at the
    deadline's ``time`` if it is unambiguous, otherwise at a sensible default time. Title comes
    from the deadline label; location is inherited from the intake.
    """
    events: list[CalendarEvent] = []
    for d in intake.deadlines:
        if not d.date:
            continue
        try:
            iso_date = date.fromisoformat(d.date.strip())
        except (ValueError, AttributeError):
            # Not a resolvable ISO date -> skip (never place it at a guessed slot).
            continue
        start_time = _norm_time(d.time) or _DEFAULT_TIME
        events.append(
            CalendarEvent(
                title=d.label,
                start=f"{iso_date.isoformat()}T{start_time}",
                end=None,
                location=intake.location,
                prep_minutes=default_prep_minutes,
                tentative=True,
                source=intake.title,
            )
        )
    return events


def _escape(text: str) -> str:
    """Escape iCalendar TEXT per RFC 5545 3.3.11 (backslash first, then ; , and newlines)."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line to <=75 octets, continuation lines prefixed with a space (RFC 5545 3.1)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    pieces: list[str] = []
    cur = ""
    cur_bytes = 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if cur_bytes + b > 75:
            pieces.append(cur)
            cur = " " + ch  # continuation lines begin with a single space
            cur_bytes = 1 + b
        else:
            cur += ch
            cur_bytes += b
    pieces.append(cur)
    return "\r\n".join(pieces)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 date or datetime into a naive/aware datetime, or None if unparseable.

    A date-only value ("2026-05-15") becomes midnight of that day.
    """
    if not value:
        return None
    s = value.strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    try:
        return datetime.combine(date.fromisoformat(s), datetime.min.time())
    except ValueError:
        return None


def _is_date_only(value: str) -> bool:
    s = value.strip()
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _fmt_dt(dt: datetime) -> str:
    """Format a datetime as a floating iCalendar DATE-TIME (YYYYMMDDTHHMMSS)."""
    return dt.strftime("%Y%m%dT%H%M%S")


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _event_lines(event: CalendarEvent, dtstamp: str) -> list[str]:
    """Render one VEVENT to a list of (unfolded) content lines."""
    start_dt = _parse_dt(event.start)
    all_day = bool(event.start) and _is_date_only(event.start)

    uid_seed = "|".join(
        [
            event.title or "",
            event.start or "",
            event.end or "",
            event.location or "",
            event.source or "",
            dtstamp,
        ]
    )
    uid = hashlib.sha1(uid_seed.encode("utf-8")).hexdigest() + "@bruce.engine"

    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{dtstamp}"]

    if start_dt is None:
        # Should not happen for well-formed events, but never emit a broken DTSTART.
        lines.append(f"SUMMARY:{_escape(event.title)}")
        lines.append("END:VEVENT")
        return lines

    if all_day:
        end_dt = _parse_dt(event.end) if event.end else None
        if end_dt is None:
            end_dt = start_dt + timedelta(days=1)
        lines.append(f"DTSTART;VALUE=DATE:{_fmt_date(start_dt)}")
        lines.append(f"DTEND;VALUE=DATE:{_fmt_date(end_dt)}")
    else:
        end_dt = _parse_dt(event.end) if event.end else None
        if end_dt is None:
            end_dt = start_dt + timedelta(hours=1)
        lines.append(f"DTSTART:{_fmt_dt(start_dt)}")
        lines.append(f"DTEND:{_fmt_dt(end_dt)}")

    lines.append(f"SUMMARY:{_escape(event.title)}")
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    if event.source:
        lines.append(f"DESCRIPTION:{_escape('Source: ' + event.source)}")
    if event.tentative:
        lines.append("STATUS:TENTATIVE")
    lines.append("END:VEVENT")
    return lines


def to_ics(events: list[CalendarEvent], dtstamp: str = "20260101T000000Z") -> str:
    """Render events to a valid RFC 5545 VCALENDAR string (CRLF line endings, deterministic).

    ``dtstamp`` is a parameter (not ``datetime.now()``) so identical inputs yield byte-identical
    output. Each VEVENT's UID is a content hash of the event plus ``dtstamp``.
    """
    content: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for event in events:
        content.extend(_event_lines(event, dtstamp))
    content.append("END:VCALENDAR")

    folded = [_fold(line) for line in content]
    # RFC 5545: each content line (incl. the last) terminated by CRLF.
    return "\r\n".join(folded) + "\r\n"


def detect_conflicts(events: list[CalendarEvent]) -> list[tuple[int, int]]:
    """Return index pairs ``(i, j)`` (i<j) whose ``[start, end)`` intervals overlap.

    Datetimes are parsed from ISO 8601; a missing ``end`` is treated as ``start + 1h``. Events
    whose start cannot be parsed are ignored (they can't be placed, so they can't conflict).
    """
    intervals: list[tuple[int, datetime, datetime]] = []
    for idx, ev in enumerate(events):
        start = _parse_dt(ev.start)
        if start is None:
            continue
        end = _parse_dt(ev.end) if ev.end else None
        if end is None or end <= start:
            end = start + timedelta(hours=1)
        intervals.append((idx, start, end))

    conflicts: list[tuple[int, int]] = []
    for a in range(len(intervals)):
        i, s1, e1 = intervals[a]
        for b in range(a + 1, len(intervals)):
            j, s2, e2 = intervals[b]
            # Half-open overlap: [s1,e1) and [s2,e2) intersect iff s1 < e2 and s2 < e1.
            if s1 < e2 and s2 < e1:
                conflicts.append((i, j) if i < j else (j, i))
    return conflicts
