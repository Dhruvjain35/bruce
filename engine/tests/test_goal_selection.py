"""WHICH open goal is this turn about — the ordered selection, and what happens when nothing decides.

THE DEFECT. `conversation_runtime` returned the NEWEST typed run and read every downstream question off
that one row: which pending Decision, which draft, which capability. With an email goal and a calendar
goal both open, a drafted subject was read as a calendar turn, "YES WRITE IT AND SEND IT" never reached
the email's Decision, and an inline reply pointing at the email proposal was answered by the model. One
cause, three symptoms: recency is a reading of the clock, and the clock knows nothing about the turn.

EVERY RULE IS TESTED IN ISOLATION AND AGAINST ITS OWN CONTROL. A selector that always picked the email
goal would satisfy most of the assertions below, so each rule is also asserted to LOSE when its evidence
points the other way, and the whole order is asserted to be an order — a lower rule must not win while a
higher one has an answer.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (conversation_graph, conversation_outcomes, conversation_runtime, crypto,
                          gmail_adapter, goal_handler, goal_runtime, goal_selection, oauth_google,
                          schema, world_state)
from bruce_engine.conversation_contract import (ConversationDecision, ExtractedEntity, IntentKind,
                                                ResponseType, RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.goal_slots import GoalKind, SlotValue, Source
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.repositories import PostgresUserRepository

gs = goal_selection

PHONE = "+15550199"
ACCOUNT = "student@example.com"
TEACHER = "alvarez@school.edu"
TZ = "America/Chicago"
SEND = "gmail.send_message"
CREATE_EVENT = "calendar.create_event"
UPDATE_EVENT = "calendar.update_event"


# ==========================================================================================================
# THE PURE SELECTOR — run dicts in, one answer out. No database, no model, no I/O.
# ==========================================================================================================

def _run_row(rid, kind, slots, *, domain, decision=None, status="understanding", outcome=""):
    """One open AgentRun shaped exactly like `goal_runtime._row_dict` returns it."""
    from bruce_engine import goal_slots
    base = {"capability": goal_slots.capability_for(kind)}
    if outcome:
        base["desired_outcome"] = outcome
    return {"id": rid, "domain": domain, "status": status,
            "goal": goal_slots.to_goal_jsonb(base, kind, slots),
            "active_decision": decision, "conversation_id": PHONE}


def _email_run(rid="email-1", *, turn_id="m3", decision=None, subject=True):
    slots = {"recipient": SlotValue(TEACHER, Source.user_stated, turn_id="m1", turn_index=1)}
    if subject:
        slots["subject"] = SlotValue("thank you", Source.model_derived, turn_id=turn_id, turn_index=3)
    return _run_row(rid, GoalKind.send_email, slots, domain="gmail", decision=decision,
                    outcome="email ms alvarez a thank-you note")


def _calendar_run(rid="cal-1", *, decision=None, turn_id=None):
    slots = {"title": SlotValue("rehearsal", Source.user_stated, turn_id=turn_id, turn_index=1),
             "start": SlotValue("2026-08-04T16:00:00", Source.user_stated, turn_id=turn_id, turn_index=1),
             "end": SlotValue("2026-08-04T17:00:00", Source.user_stated, turn_id=turn_id, turn_index=1),
             "timezone": SlotValue(TZ, Source.user_stated, turn_id=turn_id, turn_index=1)}
    return _run_row(rid, GoalKind.schedule_event, slots, domain="calendar", decision=decision,
                    outcome="book the rehearsal")


def _pending(decision_id="dec-1", *, capability=SEND, source_message_id=None):
    return {"decision_id": decision_id, "status": "pending", "capability": capability,
            "arguments_fingerprint": "fp", "question": "want me to send it?",
            "run_id": "email-1", "source_message_id": source_message_id}


def _decision(*, caps=(), entities=()):
    return ConversationDecision(
        intent=IntentKind.clarification, response_type=ResponseType.direct_answer,
        user_visible_response="ok", extracted_entities=list(entities),
        required_capabilities=list(caps), needs_mission=False, proposed_goal=None,
        risk_level=RiskLevel.none, confidence=0.9)


def _entity(kind, value):
    return ExtractedEntity(type=kind, value=value, normalized=None)


# The calendar run is NEWER in every pair below, so recency always points the wrong way. Any test that
# lands on the email goal has done so because a rule decided, not because of the order of the list.
def _both(email=None, calendar=None):
    return [calendar or _calendar_run(), email or _email_run()]


# --- the degenerate cases -----------------------------------------------------------------------------

def test_no_open_goal_selects_nothing_and_is_not_ambiguous():
    sel = gs.select([], text="send it")
    assert sel.run is None and sel.rule == gs.NOTHING_OPEN and not sel.ambiguous


def test_one_open_goal_is_the_answer_whatever_the_turn_says():
    """RULE 5, and the ONLY place recency may speak — where there is nothing to be recent about. Every
    single-goal turn in this runtime resolves here, which is why nothing outside multi-goal moved."""
    for text in ("send it", "whats 8 times 7", "", "move it to friday"):
        sel = gs.select([_email_run()], text=text)
        assert sel.run_id == "email-1" and sel.rule == gs.BY_RECENCY, text


def test_an_untyped_run_is_never_a_candidate():
    """A background audit row and a mission share this table with real goals, and only a goal has slots a
    turn can answer into."""
    untyped = {"id": "audit-1", "domain": "gmail", "status": "running", "goal": {"capability": SEND},
               "active_decision": None, "conversation_id": PHONE}
    sel = gs.select([untyped, _email_run()], text="send it")
    assert sel.run_id == "email-1" and sel.rule == gs.BY_RECENCY
    assert sel.considered == ("email-1",)


# --- rule 1: the inline reply -------------------------------------------------------------------------

def test_an_inline_reply_beats_recency():
    """THE STUDENT'S OWN POINTER. They named a message; that message belongs to one goal. Nothing else a
    turn can carry is this unambiguous, which is why it is first."""
    sel = gs.select(_both(), text="this", reply_to_message_id="m3")
    assert sel.run_id == "email-1" and sel.rule == gs.BY_REPLY


def test_the_same_reply_pointing_the_other_way_selects_the_other_goal():
    """The anti-vacuous partner. Identical turn, identical candidates, one changed message id."""
    sel = gs.select(_both(calendar=_calendar_run(turn_id="m9")), text="this", reply_to_message_id="m9")
    assert sel.run_id == "cal-1" and sel.rule == gs.BY_REPLY


def test_a_reply_to_the_proposal_message_resolves_through_the_decision():
    """A student is likeliest to reply to the message that SHOWED them the proposal, and that turn need
    not have stated any slot. The Decision records it so the reply is resolvable either way."""
    email = _email_run(decision=_pending(source_message_id="m7"), subject=False)
    sel = gs.select(_both(email=email), text="this", reply_to_message_id="m7")
    assert sel.run_id == "email-1" and sel.rule == gs.BY_REPLY


def test_a_reply_to_a_message_neither_goal_owns_falls_through():
    sel = gs.select(_both(), text="this", reply_to_message_id="m404")
    assert sel.rule != gs.BY_REPLY


# --- rule 2: the pending Decision -----------------------------------------------------------------------

def test_send_it_selects_the_goal_holding_the_pending_decision():
    """An answer belongs to the question that was asked. The calendar goal is newer and is holding no
    question, so it cannot be what "yes" is answering."""
    sel = gs.select(_both(email=_email_run(decision=_pending())), text="YES WRITE IT AND SEND IT")
    assert sel.run_id == "email-1" and sel.rule == gs.BY_DECISION


def test_a_refusal_also_lands_on_the_goal_holding_the_question():
    """The direction that matters more. Guessing here is how a bare "no" cancels the goal the student was
    not talking about — a silent, wrong cancellation rather than a visible, wrong send."""
    sel = gs.select(_both(email=_email_run(decision=_pending())), text="no dont")
    assert sel.run_id == "email-1" and sel.rule == gs.BY_DECISION


def test_a_decision_that_is_no_longer_pending_attracts_nothing():
    """An approved or closed Decision is not a question the student can be answering."""
    approved = {**_pending(), "status": "approved"}
    sel = gs.select(_both(email=_email_run(decision=approved)), text="yes")
    assert sel.rule != gs.BY_DECISION


def test_two_pending_decisions_do_not_resolve_by_this_rule():
    """"Exactly one" is the rule. Two open questions and one "yes" is precisely the turn nobody can
    answer, and answering it anyway would spend consent on the wrong operation."""
    sel = gs.select(_both(email=_email_run(decision=_pending()),
                          calendar=_calendar_run(decision=_pending("dec-2", capability=CREATE_EVENT))),
                    text="yes")
    assert sel.run is None and sel.ambiguous


def test_a_turn_that_is_not_a_yes_or_a_no_does_not_use_this_rule():
    sel = gs.select(_both(email=_email_run(decision=_pending())), text="what time is it")
    assert sel.rule != gs.BY_DECISION


def test_a_change_request_that_looks_like_a_yes_is_not_pulled_onto_the_pending_decision():
    """"yeah move it to friday" reads as an approval to a yes/no resolver and is not an approval of what
    is on screen. `continuation.resolve` already puts amendment ahead of approval for that reason; if this
    rule did not honour the same precedence, a calendar change would be answered by the email's Decision.
    """
    sel = gs.select(_both(email=_email_run(decision=_pending())), text="yeah move it to friday")
    assert sel.run_id == "cal-1", f"a calendar change landed on the email Decision ({sel.rule})"


def test_a_recipient_correction_that_looks_like_a_yes_lands_on_the_email():
    """The same rule pointing the other way — "send it to coach instead" names an address, and only an
    email goal has anywhere to put one."""
    sel = gs.select(_both(email=_email_run(decision=_pending())),
                    text="send it to coach@school.edu instead")
    assert sel.run_id == "email-1" and sel.rule == gs.BY_COMPATIBILITY


def test_a_plain_answer_is_still_a_plain_answer():
    """The control: excluding change requests must not exclude the confirmations this rule exists for."""
    for text in ("YES WRITE IT AND SEND IT", "yes", "yeah do it", "no dont"):
        sel = gs.select(_both(email=_email_run(decision=_pending())), text=text)
        assert sel.run_id == "email-1" and sel.rule == gs.BY_DECISION, text


# --- rule 3: an explicit reference ------------------------------------------------------------------------

def test_move_it_to_friday_selects_the_calendar_goal():
    """`calendar.update_event` declares no goal kind — an update and a create are different operations on
    the same piece of work — so the match is by DOMAIN. Without that this turn lands nowhere."""
    sel = gs.select(_both(), text="move it to friday", decision=_decision(caps=[UPDATE_EVENT]))
    assert sel.run_id == "cal-1" and sel.rule == gs.BY_REFERENCE


def test_a_named_send_selects_the_email_goal():
    sel = gs.select(_both(), text="ok do it", decision=_decision(caps=[SEND]))
    assert sel.run_id == "email-1" and sel.rule == gs.BY_REFERENCE


def test_naming_a_capability_no_open_goal_serves_is_a_new_goal_not_an_ambiguity():
    """Asking "which of these two do you mean" about a third thing is nonsense, and it would make a new
    goal unstartable for as long as two others were open."""
    sel = gs.select(_both(), text="what did coach say", decision=_decision(caps=["gmail.get_message"]))
    assert sel.run is None and sel.rule == gs.NEW_GOAL and not sel.ambiguous


def test_free_text_from_the_model_names_nothing():
    """"sending messages" is what the model actually emitted twenty-two times in the transcript. It joined
    to no operation then and it must join to none here."""
    sel = gs.select(_both(), text="ok do it", decision=_decision(caps=["sending messages"]))
    assert sel.rule not in (gs.BY_REFERENCE, gs.NEW_GOAL)


def test_saying_the_noun_out_loud_selects_that_goal():
    assert gs.select(_both(), text="just send the email already").run_id == "email-1"
    assert gs.select(_both(), text="put it on my calendar").run_id == "cal-1"


def test_a_turn_naming_both_nouns_names_neither():
    sel = gs.select(_both(), text="email me about the meeting")
    assert sel.rule != gs.BY_REFERENCE


# --- rule 4: unique compatibility ---------------------------------------------------------------------

def test_a_drafted_subject_and_body_land_on_the_email_goal():
    """The model drafts; the drafted values fit an email and nothing about a rehearsal. Judged through
    `goal_handler.entity_slots`, the same function that would STORE them — a rule that guessed
    compatibility differently would select a goal the turn then fails to update."""
    drafts = _decision(entities=[_entity("subject", "thank you"), _entity("body_text", "hi")])
    sel = gs.select(_both(), text="js make it whatever u think and send it alr", decision=drafts)
    assert sel.run_id == "email-1" and sel.rule == gs.BY_COMPATIBILITY


def test_a_start_time_lands_on_the_calendar_goal():
    when = _decision(entities=[_entity("start", "2026-08-07T17:00:00")])
    sel = gs.select(_both(), text="ok", decision=when)
    assert sel.run_id == "cal-1" and sel.rule == gs.BY_COMPATIBILITY


def test_a_value_both_goals_could_hold_resolves_nothing_by_compatibility():
    """A label fits an email subject AND an event title. Two answers is not an answer."""
    sel = gs.select(_both(), text="call it the spring thing")
    assert sel.rule != gs.BY_COMPATIBILITY


# --- the ORDER is an order --------------------------------------------------------------------------------

def test_a_reply_outranks_the_pending_decision():
    """Both rules have an answer and they disagree. The student's own pointer wins, because it is the one
    piece of evidence they authored on purpose."""
    email = _email_run(decision=_pending())
    sel = gs.select(_both(email=email, calendar=_calendar_run(turn_id="m9")),
                    text="yes", reply_to_message_id="m9")
    assert sel.run_id == "cal-1" and sel.rule == gs.BY_REPLY


def test_the_pending_decision_outranks_a_named_capability():
    sel = gs.select(_both(email=_email_run(decision=_pending())), text="yes do it",
                    decision=_decision(caps=[UPDATE_EVENT]))
    assert sel.run_id == "email-1" and sel.rule == gs.BY_DECISION


def test_a_named_capability_outranks_compatibility():
    drafts = _decision(caps=[UPDATE_EVENT], entities=[_entity("subject", "thank you")])
    sel = gs.select(_both(), text="ok", decision=drafts)
    assert sel.run_id == "cal-1" and sel.rule == gs.BY_REFERENCE


def test_recency_never_breaks_a_tie():
    """THE DEFECT, asserted as a property. Two goals, no evidence, and the newest one does NOT win."""
    sel = gs.select(_both(email=_email_run(decision=_pending())), text="yeah")
    assert sel.run_id == "email-1"                       # by the Decision, not by the clock
    tied = gs.select(_both(email=_email_run(decision=_pending()),
                           calendar=_calendar_run(decision=_pending("dec-2", capability=CREATE_EVENT))),
                     text="yeah")
    assert tied.run is None, "recency broke a tie"


# --- rule 6: ask, and only when the turn is about the work ------------------------------------------------

@pytest.mark.parametrize("text", ["whats 8 times 7", "lol", "my chem teacher is so annoying", ""])
def test_conversation_beside_two_goals_is_not_an_ambiguity(text):
    """AMBIGUITY IS NOT IRRELEVANCE. Claiming every turn that fails to name a goal would turn "whats 8
    times 7" into an interrogation about a subject line."""
    sel = gs.select(_both(email=_email_run(decision=_pending())), text=text)
    assert not sel.ambiguous and sel.rule == gs.NOT_ABOUT_A_GOAL, text


def test_a_bare_pointer_with_nothing_to_point_at_asks():
    """"this", with no reply reference and two goals open. It plainly means one of them and names
    neither, which is exactly the turn rule 6 exists for."""
    sel = gs.select(_both(), text="this")
    assert sel.ambiguous and sel.rule == gs.AMBIGUOUS
    assert sel.question and "which one" in sel.question.lower()


def test_the_question_names_both_things():
    sel = gs.select(_both(), text="this")
    assert "thank-you" in sel.question and "rehearsal" in sel.question, sel.question


def test_an_ambiguous_selection_carries_no_run():
    sel = gs.select(_both(), text="this")
    assert sel.run is None and sel.run_id is None


def test_every_rule_this_module_can_report_is_in_the_closed_set():
    observed = {
        gs.select([], text="x").rule,
        gs.select([_email_run()], text="x").rule,
        gs.select(_both(), text="this", reply_to_message_id="m3").rule,
        gs.select(_both(email=_email_run(decision=_pending())), text="yes").rule,
        gs.select(_both(), text="ok", decision=_decision(caps=[UPDATE_EVENT])).rule,
        gs.select(_both(), text="ok", decision=_decision(caps=["gmail.get_message"])).rule,
        gs.select(_both(), text="ok",
                  decision=_decision(entities=[_entity("subject", "s")])).rule,
        gs.select(_both(), text="this").rule,
        gs.select(_both(), text="lol").rule,
    }
    assert observed == gs.REASONS, (
        f"outside the vocabulary: {observed - gs.REASONS}; unreachable: {gs.REASONS - observed}")
    assert len(observed) == 9, "two rules collapsed and stopped being distinguishable"


# --- the second instalment of evidence --------------------------------------------------------------------

def test_refine_breaks_a_tie_the_first_pass_could_not():
    """A drafted subject is the reasoner's output and cannot exist before it runs, so the selector gets it
    on a second pass."""
    first = gs.select(_both(), text="js make it whatever and send it alr")
    assert first.run is None
    later = gs.refine(first, _both(), text="js make it whatever and send it alr",
                      decision=_decision(entities=[_entity("subject", "thank you")]))
    assert later.run_id == "email-1" and later.rule == gs.BY_COMPATIBILITY


def test_refine_never_moves_a_turn_off_a_goal_already_selected():
    """THE ONE RULE. `continuation.resolve` has by then bound this turn's yes to that run; re-selecting
    underneath it would leave a confirmation of one goal's Decision sitting on another's."""
    first = gs.select(_both(email=_email_run(decision=_pending())), text="yes")
    assert first.run_id == "email-1"
    later = gs.refine(first, _both(email=_email_run(decision=_pending())), text="yes",
                      decision=_decision(caps=[UPDATE_EVENT]))
    assert later.run_id == "email-1", "the model's reading moved a bound confirmation to another goal"


# ==========================================================================================================
# THROUGH THE REAL RUNTIME — the guarantees a pure selector cannot make on its own.
# ==========================================================================================================

users = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
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


def _go(c):
    return asyncio.run(c)


class _Reasoner:
    provider = model = "fake"
    supports_vision = True

    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def decide(self, *, text, images, context):
        self.calls += 1
        return ReasonResult(decision=self.decision, provider="fake", model="fake",
                            input_tokens=0, output_tokens=0, latency_ms=1)


class Student:
    """One student with BOTH kinds of goal open, driven through the production inbound runtime."""

    def __init__(self, decision=None):
        self.uid = uuid4()
        _go(self._seed())
        self.gmail = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
        self.reasoner = _Reasoner(decision or _decision())
        self.channel = FakeChannel()
        self.tag = uuid4().hex[:8]
        self.n = 0

    async def _seed(self):
        await users.ensure(self.uid, auth_provider="test")
        async with user_session(self.uid) as s:
            s.add(schema.Integration(
                user_id=self.uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
                scopes=["https://www.googleapis.com/auth/calendar.events",
                        "https://www.googleapis.com/auth/gmail.send"],
                refresh_token_encrypted=crypto.encrypt("rt"), selected_calendar_id="primary",
                status="connected"))
        await world_state.set_timezone(self.uid, TZ, source="user_stated")

    def open_email_goal(self) -> str:
        # The recipient only. A drafted subject arriving later is `model_derived`, and `merge_slots` will
        # not let a guess displace a stated value — so seeding one here would make the slot test pass or
        # fail on provenance rather than on which goal the turn reached.
        slots = {"recipient": SlotValue(TEACHER, Source.user_stated, turn_index=1)}
        return _go(goal_runtime.ensure_goal(self.uid, capability=SEND, conversation_id=PHONE,
                                            slots_in=slots, turn_index=1, decision=None)).run_id

    def open_calendar_goal(self) -> str:
        slots = {"title": SlotValue("rehearsal", Source.user_stated, turn_index=1),
                 "start": SlotValue("2026-08-04T16:00:00", Source.user_stated, turn_index=1),
                 "end": SlotValue("2026-08-04T17:00:00", Source.user_stated, turn_index=1),
                 "timezone": SlotValue(TZ, Source.user_stated, turn_index=1)}
        return _go(goal_runtime.ensure_goal(self.uid, capability=CREATE_EVENT, conversation_id=PHONE,
                                            slots_in=slots, turn_index=1, decision=None)).run_id

    def say(self, text: str, *, reply_to: str | None = None) -> str:
        self.n += 1
        msg = InboundMessage(provider_message_id=f"{self.tag}-m{self.n}",
                             channel=ChannelKind.self_hosted_imessage, channel_identity=PHONE,
                             text=text, attachments=[],
                             timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False,
                             reply_to_message_id=reply_to)
        msg.user_id = self.uid
        _go(conversation_graph.ingest_inbound_message(msg))
        handlers = [goal_handler.GoalHandler(adapter=self.gmail) if h.name == "goal" else h
                    for h in conversation_outcomes.default_handlers()]
        out = _go(conversation_runtime.handle(self.channel, msg, user_id=self.uid, reply_target=PHONE,
                                              reasoner=self.reasoner, handlers=handlers))
        assert out.status == "processed", out.status
        return _go(self._last_reply())

    async def _last_reply(self) -> str:
        async with user_session(self.uid) as s:
            rows = (await s.execute(
                sa_select(schema.ConversationTurn)
                .where(schema.ConversationTurn.user_id == self.uid,
                       schema.ConversationTurn.role == "assistant")
                .order_by(schema.ConversationTurn.created_at.desc()).limit(1))).scalars().all()
        return rows[0].text if rows else ""

    def runs(self) -> list[dict]:
        return _go(goal_runtime.open_runs(self.uid, conversation_id=PHONE))

    def all_runs(self) -> int:
        async def _n():
            async with user_session(self.uid) as s:
                rows = (await s.execute(sa_select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid))).scalars().all()
            return len(rows)
        return _go(_n())


def test_an_ambiguous_turn_writes_nothing_and_asks_once(clean_db):
    """RULE 6 END TO END, and the assertion the pure tests cannot make: no provider call, no new run, and
    both goals left exactly as they were. The reply is the one question."""
    s = Student()
    email = s.open_email_goal()
    calendar = s.open_calendar_goal()
    before = s.all_runs()

    reply = s.say("this")

    assert s.gmail.send_calls == 0, "an ambiguous turn reached the provider"
    assert s.all_runs() == before, "an ambiguous turn opened a new run"
    statuses = {r["id"]: r["status"] for r in s.runs()}
    assert statuses.get(email) == "understanding" and statuses.get(calendar) == "understanding", \
        "an ambiguous turn moved a goal it could not identify"
    assert "which one" in reply.lower(), f"the student was not asked which one: {reply!r}"


def test_an_ambiguous_refusal_cancels_neither_goal(clean_db):
    """The quiet failure this ordering exists to stop: with two goals open and one bare "no", guessing
    closes the goal the student was not talking about, and nothing ever says so."""
    s = Student()
    email = s.open_email_goal()
    calendar = s.open_calendar_goal()

    s.say("no")

    statuses = {r["id"]: r["status"] for r in s.runs()}
    assert statuses.get(email) != "cancelled" and statuses.get(calendar) != "cancelled", \
        "a bare no with two goals open cancelled one of them"
    assert s.gmail.send_calls == 0


def test_conversation_beside_two_goals_is_answered_normally(clean_db):
    """The control for both tests above. If ambiguity claimed every unmatched turn, this would be an
    interrogation instead of an answer — and the goals must still be untouched."""
    s = Student(decision=ConversationDecision(
        intent=IntentKind.casual, response_type=ResponseType.direct_answer, user_visible_response="56",
        extracted_entities=[], required_capabilities=[], needs_mission=False, proposed_goal=None,
        risk_level=RiskLevel.none, confidence=0.9))
    email = s.open_email_goal()
    calendar = s.open_calendar_goal()
    before = s.all_runs()

    reply = s.say("whats 8 times 7")

    assert "which one" not in reply.lower(), f"an unrelated turn was answered with a goal question: {reply!r}"
    assert s.all_runs() == before
    statuses = {r["id"]: r["status"] for r in s.runs()}
    assert statuses.get(email) == "understanding" and statuses.get(calendar) == "understanding"


def test_two_goals_stay_open_together_and_never_exchange_slots(clean_db):
    """The invariant selection-by-kind exists for, asserted against the runtime rather than the selector:
    a drafted subject reaches the email goal and the calendar goal is not rewritten."""
    from bruce_engine import goal_slots

    s = Student(decision=_decision(entities=[_entity("subject", "rec letter thanks")]))
    email = s.open_email_goal()
    calendar = s.open_calendar_goal()

    s.say("ok")

    rows = {r["id"]: r for r in s.runs()}
    _k, email_slots = goal_slots.from_goal_jsonb(rows[email]["goal"])
    _k2, cal_slots = goal_slots.from_goal_jsonb(rows[calendar]["goal"])
    assert email_slots["subject"].value == "rec letter thanks", "the drafted subject reached neither goal"
    assert "subject" not in cal_slots and "recipient" not in cal_slots, "an email slot landed in calendar"
    assert cal_slots["title"].value == "rehearsal", "the calendar goal was rewritten"
    assert "start" not in email_slots, "a calendar slot landed in the email goal"
