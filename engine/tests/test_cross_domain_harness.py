"""Cross-domain harness (Phase G, boundary 5) — calendar AND Gmail run through ONE shared spine (router →
ToolBroker → the general AgentRun loop / background runner → the Phase-F renderer) with NO per-provider
router, approval, loop, or response engine. Gmail is proven to be an INHERITED hand: adding it was registry
rows + an adapter + an executor branch, not a parallel pipeline. Real Postgres + Fake provider adapters.

~20 checks across the spine:
  routing (5) · capability truth + no cross-talk (6) · execution+verification both domains (4) ·
  background mission (2) · honest response composition (3).
"""

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
from bruce_engine import (agent_loop, agent_run_store, calendar_adapter, calendar_mutation, calendar_tools,
                          entity_store, fast_router, gmail_adapter, mission_executor, response_render,
                          tool_broker, world_state)
from bruce_engine.background_runner import BackgroundRunner, PlanMissionAdvancer
from bruce_engine.db import worker_session
from bruce_engine.gmail_executor import GmailSendExecutor
from bruce_engine.models import CalendarEvent
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.runtime_contracts import ExecutionClass, GoalAction, ToolOutcome

users = PostgresUserRepository()
ACCOUNT = "me@example.com"
CAL = "https://www.googleapis.com/auth/calendar.events"
GSEND = "https://www.googleapis.com/auth/gmail.send"
GREAD = "https://www.googleapis.com/auth/gmail.readonly"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _user():
    uid = uuid4(); _run(users.ensure(uid, auth_provider="test"))
    return uid


async def _bound_ok(_uid):
    return object()


def _google(scopes):
    async def _conn(_u, _p):
        return tool_broker._Conn(connected=bool(scopes), scopes=tuple(scopes))
    return patch.object(tool_broker, "_provider_connection", _conn)


def _seed_chess(uid):
    _run(entity_store.record_event(uid, title="chess club", start="2026-07-25T15:00:00",
                                   end="2026-07-25T16:00:00", timezone="America/Chicago", location=None,
                                   provider="google_calendar", provider_account_id=ACCOUNT,
                                   provider_event_id="evt_chess", source_message_ids=["m1"]))
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)
    _run(adapter.insert(CalendarEvent(title="chess club", start="2026-07-25T15:00:00",
                                      end="2026-07-25T16:00:00", timezone="America/Chicago"), "evt_chess"))
    return adapter


# --- routing: the shared router picks the cheapest correct path for BOTH domains (5 checks) --------

def test_router_routes_both_domains_generically():
    uid = _user()
    _run(fast_router.route(uid, "warmup"))
    d_cal, _ = _run(fast_router.route(uid, "add dentist appt tmr at 3pm"))
    d_send, _ = _run(fast_router.route(uid, "email coach@school.edu that i'll be late"))
    d_mission, _ = _run(fast_router.route(uid, "email coach and let me know when they reply"))
    d_chat, _ = _run(fast_router.route(uid, "lmaooo thats so real"))
    assert d_cal.execution_class == ExecutionClass.direct_action and d_cal.domain == "calendar"      # 1
    assert d_send.execution_class == ExecutionClass.direct_action and d_send.action == GoalAction.send  # 2
    assert d_send.domain == "gmail" and "gmail.send_message" in d_send.candidate_capabilities         # 3
    assert d_mission.execution_class == ExecutionClass.background_mission and d_mission.domain == "gmail"  # 4
    assert d_chat.execution_class == ExecutionClass.fast_conversation                                 # 5


# --- capability truth + NO cross-talk: one domain never selects the other's tool (6 checks) --------

def test_capability_truth_and_no_cross_talk():
    uid = _user()
    with _google((CAL, GSEND, GREAD)):                              # fully connected google
        cal = _run(tool_broker.select(uid, domain="calendar", action=GoalAction.update,
                                      candidate_capabilities=("calendar.update_event",)))
        gm = _run(tool_broker.select(uid, domain="gmail", action=GoalAction.send,
                                     candidate_capabilities=("gmail.send_message",)))
    assert cal.chosen.capability == "calendar.update_event" and gm.chosen.capability == "gmail.send_message"  # 6,7
    assert not cal.chosen.capability.startswith("gmail") and not gm.chosen.capability.startswith("calendar")  # 8

    with _google((CAL,)):                                           # calendar-only grant
        gm2 = _run(tool_broker.select(uid, domain="gmail", action=GoalAction.send,
                                      candidate_capabilities=("gmail.send_message",)))
        cal2 = _run(tool_broker.select(uid, domain="calendar", action=GoalAction.update,
                                       candidate_capabilities=("calendar.update_event",)))
    assert gm2.status == tool_broker.INSUFFICIENT_SCOPE and gm2.chosen is None                        # 9
    assert cal2.status == tool_broker.OK                                                              # 10 (calendar unaffected)

    with _google(()):                                               # no google connection
        gm3 = _run(tool_broker.select(uid, domain="gmail", action=GoalAction.send,
                                      candidate_capabilities=("gmail.send_message",)))
    assert gm3.status == tool_broker.DISCONNECTED                                                     # 11


# --- execution + verification: BOTH domains run through the SAME durable loop (4 checks) -----------

def test_both_domains_execute_and_verify_through_one_loop():
    uid = _user()
    _run(world_state.set_timezone(uid, "America/Chicago"))
    cal_adapter = _seed_chess(uid)
    with patch.object(calendar_tools, "_bound", _bound_ok):
        cal_reply = _run(calendar_mutation.handle(uid, "update", "move chess club to 9pm", adapter=cal_adapter))
    assert "chess club is now" in cal_reply and "✅" in cal_reply                                     # 12

    gm_adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    gm_res = _run(agent_loop.run_direct_action(
        uid, executor=GmailSendExecutor(to="coach@school.edu", subject="late", body="running late",
                                        idempotency_key="req-1", adapter=gm_adapter), idempotency_key="req-1"))
    assert gm_res.verified is True and gm_res.tool_result.outcome is ToolOutcome.ok                   # 13

    async def _completed(domain):
        async with worker_session() as s:
            return (await s.execute(sa_text(
                "SELECT count(*) FROM agent_runs WHERE user_id=:u AND domain=:d AND status='completed'"),
                {"u": str(uid), "d": domain})).scalar()
    assert _run(_completed("calendar")) >= 1                                                          # 14
    assert _run(_completed("gmail")) >= 1                                                             # 15 (same loop, two hands)


# --- background mission: the SAME runner drives a gmail send→wait→notify-once (2 checks) -----------

def test_background_mission_through_shared_runner():
    uid = _user()
    adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    calls = []
    async def _notify(u, run, *, idempotency_key):
        calls.append(idempotency_key)
    runner = BackgroundRunner(worker_id="wX", advancer=PlanMissionAdvancer(
        executor=mission_executor.MissionExecutor(adapter=adapter), notifier=_notify))
    goal = {"steps": [{"kind": "action", "action": {"capability": "gmail.send_message", "provider": "gmail",
                                                    "operation": "send_message",
                                                    "arguments": {"to": "coach@school.edu", "subject": "q",
                                                                  "body": "can we move practice?"}}},
                      {"kind": "await_reply", "from_step": 0, "poll_seconds": 300}],
            "notify": True}
    run = _run(agent_run_store.enqueue_background(uid, domain="gmail", goal=goal))
    rid = UUID(run["id"])
    _run(runner.drain())
    assert adapter.send_calls == 1 and calls == []                                                    # 16 (sent, waiting, silent)
    tid = next(iter(adapter.messages.values()))["threadId"]
    sent_id = next(k for k, v in adapter.messages.items() if "SENT" in v["labelIds"])
    adapter.inject_incoming(tid, from_addr="coach@school.edu", subject="re: q", body="sure",
                            in_reply_to=adapter.rfc_of(sent_id))
    _run(agent_run_store.reschedule_background(uid, rid, next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    _run(runner.drain())
    assert len(calls) == 1                                                                            # 17 (one notification)


# --- honest response composition: the GENERIC renderer voices the verified truth (3 checks) --------

def test_generic_renderer_is_honest_for_any_hand():
    ok = response_render.spec_from_result(
        type("_R", (), {"outcome": ToolOutcome.ok, "verified": True, "provider_entity_id": "m1"})(),
        action="sent", provider="gmail")
    text_ok, _u, _m = _run(response_render.compose(ok))
    assert text_ok == "sent ✅"                                                                       # 18

    failed = response_render.spec_from_result(
        type("_R", (), {"outcome": ToolOutcome.verification_failed, "verified": False,
                        "provider_entity_id": None})(), action="sent", provider="gmail")
    text_fail, _u, _m = _run(response_render.compose(failed))
    assert "✅" not in text_fail and "nothing was sent" in text_fail                                  # 19

    disc, _u, _m = _run(response_render.compose(
        response_render.spec_from_unavailable(tool_broker.INSUFFICIENT_SCOPE, provider="gmail")))
    assert "connect" in disc and "✅" not in disc                                                     # 20


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
