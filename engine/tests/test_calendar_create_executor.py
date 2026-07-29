"""CalendarCreateExecutor — the executor `goal_handler` had no row for, driven through the real loop.

WHAT WAS MISSING. `goal_slots` declares a full slot set for `calendar.create_event` and
`goal_handler._EXECUTORS` had no entry for it, so `goal_handler._verdict` returned `NO_EXECUTOR` and
DECLINED every calendar turn. The acceptance suite calls that D3 and states the consequence plainly: a
`schedule_event` goal is never created, so "one goal id across a move", "a replacement Decision" and
"attendees retained" have nothing live to hold against. Collecting a title, a start and a timezone and
only then admitting nothing can perform the call is the exact exchange this workstream exists to delete.

WHAT IS ASSERTED HERE, and how. Every provider-facing test below runs against
`calendar_adapter.FakeCalendarAdapter`, which models Google's own semantics — the caller-supplied event
id, the 409 on a duplicate insert, the organizer/creator stamp that identifies the account — so the
execute-once and read-back logic is genuinely exercised. Nothing about Bruce is mocked: the loop, the
MutationGateway, the execution gate, the adapter's read-back comparison and the durable AgentRun are all
the production code.

THIS FILE EXERCISES THE EXECUTION BOUNDARY rather than declaring itself exempt from it. It does not use
the test-only gate suspension that the provider-semantics files declare in `test_authorization_zero_call`;
every provider call below happens under a real authorization, minted either from a student's own approval
(`authorization_evidence.try_grant`) or by `execution_gate.granted_for_test`, whose evidence is genuine
and passes every rule. Two tests assert that NOTHING reaches the provider when that authorization is
absent or names a different event, and each is paired with a positive control asserted first so it cannot
pass by the executor simply never working.

No assertion is made against a log line, and nothing here logs message content.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (agent_loop, authorization_evidence as ae, calendar_adapter, calendar_executor,
                          crypto, execution_gate, oauth_google, schema)
from bruce_engine.calendar_executor import CalendarCreateExecutor
from bruce_engine.db import user_session
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.runtime_contracts import ToolOutcome

users = PostgresUserRepository()

ACCOUNT = "student@example.com"
CAL = "https://www.googleapis.com/auth/calendar.events"
TZ = "America/Chicago"
TITLE = "chess club"
START = "2026-08-04T16:00:00"
END = "2026-08-04T17:00:00"
KEY = "goal:11111111-1111-1111-1111-111111111111:calendar.create_event"
CONVERSATION = "+15550142"


def _run(c):
    return asyncio.run(c)


# ==========================================================================================================
# PURE — everything that can be decided without touching a provider, decided without touching one.
# ==========================================================================================================

def test_a_field_no_authorization_could_name_is_not_deliverable():
    """`undeliverable` is derived from two modules, not written down, so it cannot rot.

    POSITIVE CONTROL FIRST: `location` is in `execution_gate.calendar_create_args` AND on
    `models.CalendarEvent`, so it is deliverable and is not refused. `attendees` is in neither, so no
    authorization could ever name it and `_to_google_body` would drop it on the way out — a student who
    asked for two people to be invited would get an event with nobody on it and a green checkmark.
    """
    assert "location" in calendar_executor.DELIVERABLE          # positive control
    assert calendar_executor.undeliverable({"location": "the gym"}) == ()
    assert calendar_executor.undeliverable({"title": TITLE, "start": START}) == ()

    assert "attendees" not in calendar_executor.DELIVERABLE
    assert calendar_executor.undeliverable({"attendees": ["mr.kim@school.edu"]}) == ("attendees",)
    assert calendar_executor.undeliverable({"description": "bring a board"}) == ("description",)
    # Asking for nothing is not asking for something undeliverable.
    assert calendar_executor.undeliverable({"attendees": [], "description": None}) == ()


def test_a_timed_event_with_no_stated_end_gets_one_the_gate_can_bind():
    """`end` is optional in the tool schema, so `goal_runtime` will never ask for it. Refusing at execute
    time would strand a goal AFTER the student said yes; sending `end=None` gives Google `end == start`,
    which is a zero-length event nobody asked for. It gets an hour, and — this is the part that matters —
    the DERIVED end is what `gate_arguments` binds consent to, so what was approved and what is created
    are the same span."""
    ex = CalendarCreateExecutor(title=TITLE, start=START, end=None, timezone=TZ, idempotency_key=KEY)
    assert ex.gate_arguments()["end"] == END
    assert ex.build_action().arguments["end"] == END

    stated = CalendarCreateExecutor(title=TITLE, start=START, end="2026-08-04T18:30:00", timezone=TZ,
                                    idempotency_key=KEY)
    assert stated.gate_arguments()["end"] == "2026-08-04T18:30:00"   # a stated end is never overwritten

    # An end that does not lie past its start is not an end.
    backwards = CalendarCreateExecutor(title=TITLE, start=START, end="2026-08-04T15:00:00", timezone=TZ,
                                       idempotency_key=KEY)
    assert backwards.gate_arguments()["end"] == END


def test_an_all_day_create_uses_dates_and_googles_exclusive_end():
    """Google 400s when `date` and `dateTime` are mixed, and its all-day end is EXCLUSIVE. A one-day
    event therefore ends on the following day and carries no zone."""
    ex = CalendarCreateExecutor(title=TITLE, start="2026-08-04", timezone=TZ, idempotency_key=KEY)
    args = ex.gate_arguments()
    assert (args["start"], args["end"], args["timezone"]) == ("2026-08-04", "2026-08-05", None)

    # An explicit all_day flag beside a timed start resolves to the date rather than being sent as-is.
    flagged = CalendarCreateExecutor(title=TITLE, start=START, all_day=True, timezone=TZ,
                                     idempotency_key=KEY)
    assert flagged.gate_arguments()["start"] == "2026-08-04"

    # A multi-day span keeps the exclusive end the resolver already produced.
    span = CalendarCreateExecutor(title=TITLE, start="2026-08-04", end="2026-08-06", idempotency_key=KEY)
    assert span.gate_arguments()["end"] == "2026-08-06"


def test_a_start_that_is_not_a_moment_never_becomes_an_event():
    """`goal_handler.resolve_temporal` leaves a when-phrase it could not read exactly as the student typed
    it, on purpose. Building an event out of that string would either 400 at Google or land an event on a
    date nobody chose, so construction fails and the turn asks a question instead."""
    with pytest.raises(ValueError):
        CalendarCreateExecutor(title=TITLE, start="next tuesdayish", timezone=TZ, idempotency_key=KEY)
    with pytest.raises(ValueError):
        CalendarCreateExecutor(title="   ", start=START, timezone=TZ, idempotency_key=KEY)
    # positive control — the same call with a resolvable start builds.
    assert CalendarCreateExecutor(title=TITLE, start=START, timezone=TZ, idempotency_key=KEY)


def test_a_create_with_no_idempotency_key_is_refused_loudly():
    """No key means no stable provider event id, which means a retry creates a SECOND event on a real
    student's calendar. That must fail at construction, not quietly at the provider."""
    with pytest.raises(ValueError):
        CalendarCreateExecutor(title=TITLE, start=START, timezone=TZ, idempotency_key="")


def test_attendee_verification_needs_the_provider_to_show_them():
    """The one comparison this executor adds on top of `calendar_adapter._matches`, which cannot make it
    because `CalendarEvent` has no attendees field.

    Case and order do not matter (inviting two people in the other order is the same act — the rule
    `authorization_evidence` already applies to a recipient list). A missing attendee, an extra one, and
    a read-back that shows no attendees at all are each NOT verified: a provider that returned no
    attendee list has not proven the invitations exist.
    """
    want = ["Mr.Kim@school.edu", "sam@school.edu"]
    ok, _ = calendar_executor.verify_attendees(
        want, {"attendees": [{"email": "sam@SCHOOL.edu"}, {"email": "mr.kim@school.edu"}]})
    assert ok is True                                            # positive control

    assert calendar_executor.verify_attendees(want, {"attendees": ["sam@school.edu"]})[0] is False
    assert calendar_executor.verify_attendees(want, {})[0] is False
    assert calendar_executor.verify_attendees(want, None)[0] is False
    assert calendar_executor.verify_attendees(
        want, {"attendees": [*want, "someone@else.edu"]})[0] is False
    # Nothing requested is nothing to prove.
    assert calendar_executor.verify_attendees([], {})[0] is True


# ==========================================================================================================
# HARNESS — a student with a connected Google Calendar and an in-memory provider.
# ==========================================================================================================

class CountingCalendar(calendar_adapter.FakeCalendarAdapter):
    """The in-memory calendar, plus a count of READ-BACKS. "the create was proven" is an assertion about
    Bruce having gone and looked, and the base fake only counts inserts."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.get_calls = 0

    async def get(self, event_id: str):
        self.get_calls += 1
        return await super().get(event_id)


class Unreadable(CountingCalendar):
    """The insert succeeds and the read-back finds nothing — the write happened and cannot be proven."""

    async def get(self, event_id: str):
        self.get_calls += 1
        return None


class Disagrees(CountingCalendar):
    """The provider holds something other than what was approved."""

    async def get(self, event_id: str):
        raw = await super().get(event_id)
        return None if raw is None else {**raw, "title": "someone else's event"}


@pytest.fixture()
def pg(pg_test_db, clean_db, monkeypatch):
    monkeypatch.setattr(
        db, "create_async_engine",
        lambda url, **kw: (kw.pop("poolclass", None),
                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


async def _seed(uid, *, account: str | None = ACCOUNT, connected: bool = True) -> None:
    await users.ensure(uid, auth_provider="test")
    if not connected:
        return
    async with user_session(uid) as s:
        s.add(schema.Integration(
            user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=account, scopes=[CAL],
            refresh_token_encrypted=crypto.encrypt("rt"), selected_calendar_id="primary",
            status="connected"))


@pytest.fixture()
def student(pg):
    uid = uuid4()
    _run(_seed(uid))
    return uid


def _executor(adapter, *, key: str = KEY, **overrides) -> CalendarCreateExecutor:
    fields = {"title": TITLE, "start": START, "end": END, "timezone": TZ}
    fields.update(overrides)
    return CalendarCreateExecutor(idempotency_key=key, adapter=adapter, **fields)


def _approval(uid, ex, *, message_id: str = "m1"):
    """Consent minted exactly the way `goal_handler._run` mints it: the student's own affirmative, bound
    to the arguments the executor will actually send, against an open Decision."""
    return ae.try_grant(
        user_id=uid, provider=ex.gate_provider, operation=ex.gate_operation,
        arguments=ex.gate_arguments(), authorization_type=ae.AuthorizationType.decision_approval,
        text="yes, add it", trusted_message_id=message_id, decision_id=str(uuid4()),
        conversation_id=CONVERSATION, has_pending_decision=True, explicit_operation_request=False)


def _perform(uid, ex, *, message_id: str = "m1", key: str = KEY):
    return agent_loop.run_direct_action(
        uid, executor=ex, idempotency_key=key, authorization=_approval(uid, ex, message_id=message_id),
        conversation_id=CONVERSATION)


def _runs(uid) -> list[dict]:
    async def _read():
        async with user_session(uid) as s:
            rows = (await s.execute(select(schema.AgentRun).where(
                schema.AgentRun.user_id == uid).order_by(schema.AgentRun.created_at))).scalars().all()
        return [{"status": r.status, "domain": r.domain} for r in rows]
    return _run(_read())


def _entities(uid) -> list[dict]:
    async def _read():
        async with user_session(uid) as s:
            rows = (await s.execute(select(schema.CalendarEventEntity).where(
                schema.CalendarEventEntity.user_id == uid))).scalars().all()
        return [{"provider_event_id": r.provider_event_id, "title": r.title, "start": r.start,
                 "end": r.end, "calendar_id": r.calendar_id} for r in rows]
    return _run(_read())


# ==========================================================================================================
# THE CREATE, END TO END THROUGH THE ONE EXECUTION PATH.
# ==========================================================================================================

def test_a_confirmed_create_reaches_the_verified_terminal_only_through_a_read_back(student):
    """The whole shape, in order: a student's approval becomes AuthorizationEvidence, the MutationGateway
    reloads and rejudges it, the adapter creates the event once, an INDEPENDENT read-back is fetched and
    compared, and only then does the durable run reach its verified terminal.

    `completed` and not `succeeded`, deliberately: `agent_loop._status_for` writes `completed` as the
    direct-action lane's verified terminal and `agent_run_store._OPERATIONAL` declares `executing ->
    completed` for that lane. Reaching `succeeded` would mean walking the machine states outside
    `agent_loop`, which is a second execution path.
    """
    adapter = CountingCalendar(account=ACCOUNT)
    result = _run(_perform(student, _executor(adapter)))

    assert result.verified is True
    assert result.tool_result.outcome is ToolOutcome.ok
    assert result.status == "completed"
    assert adapter.insert_calls == 1 and len(adapter.events) == 1
    assert adapter.get_calls >= 1, "the create was never read back"

    created = next(iter(adapter.events.values()))
    assert created["summary"] == TITLE
    assert created["start"]["dateTime"] == START and created["end"]["dateTime"] == END
    assert created["start"]["timeZone"] == TZ
    assert result.tool_result.provider_entity_id == next(iter(adapter.events))
    assert (result.tool_result.read_back or {}).get("title") == TITLE

    statuses = [r["status"] for r in _runs(student) if r["domain"] == "calendar"]
    assert "completed" in statuses, f"no durable calendar run closed on the read-back: {statuses}"


def test_nothing_reaches_the_provider_without_an_authorization(student):
    """ZERO provider calls before confirmation, proven at the boundary rather than by inspection.

    The positive control runs first: the identical executor WITH the student's approval creates the
    event. Then the same call with no authorization at all is refused by the gateway, and the adapter
    records not one insert and not one read-back.
    """
    control = CountingCalendar(account=ACCOUNT)
    assert _run(_perform(student, _executor(control))).verified is True     # positive control
    assert control.insert_calls == 1

    adapter = CountingCalendar(account=ACCOUNT)
    unconfirmed = _run(agent_loop.run_direct_action(
        student, executor=_executor(adapter, key=KEY + ":unconfirmed"),
        idempotency_key=KEY + ":unconfirmed", conversation_id=CONVERSATION))

    assert unconfirmed.verified is False
    assert unconfirmed.status == "failed"
    assert unconfirmed.tool_result.outcome is ToolOutcome.forbidden
    assert adapter.insert_calls == 0 and adapter.events == {}
    assert adapter.get_calls == 0, "an unauthorized create still went and looked at the calendar"


def test_the_gate_binds_the_exact_event_that_reaches_the_adapter(student):
    """`execute_and_verify` calls `execution_gate.require` with arguments it rebuilds from the event
    itself, so an authorization opened for anything else stops the write at the adapter — the one place a
    substitution made AFTER the gateway decided can still be caught.

    The positive control is asserted first: an authorization opened for `gate_arguments()` lets the same
    create through, which is what makes the refusal below a statement about the binding rather than about
    the gate being permanently shut.
    """
    ok_adapter = CountingCalendar(account=ACCOUNT)
    ex = _executor(ok_adapter)
    with execution_gate.granted_for_test(student, provider=ex.gate_provider,
                                         operation=ex.gate_operation, arguments=ex.gate_arguments()):
        assert _run(ex.execute(student)).verified is True       # positive control
    assert ok_adapter.insert_calls == 1

    other_adapter = CountingCalendar(account=ACCOUNT)
    other = _executor(other_adapter, key=KEY + ":other")
    elsewhere = {**other.gate_arguments(), "title": "a completely different event"}
    with execution_gate.granted_for_test(student, provider=other.gate_provider,
                                         operation=other.gate_operation, arguments=elsewhere):
        with pytest.raises(execution_gate.UnauthorizedExecution):
            _run(other.execute(student))
    assert other_adapter.insert_calls == 0, "a create ran under consent for a different event"


def test_repeated_confirmation_creates_exactly_one_event(student):
    """Exactly-once is enforced REMOTELY. The provider event id is derived from the durable idempotency
    key plus the event's identity, so a second confirmation re-derives the same id, the provider answers
    409, and `execute_and_verify` falls through to the read-back instead of creating a duplicate.

    `insert_calls == 2` is asserted on purpose: without it this test would also pass if Bruce had simply
    never made the second call, which proves nothing about the guarantee.
    """
    adapter = CountingCalendar(account=ACCOUNT)
    first = _run(_perform(student, _executor(adapter), message_id="m1"))
    second = _run(_perform(student, _executor(adapter), message_id="m2"))

    assert first.verified and second.verified
    assert adapter.insert_calls == 2, "the second confirmation never reached the provider at all"
    assert len(adapter.events) == 1, "a repeated confirmation created a second event"


def test_two_confirmations_arriving_at_the_same_instant_create_exactly_one_event(student):
    """The same guarantee under a RACE — a double tap, a redelivered webhook. Both turns mint their own
    evidence, both pass the gateway and both reach the executor; the deterministic id collapses them at
    the provider, which is the arbiter, rather than in a check-then-act in this process."""
    adapter = CountingCalendar(account=ACCOUNT)

    async def _both():
        return await asyncio.gather(
            _perform(student, _executor(adapter), message_id="race-a"),
            _perform(student, _executor(adapter), message_id="race-b"))

    results = _run(_both())
    assert adapter.insert_calls == 2
    assert len(adapter.events) == 1, "a concurrent confirmation created a second event"
    assert any(r.verified for r in results)


def test_a_create_that_cannot_be_proven_never_claims_to_have_happened(student):
    """A write that returns 2xx is a claim; a read that agrees is evidence. Both failure shapes land in
    `failed` truthfully and neither reports a verified terminal.

    The positive control is the passing create in
    `test_a_confirmed_create_reaches_the_verified_terminal_only_through_a_read_back`; here the inserts
    are asserted to have HAPPENED, so these are genuinely unproven writes rather than calls that were
    never made.
    """
    absent = Unreadable(account=ACCOUNT)
    unproven = _run(_perform(student, _executor(absent), message_id="m1"))
    assert absent.insert_calls == 1, "nothing was written, so there is nothing to fail to prove"
    assert unproven.verified is False
    assert unproven.status == "failed"
    assert unproven.tool_result.outcome is ToolOutcome.verification_inconclusive

    wrong = Disagrees(account=ACCOUNT)
    mismatched = _run(_perform(student, _executor(wrong, key=KEY + ":b"), message_id="m2",
                               key=KEY + ":b"))
    assert wrong.insert_calls == 1
    assert mismatched.verified is False
    assert mismatched.status == "failed"
    assert mismatched.tool_result.outcome is ToolOutcome.verification_failed

    statuses = [r["status"] for r in _runs(student) if r["domain"] == "calendar"]
    assert "completed" not in statuses, f"an unproven create reached a verified terminal: {statuses}"


def test_a_create_that_cannot_carry_its_attendees_never_touches_the_provider(student):
    """The refusal that keeps a green checkmark honest.

    POSITIVE CONTROL FIRST: the identical create without attendees goes through. Add attendees — which
    `execution_gate.calendar_create_args` cannot name and `models.CalendarEvent` cannot express — and the
    executor refuses BEFORE any provider call, so there is no half-right event left on the student's
    calendar for anyone to clean up. The reason names the field rather than blaming Google.
    """
    control = CountingCalendar(account=ACCOUNT)
    assert _run(_perform(student, _executor(control))).verified is True     # positive control

    adapter = CountingCalendar(account=ACCOUNT)
    ex = _executor(adapter, key=KEY + ":guests", attendees=["mr.kim@school.edu", "sam@school.edu"])
    refused = _run(_perform(student, ex, message_id="m2", key=KEY + ":guests"))

    assert adapter.insert_calls == 0 and adapter.events == {}
    assert refused.verified is False and refused.status == "failed"
    assert "attendees" in refused.tool_result.reason
    assert ex.attendees == ("mr.kim@school.edu", "sam@school.edu"), \
        "the attendees the student named were dropped rather than refused"


def test_a_calendar_nobody_connected_is_refused_before_any_provider_call(pg):
    """A student who never connected Google gets a refusal, not an attempt, and the refusal happens twice
    over — which is the point of having two independent layers.

    Through the loop the MutationGateway gets there first: `authorization_store.recheck` answers
    `capability_not_live` for a user with no connection, so the run lands in `failed` with a typed reason
    and the adapter is never called. That is truthful (nothing was created) and it is where the loop puts
    a denial; `blocked` is not reachable from there because `agent_loop` never ran the executor.

    So the executor's OWN answer is asserted separately, under a real grant, because that is the part
    this file owns: a missing connection is `unauthorized`, which `agent_loop._status_for` records as
    `blocked` — a repairable state the student can fix — rather than as a failed plan or, worse, a
    success. The connected student in the tests above is the positive control for both halves.
    """
    stranger = uuid4()
    _run(_seed(stranger, connected=False))

    through_the_loop = CountingCalendar(account=ACCOUNT)
    result = _run(_perform(stranger, _executor(through_the_loop)))
    assert through_the_loop.insert_calls == 0, "a create was attempted against a calendar nobody connected"
    assert result.verified is False and result.status == "failed"
    assert result.tool_result.outcome is ToolOutcome.forbidden

    direct = CountingCalendar(account=ACCOUNT)
    ex = _executor(direct, key=KEY + ":stranger")
    with execution_gate.granted_for_test(stranger, provider=ex.gate_provider,
                                         operation=ex.gate_operation, arguments=ex.gate_arguments()):
        tool_result = _run(ex.execute(stranger))
    assert direct.insert_calls == 0 and direct.get_calls == 0
    assert tool_result.verified is False
    assert tool_result.outcome is ToolOutcome.unauthorized
    assert tool_result.reason == "google_calendar_not_connected"


def test_a_verified_create_is_remembered_so_it_can_later_be_moved(student):
    """`calendar_tools.record_created` is what makes the created event findable by the update and delete
    executors — without it the event exists on Google and Bruce cannot move it. Best-effort by design,
    and written only after the read-back verified: an entity row for an unproven create would let a later
    turn try to move something that may not exist.
    """
    adapter = CountingCalendar(account=ACCOUNT)
    result = _run(_perform(student, _executor(adapter)))
    assert result.verified is True

    rows = _entities(student)
    assert len(rows) == 1, f"a verified create left no entity behind: {rows}"
    assert rows[0]["provider_event_id"] == result.tool_result.provider_entity_id
    assert rows[0]["title"] == TITLE and rows[0]["start"] == START and rows[0]["end"] == END

    # ABSENCE, paired with the control above: an unproven create records nothing.
    other = uuid4()
    _run(_seed(other))
    unproven = _run(_perform(other, _executor(Unreadable(account=ACCOUNT))))
    assert unproven.verified is False
    assert _entities(other) == [], "an unproven create was recorded as an event Bruce could later move"


def test_the_account_is_learned_from_the_created_event_when_the_connection_could_not_say(pg):
    """A `calendar.events`-only connection cannot be asked who it belongs to before a write — Google
    answers 401/403 for every pre-write identity endpoint — so the account is learned from the
    AUTHORITATIVE created-event record during the mandatory read-back and backfilled onto the
    integration. Same rule and same source of truth as `calendar_schedule`."""
    uid = uuid4()
    _run(_seed(uid, account=None))
    adapter = CountingCalendar(account=ACCOUNT)
    result = _run(_perform(uid, _executor(adapter)))

    assert result.verified is True
    integration = _run(oauth_google.get_integration(uid))
    assert integration.provider_account_id == ACCOUNT, "the account was never learned from the read-back"


def test_a_read_back_on_a_different_account_is_not_verified(pg):
    """The account binding is HARD. An event whose title and time match but which lives on a different
    Google account has not proven anything about the calendar the student connected."""
    uid = uuid4()
    _run(_seed(uid, account=ACCOUNT))
    adapter = CountingCalendar(account="someone.else@example.com")
    result = _run(_perform(uid, _executor(adapter)))

    assert adapter.insert_calls == 1
    assert result.verified is False
    assert result.tool_result.outcome is ToolOutcome.verification_failed
