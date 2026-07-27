"""Durable authorization — a mission carries a POINTER, and the world is rejudged when it wakes.

#120 kept consent in memory for the length of a turn. That is safe and cannot serve background work: a
mission executes minutes or days after the turn that planned it, so `MissionExecutor` refused every write
and durable execution was impossible.

The fix is not to let a mission carry the verdict. A copied `approved=True` stays true through the
student changing their mind, editing the time, saying never mind, and disconnecting the account — which
is to say it stays true through exactly the events the wait exists to expose. So a mission carries an
authorization_id and nothing else, and `authorization_store.recheck` rejudges all ten conditions against
the world as it is at the moment of execution.

These tests are about the GAP. Every one of them authorizes something legitimately, then changes the
world the way it actually changes between planning and waking, and asserts the provider is never touched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (access_control, authorization_evidence as ae, authorization_store, crypto,
                          execution_gate, gmail_adapter, mission_executor, mutation_gateway,
                          oauth_google, schema, tool_broker)
from bruce_engine import user_action_boundary as uab
from bruce_engine.db import user_session
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.runtime_contracts import ActionType, NextAction, ToolOutcome

users = PostgresUserRepository()
ACCOUNT = "me@example.com"
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SEND_ARGS = {"to": "coach@school.edu", "subject": "saturday", "body": "hey coach, quick q"}


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _seed(uid, *, connected=True):
    await users.ensure(uid, auth_provider="test")
    await access_control.enroll_staging_test(uid, actor="test", reason="durable authorization tests")
    if connected:
        async with user_session(uid) as s:
            s.add(schema.Integration(
                user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
                scopes=[CAL_SCOPE, SEND_SCOPE], refresh_token_encrypted=crypto.encrypt("rt-secret"),
                selected_calendar_id="primary", status="connected"))


def _user(**kw):
    uid = uuid4()
    _run(_seed(uid, **kw))
    return uid


def _grant(uid, *, args=None, **over):
    kw = dict(user_id=uid, provider="gmail", operation="send_message", arguments=args or SEND_ARGS,
              authorization_type=ae.AuthorizationType.direct_explicit,
              boundary=uab.evaluate("yes do it", has_pending_decision=True),
              trusted_message_id="m-1", source_message_timestamp=datetime.now(timezone.utc),
              conversation_id="conv-1", explicit_operation_request=True)
    kw.update(over)
    return ae.grant(**kw)


def _stored(uid, **over):
    ev = _grant(uid, **over)
    _run(authorization_store.record(ev, mission_id=uuid4()))
    return ev


def _recheck(uid, ev, **over):
    kw = dict(provider="gmail", operation="send_message", arguments=SEND_ARGS,
              conversation_id="conv-1")
    kw.update(over)
    return _run(authorization_store.recheck(uid, ev.authorization_id if ev else None, **kw))[1]


# --- persistence ----------------------------------------------------------------------------------------

def test_an_authorization_survives_the_turn_that_created_it():
    uid = _user()
    ev = _stored(uid)
    back = _run(authorization_store.load(uid, ev.authorization_id))
    assert back is not None
    assert back.arguments_fingerprint == ev.arguments_fingerprint
    assert back.authorization_type is ae.AuthorizationType.direct_explicit
    assert back.trusted_message_id == "m-1" and back.explicit_operation_request is True
    assert _recheck(uid, ev) == ae.ALLOW


def test_another_students_authorization_id_loads_as_nothing():
    """RLS, not a WHERE clause. A mission holding an id it cannot load has nothing, and the two failure
    modes — never existed, belongs to someone else — are deliberately the same answer here."""
    mine, theirs = _user(), _user()
    ev = _stored(theirs)
    assert _run(authorization_store.load(mine, ev.authorization_id)) is None
    assert _recheck(mine, ev) == authorization_store.UNKNOWN_AUTHORIZATION


def test_a_garbage_authorization_id_is_refused_not_crashed():
    uid = _user()
    assert _run(authorization_store.recheck(
        uid, "not-a-uuid", provider="gmail", operation="send_message",
        arguments=SEND_ARGS))[1] == authorization_store.UNKNOWN_AUTHORIZATION


# --- the ten conditions, each broken the way it actually breaks ------------------------------------------

def test_the_arguments_changed_while_the_mission_slept():
    """The student approved an email to the coach and then edited the recipient. The old consent does not
    cover the new act, and the fingerprint is what notices."""
    uid = _user()
    ev = _stored(uid)
    assert _recheck(uid, ev, arguments={**SEND_ARGS, "to": "principal@school.edu"}) == ae.ARGUMENTS_CHANGED
    assert _recheck(uid, ev, arguments={**SEND_ARGS, "body": "something else entirely"}) == ae.ARGUMENTS_CHANGED


def test_the_operation_changed_while_the_mission_slept():
    uid = _user()
    ev = _stored(uid)
    assert _recheck(uid, ev, operation="reply_to_thread") == ae.OPERATION_MISMATCH
    assert _recheck(uid, ev, provider="google_calendar") == ae.PROVIDER_MISMATCH


def test_consent_given_in_one_conversation_does_not_execute_in_another():
    uid = _user()
    ev = _stored(uid)
    assert _recheck(uid, ev, conversation_id="a-different-thread") == ae.WRONG_CONVERSATION


def test_an_authorization_that_aged_out_before_the_mission_woke():
    uid = _user()
    old = datetime.now(timezone.utc) - timedelta(days=2)
    ev = _stored(uid, source_message_timestamp=old, now=old)
    assert _recheck(uid, ev) == ae.EXPIRED


def test_a_later_refusal_stops_a_mission_authorized_before_it():
    """The ordinary invalidation chain: a refusal closes every outstanding authorization."""
    uid = _user()
    ev = _stored(uid)
    _run(authorization_store.record_refusal(
        uid, uab.evaluate("actually nah dont send it"), message_id="m-2"))
    assert _recheck(uid, ev) == ae.INVALIDATED
    assert _run(authorization_store.load(uid, ev.authorization_id)).invalidated_by_message_id == "m-2"


def test_a_refusal_with_nothing_outstanding_still_stops_a_mission_that_wakes_later():
    """The reason refusals get their own table.

    A student says "never mind" at a moment when the mission's row was already consumed-adjacent, closed,
    or simply missed by the UPDATE. Nothing marks the authorization. The refusal still has to stop work
    that was authorized before it and wakes after it, so execution compares the authorization's own
    timestamp against the refusal timeline as a second, independent check.
    """
    uid = _user()
    ev = _stored(uid)
    # Record the refusal on the timeline WITHOUT the invalidating UPDATE, modelling the case where the
    # row was not reached — the timeline alone must be enough.
    async def _timeline_only():
        async with user_session(uid) as s:
            s.add(schema.AuthorizationRefusal(
                user_id=uid, refused_at=datetime.now(timezone.utc), message_id="m-9",
                polarity=uab.Polarity.cancellation.value))
    _run(_timeline_only())
    assert _recheck(uid, ev) == authorization_store.REFUSED_SINCE


def test_a_refusal_does_not_reach_backwards_to_authorizations_created_after_it():
    """Only a LATER trusted turn can create authorization — and a later turn genuinely can. A refusal
    that poisoned every future grant would mean one "never mind" ended the day."""
    uid = _user()
    _run(authorization_store.record_refusal(uid, uab.evaluate("nvm"), message_id="m-0"))
    ev = _stored(uid)
    assert _recheck(uid, ev) == ae.ALLOW


def test_the_account_was_disconnected_while_the_mission_slept():
    """Consent is necessary and never sufficient. A perfectly valid authorization cannot send mail from
    an account that is no longer connected."""
    uid = _user()
    ev = _stored(uid)

    async def _gone(_u, _p):
        return tool_broker._Conn(connected=False)

    with patch.object(tool_broker, "_provider_connection", _gone):
        assert _recheck(uid, ev) == authorization_store.CAPABILITY_NOT_LIVE


def test_access_was_revoked_while_the_mission_slept():
    uid = _user()
    ev = _stored(uid)
    _run(access_control.revoke_staging_test(uid, actor="test"))
    assert _recheck(uid, ev) == authorization_store.POLICY_DENIED


def test_consumed_by_one_attempt_refuses_a_second_and_permits_that_attempts_retry():
    """Exactly-once cuts both ways: a different operation reaching for the same consent is refused, and
    the SAME attempt retrying after a transport failure is not — otherwise a network blink permanently
    strands work the student agreed to."""
    uid = _user()
    ev = _stored(uid)
    _run(authorization_store.mark_consumed(uid, ev.authorization_id, attempt_key="attempt-1",
                                           receipt_id="msg-123"))
    assert _recheck(uid, ev, attempt_key="attempt-2") == ae.CONSUMED
    assert _recheck(uid, ev, attempt_key=None) == ae.CONSUMED
    assert _recheck(uid, ev, attempt_key="attempt-1") == ae.ALLOW
    assert _run(authorization_store.load(uid, ev.authorization_id)).operation_receipt_id == "msg-123"


def test_superseding_closes_the_old_one_and_points_at_the_replacement():
    uid = _user()
    first = _stored(uid)
    second = _stored(uid, args={**SEND_ARGS, "subject": "actually about sunday"})
    _run(authorization_store.supersede(uid, first.authorization_id, second))
    assert _recheck(uid, first) == ae.SUPERSEDED
    row = _run(authorization_store.load(uid, first.authorization_id))
    assert row.superseded_by_authorization_id == second.authorization_id


# --- the mission path, end to end -------------------------------------------------------------------------

def _send_action():
    return NextAction(type=ActionType.call_tool, capability="gmail.send_message", provider="gmail",
                      operation="send_message", arguments=dict(SEND_ARGS))


def test_a_woken_mission_sends_when_its_authorization_still_holds():
    """The positive control for the whole lane. Without this, every test above passes on a system that
    can no longer do background work at all."""
    uid = _user()
    ev = _stored(uid)
    fake = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    ex = mission_executor.MissionExecutor(adapter=fake, authorization_id=ev.authorization_id)
    res = _run(ex.execute(uid, _send_action(), idempotency_key="run-1:step1"))
    assert res.outcome is ToolOutcome.ok and res.verified is True
    assert fake.send_calls == 1
    # ...and it is spent, with the provider's own message id recorded as the receipt.
    stored = _run(authorization_store.load(uid, ev.authorization_id))
    assert stored.consumed_at is not None and stored.consumed_by_attempt == "run-1:step1"
    assert stored.operation_receipt_id == res.provider_entity_id


def test_a_woken_mission_carrying_a_refused_authorization_sends_nothing():
    uid = _user()
    ev = _stored(uid)
    _run(authorization_store.record_refusal(uid, uab.evaluate("wait no dont"), message_id="m-2"))
    fake = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    ex = mission_executor.MissionExecutor(adapter=fake, authorization_id=ev.authorization_id)
    res = _run(ex.execute(uid, _send_action(), idempotency_key="run-1:step1"))
    assert res.outcome is ToolOutcome.forbidden and not res.verified
    assert fake.send_calls == 0


def test_a_woken_mission_whose_arguments_drifted_sends_nothing():
    uid = _user()
    ev = _stored(uid)
    fake = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    ex = mission_executor.MissionExecutor(adapter=fake, authorization_id=ev.authorization_id)
    drifted = NextAction(type=ActionType.call_tool, capability="gmail.send_message", provider="gmail",
                         operation="send_message",
                         arguments={**SEND_ARGS, "to": "principal@school.edu"})
    res = _run(ex.execute(uid, drifted, idempotency_key="run-1:step1"))
    assert res.outcome is ToolOutcome.forbidden and fake.send_calls == 0


def test_a_mission_carrying_no_authorization_id_sends_nothing():
    uid = _user()
    fake = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
    res = _run(mission_executor.MissionExecutor(adapter=fake).execute(
        uid, _send_action(), idempotency_key="run-1:step1"))
    assert res.outcome is ToolOutcome.forbidden and fake.send_calls == 0


def test_a_mission_may_not_carry_a_verdict_only_a_pointer():
    """Structural, not a convention: there is no constructor parameter that accepts an evidence object or
    a boolean, so a mission cannot be handed a decision that was made in the past."""
    import inspect
    params = set(inspect.signature(mission_executor.MissionExecutor.__init__).parameters)
    assert "authorization_id" in params
    for forbidden in ("authorization", "approved", "authorized", "evidence", "consent"):
        assert forbidden not in params, f"MissionExecutor accepts {forbidden}"


# --- audit ------------------------------------------------------------------------------------------------

def test_the_audit_history_answers_who_said_bruce_could_do_this():
    uid = _user()
    ev = _stored(uid)
    _run(authorization_store.record_refusal(uid, uab.evaluate("actually no"), message_id="m-2"))
    rows = _run(authorization_store.history(uid))
    assert len(rows) == 1
    row = rows[0]
    assert row["operation"] == "gmail.send_message"
    assert row["trusted_message_id"] == "m-1" and row["invalidated_by_message_id"] == "m-2"
    assert row["mission_id"] is not None
    # Content-free: the arguments themselves never appear, only their digest.
    assert "coach@school.edu" not in str(row)


# --- the gateway is the only door ---------------------------------------------------------------------------

_ENGINE = Path(__file__).resolve().parents[1] / "bruce_engine"


def test_only_the_mutation_gateway_opens_an_authorization():
    """The invariant that replaces the five-enumerated-call-sites design.

    #120's boundary was five `require()` calls that each had to stay correct. That defends against the
    connectors that exist. This one defends against the next one: a Calendar, Gmail, files or Canvas path
    that writes straight from an orchestrator does not fail in review or in production — it fails here,
    on the commit that introduces it, because it had to open an authorization to do it.
    """
    openers = {p.name for p in _ENGINE.glob("*.py")
               if "execution_gate.open_authorization(" in p.read_text()}
    assert openers == {"mutation_gateway.py"}, f"a second module authorizes writes: {openers}"


def test_every_provider_mutation_still_passes_the_adapter_backstop():
    """The gateway decides; `require` verifies that what reached the socket is what was decided. Both
    layers, still, on every one of the five raw mutations — a gateway that was the ONLY check could not
    catch a recipient rewritten after the decision."""
    mutations = {("calendar_adapter.py", "await adapter.insert("),
                 ("calendar_adapter.py", "await adapter.delete("),
                 ("calendar_tools.py", "await a.update("),
                 ("calendar_tools.py", "await a.delete("),
                 ("gmail_adapter.py", "await adapter.send(")}
    for name, marker in mutations:
        src = (_ENGINE / name).read_text()
        assert "execution_gate.require(" in src.split(marker)[0], f"{name}: {marker} lost its backstop"


def test_the_gateway_refuses_rather_than_executing_when_the_store_is_unreachable():
    """A store outage must not silently downgrade to unauthorized execution. It must also not be reported
    as success — the student is told nothing happened, because nothing did."""
    uid = _user()
    ev = _grant(uid)
    calls = []

    async def _boom(*a, **kw):
        raise RuntimeError("store down")

    async def _perform():
        calls.append(1)
        return "did it"

    with patch.object(authorization_store, "record", _boom):
        res = _run(mutation_gateway.execute_with_evidence(
            uid, ev, provider="gmail", operation="send_message", arguments=SEND_ARGS,
            perform=_perform))
    assert res.denied and res.reason == "authorization_not_persisted" and calls == []


def test_the_gateway_never_reports_success_it_did_not_perform():
    uid = _user()
    res = _run(mutation_gateway.execute(
        uid, authorization_id=None, provider="gmail", operation="send_message", arguments=SEND_ARGS,
        perform=lambda: (_ for _ in ()).throw(AssertionError("perform must not be called"))))
    assert res.denied and res.performed is False and res.reason == ae.NO_AUTHORIZATION
