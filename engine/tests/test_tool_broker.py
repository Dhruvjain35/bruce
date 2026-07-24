"""ToolBroker harness (G0.3) — proves the broker returns a SHORTLIST (relevant, live, capability-truthful),
never the whole registry: the right tool is shortlisted and ranked first, dead tools (not live yet) and
non-tool actions are excluded honestly, per-user availability is reflected, and the list is bounded. Registry
availability is stubbed so this measures the brokering logic, not the DB/OAuth."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from bruce_engine import tool_broker, tool_registry
from bruce_engine.runtime_contracts import GoalAction


def _run(c):
    return asyncio.run(c)


@contextmanager
def _connected(is_available: bool):
    async def _avail(cap, uid):
        return is_available
    with patch.object(tool_registry, "is_available", _avail):
        yield


def _caps(sl):
    return [c.capability for c in sl.candidates]


def test_create_shortlists_only_create_not_the_universe():
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.create,
                                        candidate_capabilities=("calendar.create_event",)))
    assert _caps(sl) == ["calendar.create_event"]             # NOT update/delete/search
    assert sl.candidates[0].available and sl.has_actionable
    assert sl.candidates[0].reason.startswith("router prior + action match")


def test_repair_maps_to_update_operation():
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.repair))
    assert _caps(sl) == ["calendar.update_event"]             # a repair is a corrective update


def test_delete_shortlists_only_delete():
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.delete))
    assert _caps(sl) == ["calendar.delete_event"]


def test_dead_tool_is_excluded_honestly_not_offered():
    """search_events is live=False in the registry -> it must never be a candidate, and the broker records
    it as excluded_dead so the planner can say 'search isn't live yet' instead of proposing it."""
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.search))
    assert sl.candidates == ()
    assert "calendar.search_events" in sl.excluded_dead
    assert sl.has_actionable is False


def test_unavailable_when_provider_not_connected():
    """A live tool the user hasn't connected stays a candidate (so we can say 'connect your calendar') but
    is not actionable."""
    with _connected(False):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.create))
    assert _caps(sl) == ["calendar.create_event"]
    assert sl.candidates[0].available is False
    assert sl.has_actionable is False
    assert "calendar.create_event" in sl.unavailable
    assert "not connected" in sl.candidates[0].reason


def test_router_prior_ranks_named_capability_first():
    """When the router names a capability, it outranks a mere action match."""
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.update,
                                        candidate_capabilities=("calendar.update_event",)))
    assert sl.candidates[0].capability == "calendar.update_event"
    assert sl.candidates[0].score >= 1.0


def test_non_tool_action_shortlists_nothing():
    for action in (GoalAction.answer, GoalAction.remember, GoalAction.plan, GoalAction.coordinate):
        with _connected(True):
            sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=action))
        assert sl.candidates == () and sl.has_actionable is False


def test_unknown_domain_has_no_tools():
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="email", action=GoalAction.send))
    assert sl.candidates == () and sl.excluded_dead == ()


def test_shortlist_is_bounded():
    """Even with no action/candidate signal (all domain tools weakly relevant), the list respects `limit`."""
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=None, limit=2))
    assert len(sl.candidates) <= 2


def test_deterministic():
    with _connected(True):
        a = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=None))
        b = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=None))
    assert _caps(a) == _caps(b)


def test_availability_error_is_treated_as_unavailable():
    async def _boom(cap, uid):
        raise RuntimeError("oauth down")
    with patch.object(tool_registry, "is_available", _boom):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.create))
    assert sl.candidates[0].available is False and sl.has_actionable is False
