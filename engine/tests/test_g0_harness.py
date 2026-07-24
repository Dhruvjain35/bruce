"""G0 COMPLETE HARNESS — the thin-waist end-to-end, asserting the G0 definition-of-done.

Wires the real components together (FastRouter -> ContextCompiler -> ToolBroker -> AgentRun loop ->
background runner -> ResponseComposer) against real Postgres with the real FakeCalendarAdapter, and asserts
the whole spine behaves:
  * the CHEAPEST correct execution path is chosen per input (chat != action != mission);
  * the context is BOUNDED and carries the world model;
  * the tool shortlist is the RIGHT few tools, capability-truthful;
  * a direct action EXECUTES + VERIFIES through the durable loop (a run is persisted, verified by read-back);
  * a handoff runs DURABLY in the background (claimed via lease, advanced, completed);
  * no reply FABRICATES a completion;
  * routing is FAST (deterministic, no model on the hot path).

Each component has its own unit harness; this proves they COMPOSE.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (agent_run_store, background_runner, calendar_adapter, calendar_mutation,
                          calendar_tools, context_compiler, entity_store, fast_router, response_composer,
                          tool_broker, tool_registry, world_state)
from bruce_engine.db import worker_session
from bruce_engine.models import CalendarEvent
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.runtime_contracts import ExecutionClass, GoalAction

users = PostgresUserRepository()
ACCOUNT = "me@example.com"
_ROUTER_BUDGET_MS = 25.0


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _bound_ok(_uid):
    return object()


def _seed_chess(uid):
    """Record the chess-club entity + pre-seed the fake provider so an update read-back can verify."""
    _run(entity_store.record_event(
        uid, title="chess club", start="2026-07-25T15:00:00", end="2026-07-25T16:00:00",
        timezone="America/Chicago", location=None, provider="google_calendar",
        provider_account_id=ACCOUNT, provider_event_id="evt_chess", source_message_ids=["m1"]))
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)
    _run(adapter.insert(CalendarEvent(title="chess club", start="2026-07-25T15:00:00",
                                      end="2026-07-25T16:00:00", timezone="America/Chicago"), "evt_chess"))
    return adapter


def _user():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    return uid


# --- 1. cheapest correct path per input (router is the entry of the thin-waist) -------------------

def test_router_picks_the_cheapest_correct_path():
    uid = _user()
    _run(fast_router.route(uid, "warmup"))     # pay the one-time lazy import (pydantic_ai) that a live
                                               # process has already paid at startup; then time the hot path
    d_chat, t_chat = _run(fast_router.route(uid, "lmaooo thats so real"))
    d_sched, t_sched = _run(fast_router.route(uid, "add dentist appt tmr at 3pm"))
    d_hand, t_hand = _run(fast_router.route(uid, "stay on top of the bio group project til friday"))

    assert d_chat.execution_class == ExecutionClass.fast_conversation
    assert d_sched.execution_class == ExecutionClass.direct_action and d_sched.action == GoalAction.create
    assert d_hand.execution_class == ExecutionClass.background_mission
    # fast: deterministic, no model on the hot path
    for t in (t_chat, t_sched, t_hand):
        assert t.total_ms < _ROUTER_BUDGET_MS


# --- 2. context is bounded + carries the world model ----------------------------------------------

def test_context_is_bounded_and_grounded():
    uid = _user()
    _run(world_state.set_timezone(uid, "America/Chicago"))
    _seed_chess(uid)
    compiled = _run(context_compiler.compile(uid, [], token_budget=1200))
    assert compiled.est_tokens <= 1200
    assert "central time" in compiled.text           # world layer
    assert "chess club" in compiled.text             # entity layer


# --- 3. tool shortlist is the right few, capability-truthful ---------------------------------------

def test_broker_shortlists_the_right_tool():
    uid = _user()

    _CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"

    async def _conn(u, provider):                       # Phase B: broker's single connection probe
        return tool_broker._Conn(connected=True, scopes=(_CAL_SCOPE,))
    with patch.object(tool_broker, "_provider_connection", _conn):
        sl_create = _run(tool_broker.shortlist(uid, domain="calendar", action=GoalAction.create,
                                               candidate_capabilities=("calendar.create_event",)))
        sl_search = _run(tool_broker.shortlist(uid, domain="calendar", action=GoalAction.search))
    assert [c.capability for c in sl_create.candidates] == ["calendar.create_event"]
    assert sl_create.has_actionable
    assert sl_search.candidates == () and "calendar.search_events" in sl_search.excluded_dead


# --- 4. a direct action executes + verifies through the durable loop -------------------------------

def test_mutation_executes_and_verifies_through_the_loop():
    uid = _user()
    _run(world_state.set_timezone(uid, "America/Chicago"))
    adapter = _seed_chess(uid)

    # router classifies it as a direct-action update
    d, _t = _run(fast_router.route(uid, "move chess club to 9pm"))
    assert d.execution_class == ExecutionClass.direct_action and d.action == GoalAction.update

    # the mutation runs THROUGH agent_loop.run_direct_action (inside handle) -> verified reply
    with patch.object(calendar_tools, "_bound", _bound_ok):
        reply = _run(calendar_mutation.handle(uid, "update", "move chess club to 9pm", adapter=adapter))
    assert "chess club is now" in reply and "✅" in reply

    # a durable AgentRun was persisted and COMPLETED (verified), not left dangling
    async def _completed_calendar_runs():
        async with worker_session() as s:
            return (await s.execute(sa_text(
                "SELECT count(*) FROM agent_runs WHERE user_id = :u AND domain = 'calendar' "
                "AND status = 'completed'"), {"u": str(uid)})).scalar()
    assert _run(_completed_calendar_runs()) >= 1

    # the response guard trusts the verified action handler's copy, and downgrades a fabrication
    assert response_composer.no_false_completion(reply, handler="calendar_mutation") == reply
    fake = "sure, i added chess club to ur calendar ✅"
    assert response_composer.no_false_completion(fake, handler="default_reply") != fake


# --- 5. a handoff runs durably in the background ---------------------------------------------------

def test_background_mission_runs_durably():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid, domain="mission",
                                                 goal={"desired_outcome": "watch the bio project"}))
    r = background_runner.BackgroundRunner(worker_id="g0-harness")
    assert _run(r.run_once()) is True                # claimed via lease + advanced
    async def _status():
        async with worker_session() as s:
            return (await s.execute(sa_text("SELECT status FROM agent_runs WHERE id = :id"),
                                    {"id": run["id"]})).scalar()
    assert _run(_status()) == "completed"
    assert _run(r.run_once()) is False               # nothing left — no busy loop
