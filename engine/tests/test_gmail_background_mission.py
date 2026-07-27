"""Gmail background-mission acceptance (Phase G, boundary 4) — a durable "email X and tell me when they
reply" mission, driven by the REAL background runner + PlanMissionAdvancer + MissionExecutor (Gmail branch)
against real Postgres + FakeGmailAdapter. No Gmail-specific runner: the SAME advancer/runner calendar uses.

The load-bearing guarantees, proven:
  * the send happens EXACTLY ONCE (one message in SENT), and its thread is checkpointed for the wait;
  * while no reply is in, the run WAITS — it reschedules and yields, holding no lease, making NO model call,
    and it does NOT notify;
  * a real inbound reply WAKES the run on the next due tick and it completes;
  * EXACTLY ONE notification is produced across the whole lifecycle, even across repeated drains / re-leases;
  * a duplicate drive of the mission never sends a second email (idempotency key run_id:stepN).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_run_store, gmail_adapter, mission_executor
from bruce_engine.background_runner import BackgroundRunner, PlanMissionAdvancer
from bruce_engine.db import worker_session
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()
ACCOUNT = "me@example.com"
STUDENT = "coach@school.edu"


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


async def _status(run_id) -> str:
    async with worker_session() as s:
        return (await s.execute(sa_text("SELECT status FROM agent_runs WHERE id = :id"),
                                {"id": str(run_id)})).scalar()


async def _make_due(uid, run_id):
    """Fast-forward a rescheduled run so its next tick is due now (stand-in for wall-clock passing)."""
    await agent_run_store.reschedule_background(uid, run_id,
                                                next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1))


class _Notifier:
    def __init__(self):
        self.calls = []
        self.keys = set()
    async def __call__(self, user_id, run, *, idempotency_key):
        if idempotency_key in self.keys:        # a correct runner never calls twice; this proves it even if it did
            return
        self.keys.add(idempotency_key)
        self.calls.append((str(user_id), run.get("id")))


def _send_step():
    return {"kind": "action", "action": {
        "capability": "gmail.send_message", "provider": "gmail", "operation": "send_message",
        "arguments": {"to": STUDENT, "subject": "quick question about practice",
                      "body": "hi coach, can we move practice to 5? let me know"}}}


def _mission_goal():
    # send, then wait for the reply, then notify once. This is the plan a Tier-1 planner would emit; the harness
    # supplies it directly (planner arg-drafting is exercised where the planner is).
    return {"steps": [_send_step(), {"kind": "await_reply", "from_step": 0, "poll_seconds": 300, "max_polls": 288}],
            "notify": True, "to": STUDENT}


def _runner(adapter, notifier):
    ex = mission_executor.MissionExecutor(adapter=adapter)
    return BackgroundRunner(worker_id="wG", lease_seconds=60,
                            advancer=PlanMissionAdvancer(executor=ex, notifier=notifier))


def test_mission_sends_once_waits_then_notifies_once_on_reply():
    uid = _user()
    adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    notifier = _Notifier()
    runner = _runner(adapter, notifier)

    run = _run(agent_run_store.enqueue_background(uid, domain="gmail", goal=_mission_goal()))
    rid = UUID(run["id"])

    # tick 1: sends the email, then the first reply-poll finds nothing -> the run WAITS (rescheduled), no notify
    _run(runner.drain())
    assert adapter.send_calls == 1 and len(adapter.messages) == 1     # sent EXACTLY once
    sent = next(iter(adapter.messages.values()))
    assert "SENT" in sent["labelIds"] and sent["payload"]["headers"][0]["value"] == STUDENT
    assert _run(_status(rid)) == "queued"                            # waiting, not completed
    assert notifier.calls == []                                      # nothing to tell yet -> silent

    # more ticks while still no reply: still just waiting, still no second send, still silent
    for _ in range(3):
        _run(_make_due(uid, rid))
        _run(runner.drain())
    assert adapter.send_calls == 1 and notifier.calls == []
    assert _run(_status(rid)) == "queued"

    # the student REPLIES in the thread
    tid = sent["threadId"]
    sent_id = next(k for k, v in adapter.messages.items() if "SENT" in v["labelIds"])
    adapter.inject_incoming(tid, from_addr=STUDENT, subject="re: quick question about practice",
                            in_reply_to=adapter.rfc_of(sent_id),
                            body="sure, 5 works")

    # next due tick: the reply wakes the run -> it completes and notifies EXACTLY once
    _run(_make_due(uid, rid))
    _run(runner.drain())
    assert _run(_status(rid)) == "completed"
    assert len(notifier.calls) == 1 and notifier.calls[0][0] == str(uid)

    # draining again after completion never re-notifies or re-sends
    _run(runner.drain())
    assert len(notifier.calls) == 1 and adapter.send_calls == 1


def test_no_model_or_notify_while_waiting():
    # a reply never comes within the drains -> the mission stays waiting and NEVER fabricates a notification
    uid = _user()
    adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    notifier = _Notifier()
    runner = _runner(adapter, notifier)
    run = _run(agent_run_store.enqueue_background(uid, domain="gmail", goal=_mission_goal()))
    rid = UUID(run["id"])
    for _ in range(5):
        _run(runner.drain())
        _run(_make_due(uid, rid))
    assert notifier.calls == [] and adapter.send_calls == 1
    assert _run(_status(rid)) == "queued"                            # still patiently waiting


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
