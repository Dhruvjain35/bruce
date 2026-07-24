"""FastRouter latency instrumentation (G0.1) — the router must add negligible overhead and must NOT pay
for the compact model tier when a deterministic Stage 0 signal already decides. Guards the hot path against
someone slipping heavy work (a model call, a network hop) into Stage 0. DB is stubbed so this measures the
routing logic itself, not Cloud SQL round-trips (those are the same queries the pipeline already runs)."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import uuid4

from bruce_engine import entity_resolution, fast_router, mission_kernel
from bruce_engine.entity_resolution import ResolutionResult

# a generous ceiling: pure routing measures ~0.1 ms locally; 15 ms catches a real regression (a model/network
# call sneaking into Stage 0) without flaking on a slow CI runner.
_STAGE0_BUDGET_MS = 15.0


def _run(c):
    return asyncio.run(c)


def _stub(fn, *, entity="not_found", pending=None):
    async def _resolve(uid, t):
        return ResolutionResult("resolved", entity={"id": "1", "title": "x"}) if entity == "resolved" else ResolutionResult("not_found")

    async def _recent(uid):
        return ResolutionResult("resolved", entity={"id": "1"}) if entity == "resolved" else ResolutionResult("not_found")

    async def _pending(uid):
        return {"mission_id": "m1"} if pending else None

    with patch.object(entity_resolution, "resolve", _resolve), \
         patch.object(entity_resolution, "resolve_most_recent", _recent), \
         patch.object(mission_kernel, "latest_pending_calendar_mission", _pending):
        return _run(fn())


def test_timing_is_populated_and_consistent():
    """total_ms is always the sum of the two stage timings — no unaccounted work in the hot path."""
    d, t = _stub(lambda: fast_router.route(uuid4(), "add dentist tmr at 3pm"))
    assert t.stage0_ms >= 0.0
    assert t.stage1_ms >= 0.0
    assert abs(t.total_ms - (t.stage0_ms + t.stage1_ms)) < 1e-6


def test_deterministic_decision_skips_stage1():
    """A concrete scheduling text is decided by Stage 0 alone — the compact model tier is never entered,
    so stage1_ms stays 0 and total == stage0 (the whole point of the cheapest-first router)."""
    d, t = _stub(lambda: fast_router.route(uuid4(), "add dentist tmr at 3pm"))
    assert d.source == "deterministic"
    assert t.stage1_ms == 0.0
    assert t.total_ms == t.stage0_ms


def test_unrouted_chat_falls_to_stage1():
    """Pure chit-chat has no Stage 0 signal -> it escalates to Stage 1 (the default responder until a
    compact router model is wired), and the timing reflects that both stages ran."""
    d, t = _stub(lambda: fast_router.route(uuid4(), "lmaooo thats so real"))
    assert d.execution_class.value == "fast_conversation"
    assert d.source == "router_default"
    assert t.total_ms == t.stage0_ms + t.stage1_ms


def test_routing_stays_under_latency_budget():
    """Deterministic routing (DB stubbed) stays far under budget across varied intents — a regression that
    adds a model/network call to the hot path would blow this ceiling."""
    texts = [
        "add dentist tmr at 3pm", "move chess to 9pm", "delete chess class", "im in cst",
        "stay on top of the bio group proj til friday", "hru today", "ya add it",
        "send email to coach thursday", "whats the move tonight", "lol ok",
    ]
    for txt in texts:
        _d, t = _stub(lambda: fast_router.route(uuid4(), txt), entity="resolved")
        assert t.total_ms < _STAGE0_BUDGET_MS, f"routing {txt!r} took {t.total_ms:.2f}ms (> {_STAGE0_BUDGET_MS}ms)"
