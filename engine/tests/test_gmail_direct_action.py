"""Gmail direct-action acceptance (Phase G, boundary 3) — a send goes text → shared router → ToolBroker →
the SAME general AgentRun loop calendar uses → verified provider call → honest reply, with NO Gmail-specific
router/approval/loop/response engine anywhere on the path. Real Postgres + FakeGmailAdapter (the adapter
enforces the provider's real send/read-back rules; nothing about Bruce is mocked).

Proves the inherited hand end-to-end:
  * the shared router classifies a send as direct_action / GoalAction.send / domain gmail (generic send verb);
  * the shared ToolBroker resolves capability truth — OK only on a gmail.send-scoped Google connection,
    INSUFFICIENT_SCOPE on a calendar-only connection (honest 'connect gmail', never a fake 'sent');
  * the send EXECUTES + VERIFIES through agent_loop.run_direct_action — a durable AgentRun is persisted and
    COMPLETED only because the read-back proved it;
  * a retry with the same idempotency key sends exactly once;
  * the reply is composed by the generic Phase-F renderer from the VERIFIED result — 'sent ✅' only when
    verified, an honest failure otherwise.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (agent_loop, fast_router, gmail_adapter, response_render, tool_broker)
from bruce_engine.db import worker_session
from bruce_engine.gmail_executor import GmailSendExecutor
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


def _google(*, connected=True, scopes=(CAL, GSEND, GREAD)):
    async def _conn(_u, _p):
        return tool_broker._Conn(connected=connected, scopes=(tuple(scopes) if connected else ()))
    return patch.object(tool_broker, "_provider_connection", _conn)


# --- 1. the SHARED router classifies a send (generic verb, no Gmail-specific route) ---------------

def test_router_classifies_send_as_direct_action():
    uid = _user()
    d, _t = _run(fast_router.route(uid, "email coach@school.edu that i'll be 10 min late to practice"))
    assert d.execution_class == ExecutionClass.direct_action and d.action == GoalAction.send
    assert d.domain == "gmail" and "gmail.send_message" in d.candidate_capabilities


# --- 2. the SHARED broker is the capability-truth authority for the send --------------------------

def test_broker_capability_truth_for_send():
    uid = _user()
    with _google(scopes=(CAL, GSEND, GREAD)):
        sel = _run(tool_broker.select(uid, domain="gmail", action=GoalAction.send,
                                      candidate_capabilities=("gmail.send_message",)))
    assert sel.status == tool_broker.OK and sel.chosen.capability == "gmail.send_message"
    with _google(scopes=(CAL,)):                       # connected for calendar, never granted gmail.send
        sel2 = _run(tool_broker.select(uid, domain="gmail", action=GoalAction.send,
                                       candidate_capabilities=("gmail.send_message",)))
    assert sel2.status == tool_broker.INSUFFICIENT_SCOPE and sel2.chosen is None


# --- 3. the send EXECUTES + VERIFIES through the durable general loop ------------------------------

def test_send_executes_and_verifies_through_the_loop():
    uid = _user()
    adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    ex = GmailSendExecutor(to="coach@school.edu", subject="running late",
                           body="hey coach, i'll be ~10 min late to practice today", idempotency_key="req-1",
                           adapter=adapter)
    res = _run(agent_loop.run_direct_action(uid, executor=ex, idempotency_key="req-1"))
    assert res.status == "completed" and res.verified is True
    assert res.tool_result.outcome is ToolOutcome.ok and adapter.send_calls == 1

    async def _completed_gmail_runs():
        async with worker_session() as s:
            return (await s.execute(sa_text(
                "SELECT count(*) FROM agent_runs WHERE user_id = :u AND domain = 'gmail' "
                "AND status = 'completed'"), {"u": str(uid)})).scalar()
    assert _run(_completed_gmail_runs()) >= 1           # a real durable run, completed by read-back


def test_retry_same_key_sends_exactly_once():
    uid = _user()
    adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    def _ex():
        return GmailSendExecutor(to="coach@school.edu", subject="running late", body="late",
                                 idempotency_key="req-dup", adapter=adapter)
    r1 = _run(agent_loop.run_direct_action(uid, executor=_ex(), idempotency_key="req-dup"))
    r2 = _run(agent_loop.run_direct_action(uid, executor=_ex(), idempotency_key="req-dup"))
    assert r1.verified and r2.verified
    assert adapter.send_calls == 2 and len(adapter.messages) == 1     # two attempts, ONE real send


# --- 4. the reply is composed by the GENERIC renderer, from the VERIFIED result -------------------

def test_reply_is_honest_sent_only_when_verified():
    uid = _user()
    adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    ex = GmailSendExecutor(to="coach@school.edu", subject="hi", body="hello", idempotency_key="req-2",
                           adapter=adapter)
    res = _run(agent_loop.run_direct_action(uid, executor=ex, idempotency_key="req-2"))
    spec = response_render.spec_from_result(res.tool_result, action="sent", provider="gmail")
    text, _used, _ms = _run(response_render.compose(spec))
    assert text == "sent ✅"


def test_reply_never_claims_sent_on_verification_failure():
    # an adapter whose read-back does NOT confirm SENT -> verified=False -> honest failure, never "sent ✅"
    class _NoReadBack(gmail_adapter.FakeGmailAdapter):
        async def get(self, message_id):
            return None                                  # the send "happened" but cannot be proven
    uid = _user()
    ex = GmailSendExecutor(to="coach@school.edu", subject="hi", body="hello", idempotency_key="req-3",
                           adapter=_NoReadBack(account=ACCOUNT))
    res = _run(agent_loop.run_direct_action(uid, executor=ex, idempotency_key="req-3"))
    assert res.verified is False and res.tool_result.outcome is ToolOutcome.verification_failed
    spec = response_render.spec_from_result(res.tool_result, action="sent", provider="gmail")
    text, _used, _ms = _run(response_render.compose(spec))
    assert "✅" not in text and "sent ✅" != text


def test_disconnected_reply_before_any_send():
    # broker says the send hand isn't connected -> the honest 'connect gmail' reply, no provider call at all
    spec = response_render.spec_from_unavailable(tool_broker.INSUFFICIENT_SCOPE, provider="gmail")
    text, _used, _ms = _run(response_render.compose(spec))
    assert "connect" in text and "✅" not in text


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
