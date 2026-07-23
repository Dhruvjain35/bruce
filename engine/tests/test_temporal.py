"""TemporalResolver — universal, deterministic, fixture-free time normalization. `now` is injected so the
tests are stable and cover relative days, weekdays, ranges, clock times, and honest ambiguity."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from bruce_engine import temporal

TZ = ZoneInfo("America/Los_Angeles")
NOW = dt.datetime(2026, 7, 23, 15, 0, tzinfo=TZ)   # Thursday, Jul 23 2026, 3:00pm PT


def r(text):
    return temporal.resolve(text, now=NOW)


def test_today_at_time_is_timed_on_today():
    res = r("guitar class today at 11:30 pm")
    assert res.start == "2026-07-23T23:30:00" and res.end == "2026-07-24T00:30:00"
    assert res.all_day is False


def test_bare_time_means_today():
    assert r("call at 9pm").start == "2026-07-23T21:00:00"


def test_tomorrow_all_day():
    res = r("study group tomorrow")
    assert res.start == "2026-07-24" and res.end == "2026-07-25" and res.all_day


def test_tonight_with_time():
    assert r("concert tonight at 8pm").start == "2026-07-23T20:00:00"


def test_this_vs_next_weekday():
    assert r("friday at 5pm").start.startswith("2026-07-24")        # upcoming friday (tomorrow)
    assert r("next friday at 5pm").start.startswith("2026-07-31")   # following week's friday
    assert r("monday at 6pm").start.startswith("2026-07-27")


def test_24h_and_noon_midnight():
    assert r("meeting at 14:30 tomorrow").start == "2026-07-24T14:30:00"
    assert r("lunch at noon").start == "2026-07-23T12:00:00"
    assert r("shift at midnight").start == "2026-07-23T00:00:00"


def test_month_day_range_all_day_exclusive_end():
    res = r("save aug 1-2")
    assert res.start == "2026-08-01" and res.end == "2026-08-03" and res.all_day   # exclusive end


def test_month_day_borrows_year_from_context():
    assert r("event on dec 5 2027").start == "2027-12-05"


def test_bare_hour_is_low_confidence_and_flags_am_pm():
    res = r("dentist at 4")
    assert res.confidence < 1.0 and "am_pm" in res.needs


def test_no_time_info_returns_none():
    assert r("this looks cool") is None
    assert r("") is None


# --- relative offsets from the send time (the "4 days from now" fix) ------------------------------

def test_n_days_from_now_anchors_on_send_time():
    assert r("basketball tourney 4 days from now at 2pm").start == "2026-07-27T14:00:00"
    assert r("in 3 days at 10am").start == "2026-07-26T10:00:00"
    assert r("dentist in 5 days").start == "2026-07-28"


def test_word_number_offsets():
    assert r("couple days from now").start == "2026-07-25"
    assert r("in two weeks").start == "2026-08-06"
    assert r("in a week at noon").start == "2026-07-30T12:00:00"


def test_next_week_is_seven_days():
    assert r("next week").start == "2026-07-30"


def test_relative_offset_beats_bare_time_fallthrough():
    # THE regression: with an offset present it must NOT collapse to today at the stated time
    assert not r("4 days from now at 2pm").start.startswith("2026-07-23")
