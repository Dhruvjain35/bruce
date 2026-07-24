"""calendar_mutation classify + recompute (pure) — merge only what the user changed onto the entity."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from bruce_engine import calendar_mutation as cm

NOW = dt.datetime(2026, 7, 23, 15, 0, tzinfo=ZoneInfo("America/Chicago"))   # Thu Jul 23 2026


def test_classify():
    assert cm.classify("delete chess class") == "delete"
    assert cm.classify("cancel the meeting") == "delete"
    assert cm.classify("move chess class to 9pm") == "update"
    assert cm.classify("reschedule practice to friday") == "update"
    assert cm.classify("not today, i said 4 days from now") == "repair"
    assert cm.classify("i meant tomorrow") == "repair"
    assert cm.classify("add chess class tomorrow") is None      # a create, not a mutation


def test_recompute_time_only_keeps_date():
    entity = {"start": "2026-07-24T20:00:00", "timezone": "America/Chicago"}
    start, end, tz = cm.recompute(entity, "move it to 9pm", now=NOW)
    assert start == "2026-07-24T21:00:00"                        # date kept, time changed


def test_recompute_midnight():
    entity = {"start": "2026-07-24T20:00:00", "timezone": "America/Chicago"}
    start, _e, _t = cm.recompute(entity, "change it to midnight", now=NOW)
    assert start == "2026-07-24T00:00:00"


def test_recompute_date_only_keeps_time():
    # "not today, 4 days from now" on a timed event keeps the clock, fixes the date
    entity = {"start": "2026-07-23T14:00:00", "timezone": "America/Chicago"}
    start, _e, _t = cm.recompute(entity, "not today, i said 4 days from now", now=NOW)
    assert start == "2026-07-27T14:00:00"                        # +4 days, 2pm kept


def test_recompute_none_when_no_temporal():
    entity = {"start": "2026-07-24T20:00:00", "timezone": "America/Chicago"}
    assert cm.recompute(entity, "move chess class", now=NOW) is None


def test_classify_ignores_create_with_make_it():
    assert cm.classify("add chess class friday, make it 5pm") is None      # create, not a mutation
    assert cm.classify("put lunch on my calendar and make it an hour") is None
    assert cm.classify("cancel that plan with mike") == "delete"           # verb present (referent gate handles it)


def test_recompute_preserves_duration():
    entity = {"start": "2026-07-24T15:00:00", "end": "2026-07-24T17:00:00", "timezone": "America/Chicago"}
    start, end, _tz = cm.recompute(entity, "move it to 9pm", now=NOW)
    assert start == "2026-07-24T21:00:00" and end == "2026-07-24T23:00:00"  # 2h duration kept
