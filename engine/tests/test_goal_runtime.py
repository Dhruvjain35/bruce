"""The backend-owned goal lifecycle, against the transcript that motivated it.

The failure being pinned down here is a real one, verified in the database: a student asked Bruce to
email one named address a thank-you note, and twenty-two turns produced ZERO missions and ZERO
agent_runs. Turn 2 asked for a recipient turn 1 had supplied, an inline reply of "this" resolved
nothing, and the last turn said "i can't send messages for you" while the broker reported
`gmail.send_message` ok=True. The model had emitted `needs_mission=false` and
`required_capabilities=["sending messages"]`, and the runtime believed both.

So the tests are written as that transcript: replay it as slot inputs and the invariants that were
violated become assertions — ONE run, the turn-1 recipient still there at turn 5, `missing` shrinking
and never growing, and a goal created despite `needs_mission=false`. The PG tests use the real database
because "zero agent_runs" is a claim about rows, and a fake store cannot falsify it.

Every absence assertion is paired with a positive control: a test that only ever asserts "nothing
happened" passes just as well when the code does nothing at all.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_run_store, goal_runtime, goal_slots
from bruce_engine.contract import MachineState
from bruce_engine.conversation_contract import ConversationDecision, IntentKind, ResponseType
from bruce_engine.goal_slots import GoalKind, SlotValue, Source
from bruce_engine.repositories import PostgresUserRepository

SEND = "gmail.send_message"
SCHEDULE = "calendar.create_event"

users = PostgresUserRepository()


def _run(coro):
    return asyncio.run(coro)


def _decision(intent, *, needs_mission: bool = False, proposed_goal: str | None = None):
    """A REAL ConversationDecision, not a stub — the contract object is what the runtime will hand in,
    and a stub would let a field rename pass unnoticed."""
    return ConversationDecision(intent=intent, response_type=ResponseType.direct_answer,
                                user_visible_response="ok", needs_mission=needs_mission,
                                proposed_goal=proposed_goal)


def _said(value, turn: int = 0) -> SlotValue:
    return SlotValue(value, Source.user_stated, turn_index=turn)


def _guessed(value, turn: int = 0) -> SlotValue:
    return SlotValue(value, Source.model_derived, turn_index=turn)


# --- pure: the backend decides, and needs_mission is advisory ---------------------------------------------

def test_needs_mission_false_still_creates_a_goal():
    """The exact field that said "no" twenty-two times. It is not consulted."""
    v = goal_runtime.creation_verdict(
        decision=_decision(IntentKind.actionable, needs_mission=False), capability=SEND, continuation=False)
    assert v.create is True and v.reason == goal_runtime.ACTIONABLE


def test_conversational_turn_creates_nothing_even_when_needs_mission_is_true():
    """And the reverse: a hallucinated mission flag on a chat turn opens no durable state. Paired with
    the test above, so this cannot pass by the function simply always returning the same answer."""
    v = goal_runtime.creation_verdict(
        decision=_decision(IntentKind.casual, needs_mission=True, proposed_goal="do my homework"),
        capability=SEND, continuation=False)
    assert v.create is False and v.reason == goal_runtime.NOT_ACTIONABLE


def test_free_text_capability_is_refused_and_named_apart_from_a_typo():
    """"sending messages" is a model defect; "gmail.send_mesage" is a different one. Counting them as the
    same thing is why nobody could tell what was actually broken."""
    prose = goal_runtime.creation_verdict(decision=_decision(IntentKind.actionable),
                                          capability="sending messages", continuation=False)
    typo = goal_runtime.creation_verdict(decision=_decision(IntentKind.actionable),
                                         capability="gmail.send_mesage", continuation=False)
    kindless = goal_runtime.creation_verdict(decision=_decision(IntentKind.actionable),
                                             capability="gmail.get_message", continuation=False)
    none_named = goal_runtime.creation_verdict(decision=_decision(IntentKind.actionable),
                                               capability=None, continuation=False)
    assert prose.reason == goal_runtime.NOT_AN_OPERATION_ID
    assert typo.reason == goal_runtime.UNKNOWN_CAPABILITY
    assert kindless.reason == goal_runtime.NO_GOAL_KIND     # a real op with nothing to collect
    assert none_named.reason == goal_runtime.NO_CAPABILITY
    assert not any(v.create for v in (prose, typo, kindless, none_named))
    # positive control: the real id, same intent, does create.
    assert goal_runtime.should_create_goal(decision=_decision(IntentKind.actionable),
                                           capability=SEND, continuation=False) is True


def test_a_continuation_outranks_the_intent():
    """The turn that answers Bruce's question is intent=clarification and names no capability. If intent
    had to be actionable, that answer would be dropped — which is the transcript's second turn."""
    cont = goal_runtime.creation_verdict(decision=_decision(IntentKind.clarification),
                                         capability=SEND, continuation=True)
    assert cont.create is True and cont.reason == goal_runtime.CONTINUATION
    # positive control: the same turn with nothing open creates nothing.
    assert goal_runtime.should_create_goal(decision=_decision(IntentKind.clarification),
                                           capability=SEND, continuation=False) is False


def test_reason_constants_are_closed():
    for reason in (goal_runtime.CONTINUATION, goal_runtime.ACTIONABLE, goal_runtime.NOT_ACTIONABLE,
                   goal_runtime.NO_CAPABILITY, goal_runtime.NOT_AN_OPERATION_ID,
                   goal_runtime.UNKNOWN_CAPABILITY, goal_runtime.NO_GOAL_KIND):
        assert reason in goal_runtime.REASONS
    assert "made_up_reason" not in goal_runtime.REASONS


# --- pure: the question comes from the slot names ---------------------------------------------------------

def _view(*, missing=(), slots=None, capability=SEND, confirmed=False, status="preparing"):
    slots = dict(slots or {})
    return goal_runtime.GoalView(
        run_id="run-1", kind=GoalKind.send_email, capability=capability, status=status, slots=slots,
        missing=tuple(missing), ready_to_execute=not missing,
        decision_id="dec-1" if confirmed else None, confirmed=confirmed)


def test_question_names_only_what_is_missing():
    q = goal_runtime.clarifying_question(("subject",))
    assert "subject" in q
    # the recipient was given on turn 1 and must never be asked for again...
    assert "go to" not in q
    # ...positive control: when the recipient IS missing, the question does ask for it.
    assert "go to" in goal_runtime.clarifying_question(("recipient", "subject"))


def test_no_missing_slots_means_no_question_at_all():
    assert goal_runtime.clarifying_question(()) == ""
    assert goal_runtime.clarifying_question(("body",)) != ""


def test_every_declared_slot_has_its_own_phrase():
    """Drift guard: a new kind whose slots have no phrasing would ask the student for "start" verbatim."""
    for kind in GoalKind:
        for spec in goal_slots.slot_specs(kind):
            asked = goal_runtime.clarifying_question((spec.name,))
            assert asked and asked != f"I still need {spec.name}."
    # positive control: an undeclared name really does fall back to itself, so the check above has teeth.
    assert goal_runtime.clarifying_question(("wombat",)) == "I still need wombat."


def test_unknown_slot_names_are_kept_but_countable():
    mixed = {"to": _said("a@b.c"), "recipient": _said("a@b.c")}
    assert goal_runtime.unknown_slots(GoalKind.send_email, mixed) == ("to",)
    assert goal_runtime.unknown_slots(GoalKind.send_email, {"recipient": _said("a@b.c")}) == ()


# --- pure: what happens next ------------------------------------------------------------------------------

def test_unavailable_capability_is_reported_before_slots_are_collected():
    """The transcript collected nothing and denied the ability at the end. The inverse — collecting three
    slots and only then admitting Gmail is disconnected — is just as bad, so availability is checked
    first even when slots are also missing."""
    step = goal_runtime.next_step(_view(missing=("recipient", "subject", "body")),
                                  availability_ok=False, availability_status="disconnected")
    assert step.disposition == goal_runtime.BLOCKED_CAPABILITY
    assert step.availability_status == "disconnected"
    assert step.target_state is MachineState.blocked
    # positive control: available, same missing slots -> it asks instead of blocking.
    ok = goal_runtime.next_step(_view(missing=("recipient",)), availability_ok=True, availability_status="ok")
    assert ok.disposition == goal_runtime.ASK_MISSING


def test_ask_carries_slot_names_not_prose():
    step = goal_runtime.next_step(_view(missing=("recipient", "subject")), availability_ok=True)
    assert step.disposition == goal_runtime.ASK_MISSING
    assert step.missing == ("recipient", "subject")
    assert step.question == goal_runtime.clarifying_question(("recipient", "subject"))
    assert step.target_state is MachineState.preparing


def test_full_slots_propose_confirmation_then_execute():
    full = {"recipient": _said("a@b.c"), "subject": _said("thanks"), "body": _said("thank you")}
    unconfirmed = goal_runtime.next_step(_view(slots=full), availability_ok=True)
    assert unconfirmed.disposition == goal_runtime.PROPOSE_CONFIRMATION
    assert unconfirmed.target_state is MachineState.awaiting_approval
    confirmed = goal_runtime.next_step(_view(slots=full, confirmed=True), availability_ok=True)
    assert confirmed.disposition == goal_runtime.EXECUTE
    assert confirmed.target_state is MachineState.executing


def test_a_guessed_required_slot_forces_a_confirmation_even_for_a_read():
    """`_needs_confirmation` has two independent triggers. Held against a non-write capability, only
    provenance can move it — so this proves the guess trigger rather than the write trigger."""
    guessed = {"recipient": _guessed("a@b.c"), "subject": _said("thanks"), "body": _said("thank you")}
    stated = {**guessed, "recipient": _said("a@b.c")}
    read_cap = "gmail.get_message"                                   # write=False in the registry
    assert goal_runtime.next_step(_view(slots=guessed, capability=read_cap),
                                  availability_ok=True).disposition == goal_runtime.PROPOSE_CONFIRMATION
    assert goal_runtime.next_step(_view(slots=stated, capability=read_cap),
                                  availability_ok=True).disposition == goal_runtime.EXECUTE


def test_ensure_goal_refuses_a_capability_with_no_declared_kind():
    with pytest.raises(ValueError):
        _run(goal_runtime.ensure_goal(uuid4(), capability="gmail.get_message", conversation_id=None,
                                      slots_in={}, turn_index=1))


# --- the real database ------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    yield
    db._engine = None; db._sessionmaker = None


async def _count_runs(uid) -> int:
    async with db.user_session(uid) as s:
        return (await s.execute(sa_text("SELECT count(*) FROM agent_runs WHERE user_id = :u"),
                                {"u": str(uid)})).scalar()


def _user():
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    return uid


def _turn(uid, conv, *, index, slots=None, proposed=None, decision=None):
    """One inbound turn, wired the way the runtime is meant to wire it: the backend resolves the
    capability, reads continuation from the DATABASE, and only then decides whether a goal exists.
    Returns the GoalView, or None when the turn creates nothing."""
    async def _go():
        cap = await goal_runtime.resolve_capability(uid, proposed=proposed, conversation_id=conv)
        cont = await goal_runtime.is_continuation(uid, capability=cap, conversation_id=conv)
        if not goal_runtime.should_create_goal(decision=decision, capability=cap, continuation=cont):
            return None
        return await goal_runtime.ensure_goal(uid, capability=cap, conversation_id=conv,
                                              slots_in=slots or {}, turn_index=index, decision=decision)
    return _run(_go())


def test_the_transcript_replayed_produces_exactly_one_run(clean_db):
    uid, conv = _user(), str(uuid4())
    seen: list[tuple[str, ...]] = []

    # turn 1 — the ask, with the address in it. needs_mission=False, exactly as the model emitted it.
    t1 = _turn(uid, conv, index=1, proposed=SEND,
               decision=_decision(IntentKind.actionable, needs_mission=False,
                                  proposed_goal="email a thank-you note"),
               slots={"recipient": _said("alvarez@school.edu"), "purpose": _said("thank you")})
    assert t1 is not None and t1.created and t1.kind is GoalKind.send_email
    seen.append(t1.missing)

    # turn 2 — "what's 8x7" mid-flow. It must not open a second run, and must not disturb the slots.
    t2 = _turn(uid, conv, index=2, decision=_decision(IntentKind.casual, needs_mission=True))
    assert t2 is None or (t2.run_id == t1.run_id and not t2.created)
    assert _run(_count_runs(uid)) == 1

    # turn 3 — the inline reply of "this": no capability named, nothing extracted. It resolved to nothing
    # in production; here it continues the open goal and changes no slot.
    t3 = _turn(uid, conv, index=3, decision=_decision(IntentKind.clarification), slots={})
    assert t3 is not None and t3.run_id == t1.run_id and not t3.created
    assert t3.slots["recipient"].value == "alvarez@school.edu"
    seen.append(t3.missing)

    # turn 4 — what it should say.
    t4 = _turn(uid, conv, index=4, decision=_decision(IntentKind.actionable), proposed=SEND,
               slots={"body": _said("Thank you for writing my recommendation letter.")})
    assert t4.run_id == t1.run_id
    seen.append(t4.missing)

    # turn 5 — the subject line.
    t5 = _turn(uid, conv, index=5, decision=_decision(IntentKind.clarification),
               slots={"subject": _said("Thank you")})
    assert t5.run_id == t1.run_id
    seen.append(t5.missing)

    assert _run(_count_runs(uid)) == 1                       # ONE run for the whole conversation
    # the recipient given on turn 1 is still there on turn 5, and still marked as something the STUDENT
    # said — the fact the transcript re-asked for on turn 2.
    assert t5.slots["recipient"].value == "alvarez@school.edu"
    assert t5.slots["recipient"].source is Source.user_stated
    # missing only ever shrinks. A question Bruce already had the answer to can never come back.
    for earlier, later in zip(seen, seen[1:]):
        assert set(later) <= set(earlier)
    assert t5.missing == () and t5.ready_to_execute is True
    assert t5.tool_arguments() == {"to": "alvarez@school.edu", "subject": "Thank you",
                                   "body": "Thank you for writing my recommendation letter."}

    # and it is durable: read the run back cold, as a later turn or a worker would.
    reloaded = _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email, conversation_id=conv))
    kind, slots = goal_slots.from_goal_jsonb(reloaded["goal"])
    assert kind is GoalKind.send_email and slots["recipient"].value == "alvarez@school.edu"
    assert reloaded["goal"]["desired_outcome"] == "email a thank-you note"


def test_a_purely_conversational_turn_creates_zero_runs(clean_db):
    uid, conv = _user(), str(uuid4())
    assert _turn(uid, conv, index=1,
                 decision=_decision(IntentKind.educational_help, needs_mission=True,
                                    proposed_goal="do my homework")) is None
    assert _run(_count_runs(uid)) == 0
    # positive control: the same user, one actionable turn later, does get a run.
    assert _turn(uid, conv, index=2, proposed=SEND, decision=_decision(IntentKind.actionable),
                 slots={"recipient": _said("a@b.c")}) is not None
    assert _run(_count_runs(uid)) == 1


def test_two_kinds_coexist_without_mixing_slots(clean_db):
    uid, conv = _user(), str(uuid4())
    email = _turn(uid, conv, index=1, proposed=SEND, decision=_decision(IntentKind.actionable),
                  slots={"recipient": _said("alvarez@school.edu")})
    event = _turn(uid, conv, index=2, proposed=SCHEDULE, decision=_decision(IntentKind.actionable),
                  slots={"title": _said("Rehearsal")})
    assert email.run_id != event.run_id and event.kind is GoalKind.schedule_event

    # interleaved: the email gets a subject, the event gets a start, in that order.
    email2 = _turn(uid, conv, index=3, proposed=SEND, decision=_decision(IntentKind.clarification),
                   slots={"subject": _said("Thank you")})
    event2 = _turn(uid, conv, index=4, proposed=SCHEDULE, decision=_decision(IntentKind.clarification),
                   slots={"start": _said("2026-08-01T17:00:00")})

    assert email2.run_id == email.run_id and event2.run_id == event.run_id
    assert _run(_count_runs(uid)) == 2
    # neither goal can see the other's slots...
    assert set(email2.slots) == {"recipient", "subject"}
    assert set(event2.slots) == {"title", "start"}
    # ...and each still knows what IT is waiting for.
    assert email2.missing == ("body",)
    assert event2.missing == ()
    # a turn naming nothing while two kinds are open is ambiguous, and ambiguity resolves to nothing
    # rather than to a guess that would put a recipient in the calendar goal.
    assert _run(goal_runtime.resolve_capability(uid, proposed=None, conversation_id=conv)) == ""
    assert _turn(uid, conv, index=5, decision=_decision(IntentKind.clarification),
                 slots={"body": _said("Thank you!")}) is None
    assert _run(_count_runs(uid)) == 2
    # positive control: name it, and the body lands in the email goal and nowhere else.
    named = _turn(uid, conv, index=6, proposed=SEND, decision=_decision(IntentKind.clarification),
                  slots={"body": _said("Thank you!")})
    assert named.run_id == email.run_id and named.missing == ()
    _, event_slots = goal_slots.from_goal_jsonb(
        _run(goal_runtime.open_goal_of_kind(uid, GoalKind.schedule_event))["goal"])
    assert set(event_slots) == {"title", "start"}


def test_open_goal_sees_a_gmail_run_that_the_calendar_default_hides(clean_db):
    uid, conv = _user(), str(uuid4())
    view = _turn(uid, conv, index=1, proposed=SEND, decision=_decision(IntentKind.actionable),
                 slots={"recipient": _said("a@b.c")})
    # the known trap: agent_run_store.latest_active defaults to domain="calendar", so an email run is
    # structurally invisible to a caller that does not think to pass a domain.
    assert _run(agent_run_store.latest_active(uid)) is None
    assert _run(agent_run_store.latest_active(uid, domain=None))["id"] == view.run_id
    # open_goal never takes that default.
    assert _run(goal_runtime.open_goal(uid))["id"] == view.run_id
    assert _run(goal_runtime.open_goal(uid, conversation_id=conv))["id"] == view.run_id


def test_another_conversation_does_not_continue_this_goal(clean_db):
    uid, a, b = _user(), str(uuid4()), str(uuid4())
    first = _turn(uid, a, index=1, proposed=SEND, decision=_decision(IntentKind.actionable),
                  slots={"recipient": _said("a@b.c")})
    assert _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email, conversation_id=b)) is None
    # positive control: the conversation it belongs to still finds it, and so does an unscoped read.
    assert _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email, conversation_id=a))["id"] == first.run_id
    assert _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email))["id"] == first.run_id
    # so a send started in another thread opens its own run rather than absorbing this one's slots.
    second = _turn(uid, b, index=1, proposed=SEND, decision=_decision(IntentKind.actionable),
                   slots={"recipient": _said("c@d.e")})
    assert second.run_id != first.run_id and set(second.slots) == {"recipient"}
    assert second.slots["recipient"].value == "c@d.e"
    assert _run(_count_runs(uid)) == 2


def test_an_unattributed_run_stays_continuable_from_any_conversation(clean_db):
    """A background mission is created by the worker with no conversation at all. Hiding it would make a
    student who asks about it in a new thread start a SECOND send for the same thing."""
    uid, conv = _user(), str(uuid4())
    goal = goal_slots.to_goal_jsonb({"action": SEND}, GoalKind.send_email, {"recipient": _said("a@b.c", 1)})
    orphan = _run(agent_run_store.create_run(uid, domain="gmail", goal=goal))
    found = _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email, conversation_id=conv))
    assert found is not None and found["id"] == orphan["id"]
    # positive control that the scoping is real: attribute it to a DIFFERENT conversation and it hides.
    _run(agent_run_store.update_run(uid, UUID(orphan["id"]),
                                    goal={**goal, goal_runtime.CONVERSATION_KEY: str(uuid4())}))
    assert _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email, conversation_id=conv)) is None


def test_a_correction_survives_persistence_and_a_guess_does_not(clean_db):
    uid, conv = _user(), str(uuid4())
    _turn(uid, conv, index=1, proposed=SEND, decision=_decision(IntentKind.actionable),
          slots={"recipient": _said("first@school.edu")})
    # the model decides on a different address two turns later — a guess never beats what was stated.
    guessed = _turn(uid, conv, index=2, proposed=SEND, decision=_decision(IntentKind.actionable),
                    slots={"recipient": _guessed("wrong@school.edu")})
    assert guessed.slots["recipient"].value == "first@school.edu"
    # the student corrects it — a later stated value does win, and it wins after a round trip through JSONB.
    fixed = _turn(uid, conv, index=3, proposed=SEND, decision=_decision(IntentKind.status_cancel_correction),
                  slots={"recipient": _said("second@school.edu")})
    assert fixed.slots["recipient"].value == "second@school.edu"
    _, reloaded = goal_slots.from_goal_jsonb(
        _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email, conversation_id=conv))["goal"])
    assert reloaded["recipient"].value == "second@school.edu"
    assert _run(_count_runs(uid)) == 1


async def _force_status(uid, run_id: str, status: str) -> None:
    """Set a run's status directly. Deliberately NOT through `agent_run_store.update_run`: that path
    enforces the transition machine, and this test is about which statuses `goal_runtime` treats as over,
    not about how a run legitimately reaches one."""
    async with db.user_session(uid) as s:
        await s.execute(sa_text("UPDATE agent_runs SET status = :st WHERE id = :id AND user_id = :u"),
                        {"st": status, "id": run_id, "u": str(uid)})


def test_a_terminal_run_stops_absorbing_turns(clean_db):
    """A finished goal must not swallow the next thing the student says — the next send is a new send."""
    uid, conv = _user(), str(uuid4())
    first = _turn(uid, conv, index=1, proposed=SEND, decision=_decision(IntentKind.actionable),
                  slots={"recipient": _said("a@b.c")})
    _run(_force_status(uid, first.run_id, MachineState.succeeded.value))
    assert _run(goal_runtime.open_goal_of_kind(uid, GoalKind.send_email, conversation_id=conv)) is None
    second = _turn(uid, conv, index=2, proposed=SEND, decision=_decision(IntentKind.actionable),
                   slots={"recipient": _said("d@e.f")})
    assert second.created and second.run_id != first.run_id
    assert second.slots["recipient"].value == "d@e.f"
    # positive control: an OPEN run of the same kind is still continued, so the exclusion is about the
    # terminal status and not about the query being broken.
    third = _turn(uid, conv, index=3, proposed=SEND, decision=_decision(IntentKind.clarification),
                  slots={"subject": _said("hi")})
    assert third.run_id == second.run_id and not third.created
