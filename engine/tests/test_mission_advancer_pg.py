"""Real background advancer (Phase E) against Postgres — the runner drives a mission through its plan for
real: it advances + VERIFIES each step, CHECKPOINTS progress (resume after a restart redoes nothing), WAITS
without any work, NOTIFIES exactly once (even across a restart), CANCELS, and DEAD-LETTERS after the budget.
A genuine calendar step (via the real MissionExecutor + FakeCalendarAdapter) proves it is not a no-op."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (agent_run_store, background_runner, calendar_adapter, calendar_tools,
                          entity_store, mission_executor)
from bruce_engine.background_runner import BackgroundRunner, PlanMissionAdvancer
from bruce_engine.db import worker_session
from bruce_engine.models import CalendarEvent
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.runtime_contracts import ToolOutcome, ToolResult

users = PostgresUserRepository()
ACCOUNT = "me@example.com"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _row(run_id):
    async with worker_session() as s:
        r = (await s.execute(sa_text("SELECT status, recovery_state, next_run_at FROM agent_runs WHERE id=:id"),
                             {"id": str(run_id)})).mappings().first()
    return dict(r) if r else None


async def _due(run_id):
    async with worker_session() as s:
        await s.execute(sa_text("UPDATE agent_runs SET next_run_at = now() - interval '1 s', "
                                "lease_expires_at = now() - interval '1 s' WHERE id=:id"), {"id": str(run_id)})


def _user():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    return uid


def _act(cap="calendar.update_event", **args):
    return {"kind": "action", "action": {"capability": cap, "provider": "google_calendar",
                                         "operation": cap.split(".")[1], "target_entity_id": "e1",
                                         "arguments": args}}


def _ok():
    return ToolResult(ToolOutcome.ok, "calendar.update_event", "google_calendar", "update_event", verified=True)


class FakeExec:
    def __init__(self, results=None):
        self._results = list(results) if results else None
        self.calls = []
        self.keys = []
    async def execute(self, user_id, action, *, idempotency_key=None):
        self.calls.append(action.capability)
        self.keys.append(idempotency_key)
        return (self._results.pop(0) if self._results else _ok())


class Notifier:
    def __init__(self):
        self.count = 0
        self.keys = []
    async def __call__(self, user_id, run, *, idempotency_key=None):
        self.count += 1
        self.keys.append(idempotency_key)


# --- genuine advancement + notify-once ------------------------------------------------------------

def test_plan_advances_all_steps_completes_and_notifies_once():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid, goal={"steps": [_act(), _act()], "notify": True}))
    ex, notif = FakeExec(), Notifier()
    r = BackgroundRunner(worker_id="w1", advancer=PlanMissionAdvancer(executor=ex, notifier=notif))
    assert _run(r.run_once()) is True
    row = _run(_row(run["id"]))
    assert row["status"] == "completed" and len(ex.calls) == 2      # BOTH steps executed under one claim
    assert notif.count == 1 and row["recovery_state"]["notified"] is True
    assert ex.keys == [f"{run['id']}:step0", f"{run['id']}:step1"]   # stable per-step idempotency keys
    assert notif.keys == [f"{run['id']}:notify"]                      # a dedup key for the notification too


def test_checkpoint_resume_does_not_redo_a_completed_step():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid, goal={"steps": [_act(), _act()]}))
    # step 0 verifies, step 1 FAILS -> reschedule after step 0 is checkpointed
    ex = FakeExec([_ok(), ToolResult(ToolOutcome.verification_failed, "calendar.update_event",
                                     "google_calendar", "update_event", verified=False, reason="x")])
    r = BackgroundRunner(worker_id="w1", advancer=PlanMissionAdvancer(executor=ex))
    _run(r.run_once())
    assert _run(_row(run["id"]))["recovery_state"]["step_index"] == 1   # checkpointed past step 0
    # restart: a fresh runner reclaims and finishes — step 0 must NOT run again
    _run(_due(run["id"]))
    ex2 = FakeExec([_ok()])
    r2 = BackgroundRunner(worker_id="w2", advancer=PlanMissionAdvancer(executor=ex2))
    _run(r2.run_once())
    assert _run(_row(run["id"]))["status"] == "completed"
    assert ex2.calls == ["calendar.update_event"]                  # ONLY the remaining step, no redo


def test_notify_fires_exactly_once_across_a_respawn():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid, goal={"steps": [_act()], "notify": True}))
    notif = Notifier()
    r = BackgroundRunner(worker_id="w1", advancer=PlanMissionAdvancer(executor=FakeExec(), notifier=notif))
    _run(r.run_once())
    assert notif.count == 1
    # a spurious re-queue must NOT re-notify (notified flag is checkpointed)
    async def _requeue():
        async with worker_session() as s:
            await s.execute(sa_text("UPDATE agent_runs SET status='queued', next_run_at=now()-interval '1 s', "
                                    "lease_owner=NULL WHERE id=:id"), {"id": run["id"]})
    _run(_requeue())
    _run(BackgroundRunner(worker_id="w9", advancer=PlanMissionAdvancer(executor=FakeExec(), notifier=notif)).run_once())
    assert notif.count == 1                                         # still one — never double-notified


# --- wait / wake, cancel, dead-letter -------------------------------------------------------------

def test_wait_step_yields_without_executing_downstream():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(
        uid, goal={"steps": [_act(), {"kind": "wait", "seconds": 3600}, _act()]}))
    ex = FakeExec()
    r = BackgroundRunner(worker_id="w1", advancer=PlanMissionAdvancer(executor=ex))
    _run(r.run_once())
    row = _run(_row(run["id"]))
    assert row["status"] == "queued" and row["next_run_at"] is not None
    assert len(ex.calls) == 1                                       # step 0 ran; the post-wait step did NOT
    assert _run(agent_run_store.claim_background("w2")) is None     # not due (waiting in the future)


def test_cancel_makes_it_unclaimable():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid, goal={"steps": [_act()]}))
    _run(agent_run_store.cancel_background(uid, UUID(run["id"])))
    assert _run(_row(run["id"]))["status"] == "cancelled"
    r = BackgroundRunner(worker_id="w1", advancer=PlanMissionAdvancer(executor=FakeExec()))
    assert _run(r.run_once()) is False                             # cancelled -> nothing to claim


def test_repeatedly_failing_step_dead_letters():
    uid = _user()
    run = _run(agent_run_store.enqueue_background(uid, goal={"steps": [_act()]}))
    async def _cap1():
        async with worker_session() as s:
            await s.execute(sa_text("UPDATE agent_runs SET max_attempts=1 WHERE id=:id"), {"id": run["id"]})
    _run(_cap1())

    class AlwaysFail:
        async def execute(self, user_id, action, *, idempotency_key=None):
            return ToolResult(ToolOutcome.verification_failed, "calendar.update_event", "google_calendar",
                              "update_event", verified=False, reason="nope")

    r = BackgroundRunner(worker_id="w1", advancer=PlanMissionAdvancer(executor=AlwaysFail()))
    _run(r.run_once())
    assert _run(_row(run["id"]))["status"] == "dead_letter"


# --- genuine calendar advance (NOT a no-op) -------------------------------------------------------

def test_real_calendar_step_advances_and_verifies():
    uid = _user()
    _run(entity_store.record_event(uid, title="chess club", start="2026-07-25T15:00:00",
         end="2026-07-25T16:00:00", timezone="America/Chicago", location=None, provider="google_calendar",
         provider_account_id=ACCOUNT, provider_event_id="evt_c", source_message_ids=["m1"]))
    ent = _run(entity_store.get_entity(uid, None)) if False else None   # id resolved below
    # find the entity id
    async def _eid():
        evs = await entity_store.active_events(uid, limit=1)
        return evs[0]["id"]
    eid = _run(_eid())
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)
    _run(adapter.insert(CalendarEvent(title="chess club", start="2026-07-25T15:00:00",
                                      end="2026-07-25T16:00:00", timezone="America/Chicago"), "evt_c"))
    step = {"kind": "action", "action": {"capability": "calendar.update_event", "provider": "google_calendar",
            "operation": "update_event", "target_entity_id": eid,
            "arguments": {"new_start": "2026-07-25T21:00:00", "new_end": "2026-07-25T22:00:00",
                          "new_timezone": "America/Chicago"}}}
    run = _run(agent_run_store.enqueue_background(uid, goal={"steps": [step]}))

    async def _bound_ok(_u):
        return object()
    with patch.object(calendar_tools, "_bound", _bound_ok):
        r = BackgroundRunner(worker_id="w1",
                             advancer=PlanMissionAdvancer(executor=mission_executor.MissionExecutor(adapter=adapter)))
        assert _run(r.run_once()) is True
    assert _run(_row(run["id"]))["status"] == "completed"          # a REAL, read-back-verified calendar move


# --- execution boundary -------------------------------------------------------------------------------
# THIS FILE DOES NOT EXERCISE AUTHORIZATION. Every test above is about provider semantics — read-back
# verification, 409 handling, idempotent retry, account binding, run bookkeeping — and calls the verified
# I/O directly rather than through the turn that would mint consent for it. Suspending the gate keeps
# those assertions about the thing they are actually testing.
#
# The boundary itself is proven in test_authorization_evidence.py and test_authorization_zero_call.py,
# which never import this seam. `unchecked_provider_writes_for_test` raises outside pytest, so this is a
# statement about a test file, not a hole in the engine.
@pytest.fixture(autouse=True)
def _provider_semantics_not_authorization():
    from bruce_engine import execution_gate
    with execution_gate.unchecked_provider_writes_for_test():
        yield
