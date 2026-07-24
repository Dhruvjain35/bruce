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

CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def _run(c):
    return asyncio.run(c)


@contextmanager
def _connected(is_available: bool, *, scopes=(CAL_SCOPE,)):
    """Patch the broker's single connection probe. Connected => granted the calendar scope by default."""
    async def _conn(uid, provider):
        return tool_broker._Conn(connected=is_available, scopes=(scopes if is_available else ()))
    with patch.object(tool_broker, "_provider_connection", _conn):
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
    assert sl.candidates[0].status == "disconnected" and "disconnected" in sl.candidates[0].reason


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
    async def _boom(uid, provider):
        raise RuntimeError("oauth down")
    with patch.object(tool_broker, "_provider_connection", _boom):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.create))
    assert sl.candidates[0].available is False and sl.has_actionable is False


# --- Phase B: availability authority, scope filtering, single selection entry point ---------------

def test_availability_distinguishes_the_three_failure_kinds():
    """The single capability-truth check: unsupported (not live) vs disconnected vs insufficient_scope."""
    # search_events is live=False -> unsupported
    with _connected(True):
        av = _run(tool_broker.availability(uuid4(), "calendar.search_events"))
    assert av.status == "unsupported"
    # not connected -> disconnected
    with _connected(False):
        av = _run(tool_broker.availability(uuid4(), "calendar.update_event"))
    assert av.status == "disconnected"
    # connected but the grant is MISSING the calendar scope -> insufficient_scope
    with _connected(True, scopes=("openid",)):
        av = _run(tool_broker.availability(uuid4(), "calendar.update_event"))
    assert av.status == "insufficient_scope" and CAL_SCOPE in av.missing_scopes
    # connected + scoped -> ok
    with _connected(True):
        av = _run(tool_broker.availability(uuid4(), "calendar.update_event"))
    assert av.ok


def test_shortlist_scope_filter_marks_insufficient_scope():
    with _connected(True, scopes=("openid",)):        # connected, but no calendar scope
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.create))
    assert sl.candidates[0].available is False and sl.candidates[0].status == "insufficient_scope"
    assert "calendar.create_event" in sl.insufficient_scope and sl.has_actionable is False


def test_candidate_carries_compact_schema_and_scopes():
    with _connected(True):
        sl = _run(tool_broker.shortlist(uuid4(), domain="calendar", action=GoalAction.create))
    c = sl.candidates[0]
    assert c.arg_schema.get("title") == "str" and c.arg_schema.get("start") == "datetime"
    assert CAL_SCOPE in c.required_scopes


def test_select_is_the_single_entry_point():
    with _connected(True):
        s = _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.update,
                                    candidate_capabilities=("calendar.update_event",)))
    assert s.status == "ok" and s.chosen is not None and s.chosen.capability == "calendar.update_event"


def test_select_typed_non_selection():
    # disconnected
    with _connected(False):
        s = _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.create))
    assert s.status == "disconnected" and s.chosen is None
    # unsupported (search not live)
    with _connected(True):
        s = _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.search))
    assert s.status == "unsupported" and s.chosen is None
    # insufficient scope
    with _connected(True, scopes=("openid",)):
        s = _run(tool_broker.select(uuid4(), domain="calendar", action=GoalAction.delete))
    assert s.status == "insufficient_scope" and s.chosen is None
    # no tool at all (unknown domain)
    with _connected(True):
        s = _run(tool_broker.select(uuid4(), domain="email", action=GoalAction.send))
    assert s.status == "no_tool" and s.chosen is None


def test_kill_switch_flag(monkeypatch):
    assert tool_broker.authority_enabled() is True
    monkeypatch.setenv("BRUCE_BROKER_AUTHORITY_OFF", "true")
    assert tool_broker.authority_enabled() is False
