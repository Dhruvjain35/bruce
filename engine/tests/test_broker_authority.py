"""ToolBroker authority metrics (Phase B) — the gate numbers: the broker's selection matches the legacy
hard-coded capability for every action (selection accuracy = 1.0), the shortlist stays tiny, there are ZERO
capability-truth contradictions between the broker and the registry, no live+connected action is ever
missed, and selection is fast (deterministic, no model). Connection is stubbed to measure the logic."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from bruce_engine import calendar_executor, tool_broker, tool_registry
from bruce_engine.runtime_contracts import GoalAction

CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _run(c):
    return asyncio.run(c)


@contextmanager
def _connected(ok=True, *, scopes=(CAL_SCOPE,)):
    async def _conn(uid, provider):
        return tool_broker._Conn(connected=ok, scopes=(scopes if ok else ()))
    with patch.object(tool_broker, "_provider_connection", _conn):
        yield


# The legacy selection: what the executor / handler hard-codes today, per action.
_LEGACY_CAP = {
    GoalAction.create: "calendar.create_event",
    GoalAction.update: "calendar.update_event",
    GoalAction.repair: "calendar.update_event",   # repair executes as an update
    GoalAction.delete: "calendar.delete_event",
}


def test_selection_accuracy_matches_legacy():
    """The broker's chosen capability equals the legacy hard-coded one for every calendar action -> the
    broker can BECOME the selector with zero behavior change."""
    with _connected():
        for action, expected in _LEGACY_CAP.items():
            s = _run(tool_broker.select(uuid4(), domain="calendar", action=action))
            assert s.status == "ok" and s.chosen.capability == expected, action


def test_legacy_executor_capability_agrees_with_broker():
    """The CalendarMutationExecutor's hard-coded capability (delete vs update) is exactly what the broker
    would select — the invariant that lets Phase D route the executor through broker.select()."""
    ex_del = calendar_executor.CalendarMutationExecutor("delete", {"id": "1", "title": "x"})
    ex_upd = calendar_executor.CalendarMutationExecutor("update", {"id": "1", "title": "x"},
                                                        new_start="2026-07-25T21:00:00")
    with _connected():
        assert _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.delete)).chosen.capability == ex_del.capability
        assert _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.update)).chosen.capability == ex_upd.capability


def test_shortlist_size_stays_tiny():
    with _connected():
        for action in (GoalAction.create, GoalAction.update, GoalAction.delete):
            sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=action))
            assert len(sl.candidates) == 1        # one relevant tool per concrete action, never the universe


def test_zero_capability_truth_contradictions():
    """The broker's liveness verdict must never contradict the registry's is_live — one source of truth."""
    with _connected():
        for spec in tool_registry.specs("calendar"):
            av = _run(tool_broker.availability(uuid4(), spec.capability))
            registry_live = tool_registry.is_live(spec.capability)
            broker_says_unsupported = (av.status == "unsupported")
            assert broker_says_unsupported == (not registry_live), spec.capability   # agree, always


def test_no_missed_tool_when_live_and_connected():
    """Missed-tool rate = 0: every live action, connected + scoped, yields an actionable selection."""
    with _connected():
        for action in _LEGACY_CAP:
            assert _run(tool_broker.select(uuid4(), domain="calendar", action=action)).status == "ok"


def test_selection_latency_is_negligible():
    """Deterministic, no model on the selection path."""
    with _connected():
        # warm
        _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.create))
        t0 = time.perf_counter()
        for _ in range(50):
            _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.update))
        per_call_ms = (time.perf_counter() - t0) * 1000.0 / 50
    assert per_call_ms < 5.0, f"broker.select {per_call_ms:.2f}ms/call too slow"
