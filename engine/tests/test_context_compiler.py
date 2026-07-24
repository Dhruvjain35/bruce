"""ContextCompiler harness (G0.2) — proves the compiler bounds the token budget, never drops a layer
silently, keeps the decision-critical layers (world/operational) when budget is tight, trims the raw
conversation window FIRST, and withholds episodic honestly when an explicit reply-target owns the context.
Store reads are stubbed so this measures the assembly logic, not the DB."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from bruce_engine import agent_run_store, context_compiler, entity_store, world_state
from bruce_engine.conversation_store import TurnBrief


def _run(c):
    return asyncio.run(c)


@contextmanager
def _state(*, tz=None, run=None, events=None):
    async def _tz(uid):
        return tz

    async def _run_(uid, **kw):
        return run

    async def _events(uid, *, limit=50):
        return list(events or [])

    with patch.object(world_state, "get_timezone", _tz), \
         patch.object(agent_run_store, "latest_active", _run_), \
         patch.object(entity_store, "active_events", _events):
        yield


def _turns(n):
    return [TurnBrief(role="user" if i % 2 == 0 else "assistant", text=f"turn number {i} content") for i in range(n)]


def test_fresh_user_has_no_history_marker():
    with _state():
        c = _run(context_compiler.compile(uuid4(), []))
    assert c.text == "No prior conversation."
    assert c.blocks == () and c.est_tokens == 0


def test_all_layers_present_and_priority_ordered():
    run = {"domain": "calendar", "status": "verifying", "goal": {"desired_outcome": "schedule the dentist"}}
    events = [{"title": "Chess Club", "start": "2026-07-25T15:00:00", "end": "2026-07-25T16:00:00"}]
    with _state(tz="America/Chicago", run=run, events=events):
        c = _run(context_compiler.compile(uuid4(), _turns(4)))
    layers = [b.layer for b in c.blocks]
    assert layers == ["world", "operational", "entity", "episodic"]      # priority order, highest first
    assert "central time" in c.text and "schedule the dentist" in c.text and "Chess Club" in c.text
    assert "Recent conversation" in c.text
    assert c.dropped == ()


def test_episodic_is_bounded_not_dumped():
    """100 turns must never all land in context — the window is capped regardless of budget headroom."""
    with _state():
        c = _run(context_compiler.compile(uuid4(), _turns(100)))
    epi = next(b for b in c.blocks if b.layer == "episodic")
    body_lines = epi.text.split("\n")[1:]                                 # drop header
    assert len(body_lines) <= context_compiler._MAX_TURNS


def test_budget_is_respected_and_never_silent():
    """Under a tight budget the compiler stays within it AND records what it cut — no silent truncation."""
    run = {"domain": "calendar", "status": "verifying", "goal": {"desired_outcome": "schedule the dentist"}}
    events = [{"title": f"Event {i}", "start": "2026-07-25T15:00:00", "end": "2026-07-25T16:00:00"} for i in range(8)]
    with _state(tz="America/Chicago", run=run, events=events):
        c = _run(context_compiler.compile(uuid4(), _turns(40), token_budget=40))
    assert c.est_tokens <= 40
    assert c.dropped                                                      # something was cut, and it's named


def test_critical_layers_survive_tight_budget():
    """World + operational (tiny, decision-critical) must outrank the raw window when budget forces a choice."""
    events = [{"title": f"Event {i}", "start": "2026-07-25T15:00:00", "end": "2026-07-25T16:00:00"} for i in range(8)]
    run = {"domain": "calendar", "status": "verifying", "goal": {"desired_outcome": "move chess to friday"}}
    with _state(tz="America/Chicago", run=run, events=events):
        c = _run(context_compiler.compile(uuid4(), _turns(60), token_budget=30))
    kept = {b.layer for b in c.blocks}
    assert "world" in kept and "operational" in kept                     # survived
    assert "episodic" in c.dropped or "episodic:trimmed" in c.dropped    # the window yielded budget first


def test_episodic_withheld_is_honest_and_leak_free():
    """include_episodic=False withholds the window (explicit reply-target owns context) but still grounds
    with world/entity — the marker is present and no raw turn leaks in."""
    with _state(tz="America/Chicago"):
        c = _run(context_compiler.compile(uuid4(), _turns(6), include_episodic=False))
    assert "No prior conversation." in c.text
    assert "turn number" not in c.text                                   # no leaked turns
    assert "central time" in c.text                                      # world still grounds


def test_deterministic():
    run = {"domain": "calendar", "status": "verifying", "goal": {"desired_outcome": "schedule the dentist"}}
    events = [{"title": "Chess Club", "start": "2026-07-25T15:00:00", "end": "2026-07-25T16:00:00"}]
    with _state(tz="America/Chicago", run=run, events=events):
        a = _run(context_compiler.compile(uuid4(), _turns(5)))
        b = _run(context_compiler.compile(uuid4(), _turns(5)))
    assert a.text == b.text and a.est_tokens == b.est_tokens


def test_nondict_goal_does_not_collapse_the_whole_compile():
    """A truthy non-dict JSONB `goal` must NOT throw past the layer guard and collapse the whole compile to
    the legacy fallback (the MEDIUM regression). It degrades gracefully — the operational layer coerces the
    bad goal to {} and falls back to the domain — while world/episodic compile normally."""
    run = {"domain": "school", "status": "active", "goal": "finish my essay"}   # goal is a STRING
    with _state(tz="America/Chicago", run=run):
        c = _run(context_compiler.compile(uuid4(), _turns(3)))
    layers = {b.layer for b in c.blocks}
    assert layers == {"world", "operational", "episodic"}    # nothing collapsed; all present
    assert "Open task" in c.text and "school" in c.text      # operational degraded to the domain, no crash
    assert "central time" in c.text and "Recent conversation" in c.text


def test_status_none_does_not_leak_the_literal():
    run = {"domain": "calendar", "status": None, "goal": {"desired_outcome": "schedule the dentist"}}
    with _state(run=run):
        c = _run(context_compiler.compile(uuid4(), []))
    assert "status: None" not in c.text and "schedule the dentist" in c.text


def test_trim_never_emits_a_dangling_header():
    """If a single newest turn line can't fit the remaining budget, episodic is DROPPED — never a header
    that promises recent conversation with no turns under it."""
    huge = [TurnBrief(role="user", text="x" * 4000)]
    with _state(tz="America/Chicago"):
        c = _run(context_compiler.compile(uuid4(), huge, token_budget=20))
    assert "Recent conversation" not in c.text               # no dangling header
    assert "episodic" in c.dropped or "episodic:trimmed" in c.dropped


def test_store_hiccup_degrades_one_layer_not_the_turn():
    """A store raising must omit only its layer, never crash compilation."""
    async def _boom(uid):
        raise RuntimeError("db down")

    async def _run_(uid, **kw):
        return None

    async def _events(uid, *, limit=50):
        return []

    with patch.object(world_state, "get_timezone", _boom), \
         patch.object(agent_run_store, "latest_active", _run_), \
         patch.object(entity_store, "active_events", _events):
        c = _run(context_compiler.compile(uuid4(), _turns(3)))
    assert "world" not in {b.layer for b in c.blocks}                    # the broken layer is simply absent
    assert "Recent conversation" in c.text                              # the turn still compiled
