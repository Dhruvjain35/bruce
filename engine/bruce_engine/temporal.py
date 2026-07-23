"""TemporalResolver — deterministic natural-language time normalization (provider-neutral).

The runtime is the hands: it turns how a student actually writes time ("today at 11:30 pm", "next friday
at 4", "tomorrow", "aug 1-2") into a concrete start/end, rather than depending on the model to emit
perfect ISO. This is universal — NOT tied to any one event, phrasing, or fixture. Calendar is its first
consumer; anything else that needs to place a moment in time reuses it.

`now` is injected (a timezone-aware datetime) so resolution is deterministic + testable and honors the
user's local day — "today" near midnight must not slip a day because the server runs in UTC.

Honesty rules baked in:
  * we never invent a date/time that wasn't said. If only a bare hour is given ("at 4") we mark it
    low-confidence rather than silently committing to am/pm beyond a stated, documented default.
  * a timed event with no stated end gets a documented default duration (60 min) — Google requires an
    end; this is applied consistently, not guessed per-case.
"""

from __future__ import annotations

import calendar as _cal
import datetime as _dt
import re
from dataclasses import dataclass

_DEFAULT_DURATION = _dt.timedelta(minutes=60)   # documented default when no end is stated

_MONTHS: dict[str, int] = {}
for _i in range(1, 13):
    _MONTHS[_cal.month_name[_i].lower()] = _i
    _MONTHS[_cal.month_abbr[_i].lower()] = _i
_MONTHS["sept"] = 9
_MONTH_ALT = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))

_WEEKDAYS = {name.lower(): i for i, name in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}
_WEEKDAYS.update({"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
                  "fri": 4, "sat": 5, "sun": 6})
_WEEKDAY_ALT = "|".join(sorted((re.escape(w) for w in _WEEKDAYS), key=len, reverse=True))

_DATE_RANGE_RE = re.compile(
    rf"\b(?P<month>{_MONTH_ALT})\.?\s+(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:\s*(?:-|–|—|to|through|thru|&|and|\+)\s*(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?)?"
    rf"(?:,?\s*(?P<year>\d{{4}}))?", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_ISO_DT_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::\d{2})?(?P<off>Z|[+-]\d{2}:?\d{2})?")

# Relative offsets from the message's send time: "4 days from now", "in 3 days", "in two weeks",
# "next week". THE fix for "basketball tourney 4 days from now" resolving to today — the old resolver had
# no offset branch, so it fell through to "bare time => today". Word-numbers a student actually types.
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
            "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "couple": 2,
            "few": 3, "fourteen": 14}
_NUM = r"(?:\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|couple|few|fourteen)"
_OFFSET_DAYS_RE = re.compile(rf"\b(?:in\s+)?(?P<n>{_NUM})\s+days?\s+from\s+(?:now|today)\b|\bin\s+(?P<n2>{_NUM})\s+days?\b", re.IGNORECASE)
_OFFSET_WEEKS_RE = re.compile(rf"\b(?:in\s+)?(?P<n>{_NUM})\s+weeks?\s+from\s+(?:now|today)\b|\bin\s+(?P<n2>{_NUM})\s+weeks?\b", re.IGNORECASE)


def _num(tok: str | None) -> int | None:
    if not tok:
        return None
    tok = tok.lower()
    return int(tok) if tok.isdigit() else _WORDNUM.get(tok)
_NEXT_WEEKDAY_RE = re.compile(rf"\b(?P<mod>next|this|coming)?\s*(?P<wd>{_WEEKDAY_ALT})\b", re.IGNORECASE)
# EXPLICIT time — am/pm, noon/midnight, or a genuine 24h clock (hour >= 13). A colon-time with hour <= 12
# and NO am/pm is deliberately NOT matched here (it is ambiguous, handled only when anchored by "at"), so a
# score/ratio like "up 3:15 in the 4th" or "mix 1:30 with water" never becomes a spurious event.
_TIME_RE = re.compile(
    r"\b(?:(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm|a\.m\.|p\.m\.)"
    r"|(?P<h24>(?:1[3-9]|2[0-3])):(?P<m24>\d{2})"
    r"|(?P<noon>noon|midnight))\b", re.IGNORECASE)
# ANCHORED time — "at 4", "at 3:30", "@ 3:30", "around 5", "by 6:15". No am/pm -> ambiguous, resolved by
# evening/morning context + a low-confidence heuristic, and flagged. The anchor ("at"/"@"/"around"/"by")
# is what separates a real time from a stray colon number.
_AT_TIME_RE = re.compile(
    r"\b(?:at|@|around|by)\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\b(?!\s*(?:am|pm))", re.IGNORECASE)
# time RANGE — "2 to 4pm", "3:00-4:30pm", "from 9 to 10:30am". The end's am/pm applies to a bare start.
_TIME_RANGE_RE = re.compile(
    r"\b(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*(?P<ap1>am|pm)?\s*(?:-|–|—|to|until|till|thru|through)\s*"
    r"(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*(?P<ap2>am|pm)\b", re.IGNORECASE)


@dataclass
class Resolved:
    """A normalized moment (or span). `start`/`end` are ISO strings: date-only (YYYY-MM-DD) when all_day,
    else naive local datetime (YYYY-MM-DDTHH:MM:SS) — Google places a tz-less dateTime in the calendar's
    own timezone, which is exactly the student's local time."""
    start: str
    end: str
    all_day: bool
    confidence: float = 1.0
    needs: tuple[str, ...] = ()      # what's genuinely ambiguous and worth ONE question (e.g. "am_pm")


def _year_hint(text: str, now: _dt.datetime) -> int:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else now.year


def _resolve_date(text: str, now: _dt.datetime) -> tuple[_dt.date, _dt.date | None] | None:
    """A single date or an (start, inclusive-end) span, from relative words / weekday / month-day / ISO."""
    t = text.lower()
    today = now.date()
    if re.search(r"\b(today|tonight)\b", t):
        return today, None
    if re.search(r"\b(tomorrow|tmrw|tmr)\b", t):
        return today + _dt.timedelta(days=1), None
    if re.search(r"\b(day after tomorrow)\b", t):
        return today + _dt.timedelta(days=2), None
    # relative offsets from the send time (before weekday matching so "in 2 weeks" isn't misread)
    m = _OFFSET_DAYS_RE.search(t)
    if m:
        n = _num(m.group("n") or m.group("n2"))
        if n is not None:
            return today + _dt.timedelta(days=n), None
    m = _OFFSET_WEEKS_RE.search(t)
    if m:
        n = _num(m.group("n") or m.group("n2"))
        if n is not None:
            return today + _dt.timedelta(weeks=n), None
    if re.search(r"\bnext\s+week\b", t):
        return today + _dt.timedelta(days=7), None
    if re.search(r"\bthis weekend\b", t):
        sat = today + _dt.timedelta(days=(5 - today.weekday()) % 7)
        return sat, sat + _dt.timedelta(days=1)
    m = _ISO_DATE_RE.search(text)
    if m:
        d = _dt.date.fromisoformat(m.group(1))
        return d, None
    m = _DATE_RANGE_RE.search(text)
    if m and _MONTHS.get(m.group("month").lower()):
        mon = _MONTHS[m.group("month").lower()]
        explicit_year = m.group("year")
        year = int(explicit_year) if explicit_year else _year_hint(text, now)
        try:
            d1 = _dt.date(year, mon, int(m.group("d1")))
        except ValueError:
            d1 = None
        # No explicit year and the date already passed this year -> next year (a "jan 2" flyer read in
        # december is next january, not 11 months in the past).
        if d1 and not explicit_year and d1 < today:
            try:
                d1 = _dt.date(year + 1, mon, int(m.group("d1")))
                year += 1
            except ValueError:
                pass
        if d1:
            d2 = None
            if m.group("d2"):
                try:
                    d2 = _dt.date(year, mon, int(m.group("d2")))
                except ValueError:
                    d2 = None
            return d1, d2
    m = _NEXT_WEEKDAY_RE.search(text)
    if m:
        wd = _WEEKDAYS[m.group("wd").lower()]
        delta = (wd - today.weekday()) % 7 or 7      # upcoming occurrence (a weekday that IS today -> +7)
        if (m.group("mod") or "").lower() == "next":
            delta += 7                                # "next friday" = the FOLLOWING week's friday
        return today + _dt.timedelta(days=delta), None
    return None


def _apply_ampm(h: int, mn: int, ap: str) -> tuple[int, int]:
    ap = (ap or "").replace(".", "").lower()
    if ap == "pm" and h != 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return h % 24, mn


def _ambiguous_hour(h: int, mn: int, t: str) -> tuple[int, int, float, tuple[str, ...]]:
    """An hour with no am/pm, resolved by context. Evening words push pm (12 -> midnight); morning -> am;
    otherwise the documented 1-7 -> afternoon heuristic, flagged am_pm at low confidence."""
    if re.search(r"\b(tonight|evening|night|pm)\b", t):
        return (0 if h == 12 else (h + 12) % 24), mn, 0.9, ()      # "tonight at 12" -> midnight
    if re.search(r"\b(morning|am)\b", t):
        return (0 if h == 12 else h), mn, 0.9, ()
    h24 = h + 12 if 1 <= h <= 7 else h                              # 1-7 -> afternoon/evening
    return h24 % 24, mn, 0.6, ("am_pm",)


def _resolve_time(text: str) -> tuple[int, int, float, tuple[str, ...]] | None:
    """(hour24, minute, confidence, needs). None if no time is stated. EXPLICIT (am/pm, noon/midnight,
    24h>=13) is certain; an ANCHORED bare/colon time ("at 3:30") is ambiguous + flagged; an unanchored
    colon number ("3:15 in the 4th") is NOT a time."""
    t = text.lower()
    m = _TIME_RE.search(t)
    if m:
        if m.group("noon"):
            return (12, 0, 1.0, ()) if m.group("noon").lower() == "noon" else (0, 0, 1.0, ())
        if m.group("h24") is not None:                              # genuine 24h clock (hour 13-23)
            return int(m.group("h24")) % 24, int(m.group("m24")), 1.0, ()
        h, mn = _apply_ampm(int(m.group("h")), int(m.group("m") or 0), m.group("ap"))
        return h, mn, 1.0, ()
    m = _AT_TIME_RE.search(t)
    if m:                                                           # "at 3", "at 3:30" — no am/pm
        return _ambiguous_hour(int(m.group("h")), int(m.group("m") or 0), t)
    return None


def _resolve_time_range(text: str) -> tuple[int, int, int, int, float, tuple[str, ...]] | None:
    """A stated start-end time range ("2 to 4pm", "3:00-4:30pm"). The end's am/pm applies to a bare start.
    Returns (start_h, start_m, end_h, end_m, conf, needs) or None."""
    m = _TIME_RANGE_RE.search(text)
    if m is None:
        return None
    ap2 = m.group("ap2").lower()
    ap1 = (m.group("ap1") or ap2).lower()
    sh, sm = _apply_ampm(int(m.group("h1")), int(m.group("m1") or 0), ap1)
    eh, em = _apply_ampm(int(m.group("h2")), int(m.group("m2") or 0), ap2)
    return sh, sm, eh, em, 1.0, ()


def resolve(text: str, *, now: _dt.datetime) -> Resolved | None:
    """Normalize a natural-language when-phrase to a concrete start/end. None if no time info is present.

    date + time -> timed (default 60-min duration if no end). date only -> all-day (Google exclusive end).
    time only ("at 11:30 pm") -> today at that time. A stated time RANGE keeps both ends."""
    # Fast path: a full ISO datetime the model already normalized. If it carries an OFFSET (models emit
    # UTC 'Z'), honor it — the offset IS the timezone; discarding it would misplace the event by hours.
    m = _ISO_DT_RE.search(text)
    if m:
        off = m.group("off")
        try:
            naive = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                 int(m.group(4)) % 24, int(m.group(5)))
        except ValueError:
            naive = None
        if naive is not None and off:
            offtxt = "+00:00" if off.upper() == "Z" else (off if ":" in off else off[:3] + ":" + off[3:])
            aware = _dt.datetime.fromisoformat(naive.isoformat() + offtxt)
            return Resolved(start=aware.isoformat(), end=(aware + _DEFAULT_DURATION).isoformat(),
                            all_day=False)
        if naive is not None:
            return Resolved(start=naive.isoformat(timespec="seconds"),
                            end=(naive + _DEFAULT_DURATION).isoformat(timespec="seconds"), all_day=False)

    date_part = _resolve_date(text, now)
    rng = _resolve_time_range(text)
    time_part = _resolve_time(text) if rng is None else None

    if date_part is None and time_part is None and rng is None:
        return None
    if date_part is None:
        date_part = (now.date(), None)               # a bare time means today
    start_date, end_date = date_part

    if rng is not None:
        sh, sm, eh, em, conf, needs = rng
        start = _dt.datetime.combine(start_date, _dt.time(sh, sm))
        end = _dt.datetime.combine(start_date, _dt.time(eh, em))
        if end <= start:
            end += _dt.timedelta(days=1)              # range crosses midnight
        return Resolved(start=start.isoformat(timespec="seconds"),
                        end=end.isoformat(timespec="seconds"), all_day=False, confidence=conf, needs=needs)
    if time_part is not None:
        h, mn, conf, needs = time_part
        start = _dt.datetime.combine(start_date, _dt.time(h, mn))
        end = start + _DEFAULT_DURATION
        return Resolved(start=start.isoformat(timespec="seconds"),
                        end=end.isoformat(timespec="seconds"), all_day=False,
                        confidence=conf, needs=needs)
    # all-day: Google's exclusive end = day after the last inclusive day
    last = end_date or start_date
    return Resolved(start=start_date.isoformat(),
                    end=(last + _dt.timedelta(days=1)).isoformat(), all_day=True)
