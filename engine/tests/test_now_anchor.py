"""M1A — the model must know what day it is.

WHAT WAS ACTUALLY WRONG. A deterministic temporal resolver already existed (`temporal.resolve`, with an
injected `now`), and calendar_schedule / calendar_mutation already used it correctly. The gap was one
level up: `context_compiler` told the model only "The student's timezone is Central" — no date, no
clock, no weekday. So whenever the MODEL reasoned about time rather than delegating to the resolver, it
was guessing. In the M1 capability replay Bruce echoed "tomorrow at 3" back rather than resolving it,
which is safe but not correct.

A second, sharper bug: `fast_router._has_time_expression` read the clock as
`datetime.now(ZoneInfo(DEFAULT_TZ))`, and DEFAULT_TZ is America/Los_Angeles with a TODO next to it. For
a Central-timezone student near local midnight that is the wrong day.

These tests pin the ANCHOR and the ZONE, not any wording.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from zoneinfo import ZoneInfo

import pytest

from bruce_engine import context_compiler


def _run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _quiet_other_layers(monkeypatch):
    async def _empty(*_a, **_k):
        return ""
    for fn in ("_operational_block", "_entity_block"):
        monkeypatch.setattr(context_compiler, fn, _empty)
    monkeypatch.setattr(context_compiler, "_episodic_block", lambda *a, **k: "")


def _tz(monkeypatch, tz):
    async def _get(_uid):
        return tz
    monkeypatch.setattr(context_compiler.world_state, "get_timezone", _get)


# --- the anchor exists and is specific -----------------------------------------------------------------

def test_context_states_the_weekday_date_and_clock(monkeypatch):
    _tz(monkeypatch, "America/Chicago")
    import uuid
    text = _run(context_compiler.compile(uuid.uuid4(), []))
    now = _dt.datetime.now(ZoneInfo("America/Chicago"))
    assert "Right now it is" in text.text
    assert f"{now:%A}" in text.text, "weekday must be stated"
    assert f"{now:%Y}" in text.text, "year must be stated"
    assert "never assume today's date" in text.text


def test_the_anchor_uses_the_students_timezone_not_a_default(monkeypatch):
    """DEFAULT_TZ is America/Los_Angeles. A Central student must never be anchored to Pacific."""
    _tz(monkeypatch, "America/Chicago")
    import uuid
    text = _run(context_compiler.compile(uuid.uuid4(), [])).text
    chicago = _dt.datetime.now(ZoneInfo("America/Chicago"))
    assert f"{chicago:%I:%M %p}" in text


def test_no_timezone_means_no_date_claim(monkeypatch):
    """Silence beats a wrong date. An unknown timezone must not fall back to a guess."""
    _tz(monkeypatch, None)
    import uuid
    compiled = _run(context_compiler.compile(uuid.uuid4(), []))
    assert "Right now it is" not in compiled.text
    assert all(b.layer != "now" for b in compiled.blocks)


def test_a_broken_clock_degrades_only_its_own_layer(monkeypatch):
    """Per-layer fault isolation: a time failure must not blank capability or world context."""
    async def _boom(_uid):
        raise RuntimeError("tz lookup exploded")
    monkeypatch.setattr(context_compiler.world_state, "get_timezone", _boom)
    from bruce_engine import capability_snapshot as cs
    snap = cs.CapabilitySnapshot(families=(cs.FamilyState("calendar", True, capabilities=("x",)),))
    import uuid
    compiled = _run(context_compiler.compile(uuid.uuid4(), [], capabilities=snap))
    assert "CAN use: calendar" in compiled.text, "capability must survive a clock failure"


# --- ordering: the anchor must survive truncation ------------------------------------------------------

def test_now_outranks_world_and_entity_but_not_capability():
    """Capability > now > world > entity > episodic. A truncated context that drops the clock returns the
    model to guessing dates, which is the defect being fixed."""
    assert (context_compiler._P_CAPABILITY > context_compiler._P_NOW
            > context_compiler._P_WORLD > context_compiler._P_ENTITY
            > context_compiler._P_EPISODIC)


# --- the router's clock must not be zone-biased --------------------------------------------------------

def test_router_time_detection_is_not_pacific_biased():
    """_has_time_expression previously read the clock in America/Los_Angeles. Detection must not depend
    on which timezone the server happens to name."""
    import inspect
    from bruce_engine import fast_router
    src = inspect.getsource(fast_router._has_time_expression)
    # assert on the USAGE, not the word: the comment there explains why the Pacific clock was removed
    assert "ZoneInfo(DEFAULT_TZ)" not in src, "the hardcoded Pacific clock was reintroduced"
    assert "timezone.utc" in src, "detection must use a neutral reference clock"


@pytest.mark.parametrize("text,expected", [
    ("tmr at 3", True), ("friday 6pm", True), ("aug 20", True),
    ("hey whats up", False), ("how are you", False), ("thanks", False),
])
def test_time_expression_detection_still_works(text, expected):
    from bruce_engine import fast_router
    assert fast_router._has_time_expression(text) is expected, text


# --- the deterministic resolver keeps owning resolution ------------------------------------------------

def test_resolution_is_still_deterministic_and_clock_injected():
    """The model supplies MEANING; `temporal.resolve` supplies the timestamp, against an injected clock.
    Pinning the clock must pin the answer."""
    from bruce_engine import temporal
    now = _dt.datetime(2026, 7, 27, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
    a = temporal.resolve("tomorrow at 3pm", now=now)
    b = temporal.resolve("tomorrow at 3pm", now=now)
    assert a is not None and a.start == b.start, "same clock must give the same answer"
    # Resolved.start is an ISO string (date-only when all_day, else naive local datetime)
    assert a.start.startswith("2026-07-28"), a.start


def test_relative_resolution_follows_the_injected_clock_not_the_wall_clock():
    from bruce_engine import temporal
    monday = _dt.datetime(2026, 7, 27, 9, 0, tzinfo=ZoneInfo("America/Chicago"))
    later = _dt.datetime(2026, 12, 1, 9, 0, tzinfo=ZoneInfo("America/Chicago"))
    a = temporal.resolve("tomorrow at 3pm", now=monday)
    b = temporal.resolve("tomorrow at 3pm", now=later)
    assert a.start[:10] != b.start[:10], "resolution must track the injected now"
