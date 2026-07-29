"""ONE SEND MUST NEVER READ AS TWO GOALS — the goal run and the execution attempt, joined and countable.

WHAT THIS IS ABOUT. The transcript this whole spine exists to fix produced twenty-two turns, zero missions
and ZERO agent_runs. `goal_handler` fixed the zero: a send now has a canonical goal run holding the typed
slots, the Decision and the consent. But `agent_loop.run_direct_action` opens its own row for the provider
call, so the same thank-you email now writes TWO rows — and nothing joined them. The first regression a
fix for "no rows" can produce is "one row too many", and it is not cosmetic: the ATTEMPT is created last,
`goal_runtime.open_goal` answers "the newest run that is not closed", and a blocked attempt is not closed.
The next turn's continuation would then land on the audit row for a send instead of on the goal that owns
it — and the audit row has no slots to answer into, so the amendment would be silently dropped, which is
the transcript's failure wearing a different hat.

THE CONTRACT PINNED BELOW, in the words of the brief:
  * exactly ONE canonical goal — the row with slots — per send;
  * an execution audit row may exist ONLY when explicitly linked to that goal AND closed;
  * the link is QUERYABLE (`goal->>'parent_run_id'`, asserted here as real SQL, not as a dict key);
  * no competing slot stores: only the canonical goal carries slots, even when the executor's own goal()
    summary is shaped like one;
  * exactly ONE ExecutionAttempt: two simultaneous confirmations, and a retry after a block, all resolve
    to the same single attempt row rather than forking it.

Every call below goes through the REAL execution gate on a genuinely connected account, minting consent
from trusted words. This file could have suspended the gate — it is about bookkeeping, not authorization —
but `test_authorization_zero_call` keeps the suspension list closed on purpose, and nothing here needed it.

HOW TO READ A FAILURE. Every absence assertion here is paired with a positive control asserted first —
the canonical goal IS found by the same query, the attempt row DOES exist — so a red test says "the query
works and the attempt escaped it", never "nothing was there to find".

Nothing here asserts on a log line and nothing logs message content.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_loop, agent_run_store, crypto, goal_runtime, goal_slots, oauth_google, schema
from bruce_engine.db import user_session, worker_session
from bruce_engine.goal_slots import GoalKind, SlotValue, Source
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.runtime_contracts import (ActionType, NextAction, Risk, ToolOutcome, ToolResult)

users = PostgresUserRepository()

CONV = "11111111-1111-4111-8111-111111111111"
ACCOUNT = "me@example.com"
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _seed(uid):
    """A student whose Google account is genuinely connected and scoped, enrolled for access.

    Everything except the bookkeeping under test is real, which is what makes a row count mean what it
    says: a missing entitlement would have denied the call at the gateway and left an attempt row that
    looks the same from the outside as one whose provider refused it.
    """
    from bruce_engine import access_control
    await users.ensure(uid, auth_provider="test")
    await access_control.enroll_staging_test(uid, actor="test", reason="execution run linkage")
    async with user_session(uid) as s:
        s.add(schema.Integration(
            user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
            scopes=[CAL_SCOPE], refresh_token_encrypted=crypto.encrypt("rt-secret"),
            selected_calendar_id="primary", status="connected"))


def _user():
    uid = uuid4(); _run(_seed(uid))
    return uid


class FakeExecutor:
    """A live-capability executor whose ToolResult is scripted. `goal()` deliberately summarizes itself
    with the same words the canonical goal holds in slots — that overlap is the point: it is what makes a
    second row look like a goal to anything that reads blobs loosely."""

    domain = "calendar"
    capability = "calendar.update_event"
    gate_provider = "google_calendar"
    gate_operation = "update_event"

    def __init__(self, tr: ToolResult, *, extra_goal: dict | None = None) -> None:
        self._tr = tr
        self._extra = extra_goal or {}
        self.executed = False

    def goal(self) -> dict:
        return {"action": "update", "desired_outcome": "move chess to 9pm", **self._extra}

    def build_action(self) -> NextAction:
        return NextAction(type=ActionType.call_tool, capability=self.capability,
                          provider="google_calendar", operation="update_event",
                          arguments={"new_start": "2026-07-29T21:00:00"}, risk=Risk.medium)

    def gate_arguments(self) -> dict:
        return {"new_start": "2026-07-29T21:00:00"}

    async def execute(self, user_id: UUID) -> ToolResult:
        self.executed = True
        return self._tr


def _tr(outcome, *, verified=False, reason=""):
    return ToolResult(outcome, "calendar.update_event", "google_calendar", "update_event",
                      verified=verified, provider_entity_id="evt123", reason=reason)


def _ok():
    return _tr(ToolOutcome.ok, verified=True, reason="read-back matched")


def _unauthorized():
    return _tr(ToolOutcome.unauthorized, reason="google_calendar_not_connected")


def _go(uid, ex: FakeExecutor, **kw):
    """`run_direct_action` with REAL consent, freshly minted from trusted words, for every call.

    This file could have suspended the execution gate — it is about bookkeeping, not authorization — but
    the suspension list is a closed set somebody has to widen in review, and there is nothing here that
    needs widening it: the student's words genuinely ask for the move, so the honest grant is one call.
    A retry mints its OWN evidence, because an authorization is spent by the attempt that used it.
    """
    from bruce_engine import authorization_evidence as ae
    authz = ae.try_grant(user_id=uid, provider=ex.gate_provider, operation=ex.gate_operation,
                         arguments=ex.gate_arguments(),
                         authorization_type=ae.AuthorizationType.direct_explicit,
                         text="move chess club to 9pm", trusted_message_id=f"m-{uuid4()}",
                         request_span="move")
    assert authz is not None, "the corpus grant this file depends on stopped being granted"
    return _run(agent_loop.run_direct_action(uid, executor=ex, authorization=authz, **kw))


def _canonical_goal(uid) -> str:
    """A real canonical goal run: the typed slot block `goal_slots` writes, on a run created by the store.

    Built through `goal_slots.to_goal_jsonb` rather than by hand so that if the slot block's shape ever
    changes, this file follows it instead of asserting against a shape nothing writes any more.
    """
    slots = {"recipient": SlotValue("coach@school.edu", Source.user_stated),
             "purpose": SlotValue("thank you for the recommendation", Source.user_stated)}
    goal = goal_slots.to_goal_jsonb({goal_runtime.CONVERSATION_KEY: CONV}, GoalKind.send_email, slots)
    run = _run(agent_run_store.create_run(uid, domain="gmail", goal=goal,
                                          idempotency_key=f"goal:{uuid4()}"))
    return str(run["id"])


async def _rows(uid):
    async with worker_session() as s:
        res = (await s.execute(sa_text(
            "SELECT id::text, status, goal FROM agent_runs WHERE user_id = :u ORDER BY created_at, id"),
            {"u": str(uid)})).all()
    return [{"id": r[0], "status": r[1], "goal": r[2]} for r in res]


async def _completed_at(uid, run_id: str):
    """Read straight from the column: `agent_run_store._to_dict` does not carry `completed_at`, and
    "is this attempt finished" is exactly the question this file is about."""
    async with worker_session() as s:
        return (await s.execute(sa_text(
            "SELECT completed_at FROM agent_runs WHERE user_id = :u AND id = :r"),
            {"u": str(uid), "r": run_id})).scalar()


async def _children_of(uid, parent_id: str):
    """The link as the DATABASE sees it. A dict key that only ever round-trips through Python is not a
    queryable link, so the claim is made in SQL."""
    async with worker_session() as s:
        return [r[0] for r in (await s.execute(sa_text(
            "SELECT id::text FROM agent_runs WHERE user_id = :u AND goal->>'parent_run_id' = :p"),
            {"u": str(uid), "p": parent_id})).all()]


# --- 1. one goal, one attempt, joined ---------------------------------------------------------------------

def test_a_verified_send_leaves_one_goal_and_one_attempt_linked_to_it(clean_db):
    uid = _user()
    goal_id = _canonical_goal(uid)

    res = _go(uid, FakeExecutor(_ok()), conversation_id=CONV, parent_run_id=goal_id,
              idempotency_key=f"goal:{goal_id}:calendar.update_event")
    assert res.status == "completed" and res.verified is True

    rows = _run(_rows(uid))
    assert len(rows) == 2                                    # the goal and its attempt — both real
    assert res.run_id != goal_id                             # the attempt is NOT the goal row

    # exactly one canonical goal: exactly one row that is not an execution attempt.
    goals = [r for r in rows if not agent_loop.is_execution_run(r)]
    attempts = [r for r in rows if agent_loop.is_execution_run(r)]
    assert [r["id"] for r in goals] == [goal_id]
    assert [r["id"] for r in attempts] == [res.run_id]
    assert agent_loop.parent_run_id_of(attempts[0]) == goal_id


def test_the_link_is_queryable_as_sql_not_just_as_a_dict_key(clean_db):
    uid = _user()
    goal_id = _canonical_goal(uid)
    res = _go(uid, FakeExecutor(_ok()), idempotency_key="k-linked", parent_run_id=goal_id)

    assert _run(_children_of(uid, goal_id)) == [res.run_id]
    # positive control for the query itself: a goal id nothing was executed for has no children, and the
    # query that returns [] here is the same one that returned the child above.
    assert _run(_children_of(uid, str(uuid4()))) == []


def test_an_unlinked_attempt_is_still_marked_as_an_attempt(clean_db):
    """`calendar_mutation` and `mission_planner` call the loop with no goal row of their own. They get no
    parent — there is none to name — but the row must still not read as a goal, because `open_goal` does
    not ask who the parent is, only whether the run is open."""
    uid = _user()
    res = _go(uid, FakeExecutor(_ok()), idempotency_key="k-solo")

    rows = _run(_rows(uid))
    assert [r["id"] for r in rows] == [res.run_id]
    assert agent_loop.is_execution_run(rows[0]) is True
    assert agent_loop.parent_run_id_of(rows[0]) is None


# --- 2. the attempt never becomes the open goal ------------------------------------------------------------

def test_a_blocked_attempt_never_becomes_the_open_goal(clean_db):
    """THE REGRESSION THIS FILE EXISTS FOR. `open_goal` returns the newest run that is not closed. The
    attempt is created after the goal, so if a blocked attempt stayed open it would win — and the next
    turn's continuation would resolve against a row with no slots."""
    uid = _user()
    goal_id = _canonical_goal(uid)

    # positive control, asserted FIRST: with only the goal in the table, the query finds the goal.
    assert str(_run(goal_runtime.open_goal(uid, conversation_id=CONV))["id"]) == goal_id

    res = _go(uid, FakeExecutor(_unauthorized()), idempotency_key="k-blocked",
              conversation_id=CONV, parent_run_id=goal_id)
    assert res.status == "blocked"                            # the CALLER still hears "blocked"
    assert len(_run(_rows(uid))) == 2                         # and the attempt row really was written

    still = _run(goal_runtime.open_goal(uid, conversation_id=CONV))
    assert still is not None and str(still["id"]) == goal_id  # the GOAL, not the attempt
    open_ids = [str(r["id"]) for r in _run(goal_runtime.open_runs(uid, conversation_id=CONV))]
    assert res.run_id not in open_ids and goal_id in open_ids


def test_a_closed_attempt_still_records_why_the_provider_refused(clean_db):
    """Closing the child must not cost the audit its content: `blocked_reason` and the provider's own
    `unauthorized` outcome are both on the row, so "they refused us" is still distinguishable from "the
    call went wrong" without the status column having to carry it."""
    uid = _user()
    goal_id = _canonical_goal(uid)
    res = _go(uid, FakeExecutor(_unauthorized()), idempotency_key="k-blocked-audit",
              parent_run_id=goal_id)

    child = _run(agent_run_store.get_run(uid, UUID(res.run_id)))
    assert child["status"] in goal_runtime._CLOSED_STATUSES   # closed to goal selection
    assert _run(_completed_at(uid, res.run_id)) is not None
    assert child["blocked_reason"] == "google_calendar_not_connected"
    assert child["last_tool_result"]["outcome"] == ToolOutcome.unauthorized.value
    assert child["verification_result"]["verified"] is False


def test_a_standalone_blocked_attempt_stays_resumable(clean_db):
    """The rename applies to a CHILD only. An attempt with no goal behind it has no other row to carry the
    reconnect state, so it keeps `blocked` and stays active — the behaviour `calendar_mutation` relies on."""
    uid = _user()
    linked_goal = _canonical_goal(uid)

    solo = _go(uid, FakeExecutor(_unauthorized()), idempotency_key="k-solo-blocked")
    child = _go(uid, FakeExecutor(_unauthorized()), idempotency_key="k-child-blocked",
                parent_run_id=linked_goal)

    assert _run(agent_run_store.get_run(uid, UUID(solo.run_id)))["status"] == "blocked"
    assert _run(_completed_at(uid, solo.run_id)) is None       # still resumable, not finished
    assert _run(agent_run_store.get_run(uid, UUID(child.run_id)))["status"] != "blocked"
    assert solo.status == child.status == "blocked"            # both callers hear the same thing


# --- 3. no competing slot stores ---------------------------------------------------------------------------

def test_an_attempt_never_carries_slots_even_when_its_executor_writes_them(clean_db):
    """An executor that summarizes itself in the slot block's shape would otherwise create a SECOND place
    a recipient lives — and a correction lands in one of them while the send reads the other."""
    uid = _user()
    goal_id = _canonical_goal(uid)
    smuggled = goal_slots.to_goal_jsonb({}, GoalKind.send_email,
                                        {"recipient": SlotValue("wrong@example.com", Source.user_stated)})

    res = _go(uid, FakeExecutor(_ok(), extra_goal=smuggled), idempotency_key="k-slots",
              parent_run_id=goal_id)
    assert res.verified is True          # the attempt really ran; the blob below is a REAL attempt's

    rows = {r["id"]: r for r in _run(_rows(uid))}
    # positive control: the CANONICAL goal does parse to a typed kind with the recipient still on it.
    kind, slots = goal_slots.from_goal_jsonb(rows[goal_id]["goal"])
    assert kind is GoalKind.send_email and slots["recipient"].value == "coach@school.edu"
    # and the attempt parses to nothing at all — no kind, no slots, no second recipient.
    child_kind, child_slots = goal_slots.from_goal_jsonb(rows[res.run_id]["goal"])
    assert child_kind is None and child_slots == {}
    assert goal_slots.SLOT_KEY not in rows[res.run_id]["goal"]
    # the executor's own summary survives — stripping slots must not blank the audit.
    assert rows[res.run_id]["goal"]["desired_outcome"] == "move chess to 9pm"


def test_the_attempt_is_attributed_to_the_thread_it_ran_in(clean_db):
    """An unattributed run is visible to EVERY conversation (`goal_runtime._conversation_of` reads "" as
    "belongs to all"), so an attempt with no conversation was an open row in threads it had nothing to do
    with. Naming the thread narrows it."""
    uid = _user()
    goal_id = _canonical_goal(uid)
    res = _go(uid, FakeExecutor(_ok()), idempotency_key="k-conv", conversation_id=CONV,
              parent_run_id=goal_id)
    rows = {r["id"]: r for r in _run(_rows(uid))}
    assert rows[res.run_id]["goal"][goal_runtime.CONVERSATION_KEY] == CONV


# --- 4. exactly one ExecutionAttempt -----------------------------------------------------------------------

def test_two_confirmations_at_once_produce_one_attempt_not_two(clean_db):
    uid = _user()
    goal_id = _canonical_goal(uid)
    key = f"goal:{goal_id}:calendar.update_event"

    a = _go(uid, FakeExecutor(_ok()), idempotency_key=key, parent_run_id=goal_id)
    b = _go(uid, FakeExecutor(_ok()), idempotency_key=key, parent_run_id=goal_id)

    assert a.run_id == b.run_id
    assert _run(_children_of(uid, goal_id)) == [a.run_id]      # ONE attempt, not two
    assert len(_run(_rows(uid))) == 2


def test_a_retry_after_a_block_does_not_fork_the_attempt(clean_db):
    """Closing the child must not fork the audit — and it does not: the reconnect retry resolves to the
    SAME row, so the goal still has exactly one attempt.

    It also does not ADVANCE that row, and this test says so out loud rather than pretending otherwise.
    `update_run` aborts the entire write when the status change is refused, and a retried attempt has no
    legal edge from its closed state to its new outcome — so the row keeps attempt 1's result. That is
    PRE-EXISTING and measured on the unlinked path below as the control: before linking, the same retry
    left the row stuck at `blocked`, which is worse, because `blocked` is not closed and the stuck row
    went on posing as the student's open goal. Linking does not fix the gap; it stops the gap from
    costing a goal. The caller is told the truth either way, and the CANONICAL goal — the row a human
    reads for "did it send" — is settled from that by `goal_handler._settle`.
    """
    uid = _user()
    goal_id = _canonical_goal(uid)
    key = f"goal:{goal_id}:calendar.update_event"

    first = _go(uid, FakeExecutor(_unauthorized()), idempotency_key=key, parent_run_id=goal_id)
    assert first.status == "blocked"

    second = _go(uid, FakeExecutor(_ok()), idempotency_key=key, parent_run_id=goal_id)
    assert second.run_id == first.run_id and second.verified is True   # the caller gets the real result

    assert _run(_children_of(uid, goal_id)) == [first.run_id]          # ONE attempt, still
    assert len(_run(_rows(uid))) == 2
    child = _run(agent_run_store.get_run(uid, UUID(second.run_id)))
    assert child["status"] in goal_runtime._CLOSED_STATUSES            # closed, whatever it records

    # THE CONTROL for "this gap is not caused by linking": the same retry on an unlinked attempt is stuck
    # in exactly the same way, and stuck in the OPEN state that linking exists to prevent.
    solo_a = _go(uid, FakeExecutor(_unauthorized()), idempotency_key="k-solo-retry")
    solo_b = _go(uid, FakeExecutor(_ok()), idempotency_key="k-solo-retry")
    assert solo_b.run_id == solo_a.run_id and solo_b.verified is True
    solo_row = _run(agent_run_store.get_run(uid, UUID(solo_b.run_id)))
    assert solo_row["status"] == "blocked"                             # unchanged by its own retry
    assert solo_row["status"] not in goal_runtime._CLOSED_STATUSES     # and therefore still "open"


# --- 5. the predicates other modules will select on --------------------------------------------------------

def test_the_predicates_answer_no_for_a_goal_row(clean_db):
    """`is_execution_run` is what goal selection can exclude attempts BY, instead of relying on an
    executor happening never to write a slot block. It must be false for a real goal."""
    uid = _user()
    goal_id = _canonical_goal(uid)
    rows = {r["id"]: r for r in _run(_rows(uid))}

    assert agent_loop.is_execution_run(rows[goal_id]) is False
    assert agent_loop.parent_run_id_of(rows[goal_id]) is None
    # and a malformed row is not an attempt either — an unreadable blob must not silently hide a goal.
    assert agent_loop.is_execution_run({"goal": None}) is False
    assert agent_loop.is_execution_run({"goal": {"execution_run": "yes"}}) is False
    assert agent_loop.is_execution_run(None) is False


def test_the_audit_run_is_still_best_effort(clean_db):
    """The linkage must not have made bookkeeping load-bearing. With the store down, the provider call
    still happens and its verified result still flows back — the rule `run_direct_action` was written to."""
    from unittest.mock import patch

    uid = _user()
    ex = FakeExecutor(_ok())

    async def _boom(*a, **k):
        raise RuntimeError("db pool exhausted")

    with patch.object(agent_run_store, "create_run", _boom), \
         patch.object(agent_run_store, "update_run", _boom):
        res = _go(uid, ex, parent_run_id=str(uuid4()))

    assert ex.executed is True and res.status == "completed" and res.verified is True
    assert res.run_id == ""
