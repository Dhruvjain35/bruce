"""General AgentRun loop harness (G0.4) — the loop drives a CapabilityExecutor over the frozen contracts,
persists a durable run, and commits its terminal state STRICTLY from the executor's ToolResult. Verified
against real Postgres (the loop is the first live writer of AgentRun) with a fake executor simulating each
provider outcome. The parity the calendar path relies on: a verified result -> completed, an unauthorized
result -> blocked (resumable, not done), anything else -> failed, and 'done' is never fabricated."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_loop, agent_run_store
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.runtime_contracts import (ActionType, ExecutionClass, NextAction, Risk, ToolOutcome,
                                            ToolResult)

users = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


class FakeExecutor:
    """A live-capability executor whose ToolResult is scripted (or raises), to exercise the loop's state
    machine. `capability` defaults to a real live cap so the registry's is_live gate passes."""

    domain = "calendar"

    def __init__(self, tr: ToolResult | None = None, *, raises: bool = False,
                 capability: str = "calendar.update_event", op: str = "update_event") -> None:
        self._tr = tr
        self._raises = raises
        self.capability = capability
        self._op = op
        self.executed = False

    def goal(self) -> dict:
        return {"action": "update", "desired_outcome": "move chess to 9pm"}

    def build_action(self) -> NextAction:
        return NextAction(type=ActionType.call_tool, capability=self.capability, provider="google_calendar",
                          operation=self._op, arguments={"new_start": "2026-07-25T21:00:00"}, risk=Risk.medium)

    async def execute(self, user_id: UUID) -> ToolResult:
        self.executed = True
        if self._raises:
            raise RuntimeError("provider blew up")
        return self._tr


def _tr(outcome, *, verified=False, cap="calendar.update_event", op="update_event", reason=""):
    return ToolResult(outcome, cap, "google_calendar", op, verified=verified,
                      provider_entity_id="evt123", reason=reason)


def test_verified_result_completes_the_run_and_records_it():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    ex = FakeExecutor(_tr(ToolOutcome.ok, verified=True, reason="read-back matched"))
    res = _run(agent_loop.run_direct_action(uid, executor=ex))
    assert res.status == "completed" and res.verified is True and ex.executed
    # persisted + terminal (completed runs are not "active")
    run = _run(agent_run_store.get_run(uid, UUID(res.run_id)))
    assert run["status"] == "completed"
    assert run["last_tool_result"]["verified"] is True
    assert run["current_action"]["operation"] == "update_event"
    assert _run(agent_run_store.latest_active(uid)) is None


def test_unauthorized_is_blocked_and_stays_resumable():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    ex = FakeExecutor(_tr(ToolOutcome.unauthorized, reason="google_calendar_not_connected"))
    res = _run(agent_loop.run_direct_action(uid, executor=ex))
    assert res.status == "blocked" and res.verified is False
    run = _run(agent_run_store.get_run(uid, UUID(res.run_id)))
    assert run["status"] == "blocked" and run["blocked_reason"] == "google_calendar_not_connected"
    # blocked is NOT terminal — the run remains active so it can resume after reconnect
    assert _run(agent_run_store.latest_active(uid)) is not None


def test_verification_failed_is_failed_not_done():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    ex = FakeExecutor(_tr(ToolOutcome.verification_failed, reason="read-back mismatch"))
    res = _run(agent_loop.run_direct_action(uid, executor=ex))
    assert res.status == "failed" and res.verified is False           # never claims done on a write alone


def test_executor_raise_is_contained_as_failed():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    ex = FakeExecutor(raises=True)
    res = _run(agent_loop.run_direct_action(uid, executor=ex))
    assert res.status == "failed" and res.verified is False
    assert res.tool_result.outcome is ToolOutcome.provider_error
    assert res.tool_result.reason == "executor_raised"


def test_non_live_capability_short_circuits_without_executing():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    ex = FakeExecutor(_tr(ToolOutcome.ok, verified=True), capability="calendar.search_events", op="search_events")
    res = _run(agent_loop.run_direct_action(uid, executor=ex))
    assert res.status == "failed" and ex.executed is False            # registry says not live -> never dispatched
    assert res.tool_result.reason == "capability_not_live"


def test_idempotency_key_reuses_the_same_run():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    ex1 = FakeExecutor(_tr(ToolOutcome.ok, verified=True))
    a = _run(agent_loop.run_direct_action(uid, executor=ex1, idempotency_key="k-move-chess"))
    b = _run(agent_loop.run_direct_action(uid, executor=FakeExecutor(_tr(ToolOutcome.ok, verified=True)),
                                          idempotency_key="k-move-chess"))
    assert a.run_id == b.run_id                                       # same idempotency key -> same run


def test_run_store_failure_never_breaks_the_verified_action():
    """Finding A regression: if the AgentRun store is completely down (create AND update raise), the loop
    STILL runs the verified tool and returns its real ToolResult — audit is best-effort, the mutation is not.
    Before this guard a run-store hiccup could lose a verified Google write's success confirmation."""
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))

    async def _boom(*a, **k):
        raise RuntimeError("db pool exhausted")

    ex = FakeExecutor(_tr(ToolOutcome.ok, verified=True, reason="read-back matched"))
    from unittest.mock import patch
    with patch.object(agent_run_store, "create_run", _boom), \
         patch.object(agent_run_store, "update_run", _boom):
        res = _run(agent_loop.run_direct_action(uid, executor=ex))
    assert ex.executed is True                                        # the provider tool still ran
    assert res.status == "completed" and res.verified is True         # the verified result still flows back
    assert res.run_id == ""                                           # no durable run, but the action stands


def test_planning_tier_selection():
    assert agent_loop.planning_tier(ExecutionClass.direct_action) == 0
    assert agent_loop.planning_tier(ExecutionClass.foreground_agent) == 1
    assert agent_loop.planning_tier(ExecutionClass.background_mission) == 1
    assert agent_loop.planning_tier(ExecutionClass.direct_action, needs_deeper_planning=True) == 2
