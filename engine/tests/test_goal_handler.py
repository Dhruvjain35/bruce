"""The brain spine, LIVE — driven through the real inbound runtime against the transcript that motivated it.

THE FAILURE BEING PINNED DOWN, verified in the database and not a hypothetical. A student asked Bruce to
email one named address a thank-you note. Turn 2 asked for a recipient turn 1 had supplied. An inline reply
of "this" resolved nothing. The last of twenty-two turns said "i can't send messages for you" while
`tool_broker` was answering ok=True for `gmail.send_message` in the same process. Twenty-two turns produced
ZERO missions and ZERO agent_runs.

So this file replays that shape as a real conversation through `conversation_runtime.handle` — real
Postgres, real router, real continuation, real goal runtime, real execution gate, real MutationGateway, and
a FakeGmailAdapter that enforces Google's own send/read-back rules. Nothing about Bruce is mocked: the only
substitutions are the model (a scripted reasoner) and the provider (an in-memory Gmail). The invariants that
were violated become assertions:

  * ONE run across the whole exchange, and the recipient given on turn 2 is still there on turn 5;
  * a conversational turn creates nothing, PAIRED with the actionable turn that does — an absence assertion
    on its own passes just as well when the code does nothing at all;
  * `needs_mission=False` creates a goal anyway, because the backend decides and that field is never read;
  * ZERO provider calls before the student confirms, EXACTLY ONE after, and a repeated confirmation adds
    none.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (conversation_outcomes, conversation_runtime, crypto, gmail_adapter,
                          goal_handler, goal_runtime, goal_slots, oauth_google, schema)
from bruce_engine.conversation_contract import (ConversationDecision, ExtractedEntity, IntentKind,
                                                ResponseType, RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.goal_slots import GoalKind, SlotValue, Source
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.repositories import PostgresUserRepository

PHONE = "+15550199"
ACCOUNT = "me@example.com"
TEACHER = "alvarez@school.edu"
CAL = "https://www.googleapis.com/auth/calendar.events"
GSEND = "https://www.googleapis.com/auth/gmail.send"
GREAD = "https://www.googleapis.com/auth/gmail.readonly"
SEND = "gmail.send_message"
CREATE_EVENT = "calendar.create_event"

users = PostgresUserRepository()


def _run(c):
    return asyncio.run(c)


# ==========================================================================================================
# PURE UNITS — no database, no network. These are the pieces the handler is assembled from, tested where
# they can actually be made to fail.
# ==========================================================================================================

def _entity(kind: str, value: str, normalized: str | None = None) -> ExtractedEntity:
    return ExtractedEntity(type=kind, value=value, normalized=normalized)


def test_model_entities_land_in_the_slots_their_type_names():
    """The model MAY propose slot values. The mapping is driven by the kind's own declared slot names, so
    a third product needs no line in the handler."""
    got = goal_handler.entity_slots(
        GoalKind.send_email,
        [_entity("recipient_email", TEACHER), _entity("subject", "thank you"),
         _entity("body_text", "thanks for the letter")])
    assert got == {"recipient": TEACHER, "subject": "thank you", "body": "thanks for the letter"}


def test_an_entity_type_that_names_no_slot_is_dropped():
    """Paired with the test above, so this cannot pass by the matcher simply never matching anything.
    A dropped value costs one question; a value forced into the wrong slot costs a letter to the wrong
    person."""
    assert goal_handler.entity_slots(GoalKind.send_email, [_entity("venue", "the gym")]) == {}
    assert goal_handler.entity_slots(GoalKind.send_email, [_entity("subject", "hi")]) == {"subject": "hi"}


def test_a_when_phrase_resolves_in_the_students_own_timezone():
    """Not the process's. A Central student's "tomorrow at 3pm" is not a Pacific student's, and a resolver
    that guessed a zone would place every event hours off with no visible error at all."""
    at = datetime.datetime(2026, 8, 1, 5, 30, tzinfo=datetime.timezone.utc)   # Aug 1 in CT, Jul 31 in PT
    incoming = {"start": SlotValue("tomorrow at 3pm", Source.user_stated, turn_index=2)}
    central = goal_handler.resolve_temporal(GoalKind.schedule_event, incoming,
                                            timezone_name="America/Chicago", now=at)
    pacific = goal_handler.resolve_temporal(GoalKind.schedule_event, incoming,
                                            timezone_name="America/Los_Angeles", now=at)
    assert central["start"].value.startswith("2026-08-02")
    assert pacific["start"].value.startswith("2026-08-01")


def test_the_end_moves_with_the_start_and_never_stays_stale():
    """`continuation.slot_patch` is replacement-shaped and can only name ONE slot, so a start patched on its
    own would leave the previous end beside it — an event that starts on Friday and ends on Tuesday."""
    at = datetime.datetime(2026, 8, 10, 16, 0, tzinfo=datetime.timezone.utc)
    known = {"start": SlotValue("2026-08-11T09:00:00", Source.user_stated, turn_index=1),
             "end": SlotValue("2026-08-11T10:00:00", Source.user_stated, turn_index=1)}
    patched = goal_handler.resolve_temporal(
        GoalKind.schedule_event, {"start": SlotValue("friday at 4pm", Source.user_stated, turn_index=4)},
        timezone_name="America/Chicago", now=at)
    merged = goal_slots.merge_slots(known, patched)
    assert merged["start"].value.startswith("2026-08-14T16:00")
    assert merged["end"].value.startswith("2026-08-14"), "the end was left on the old day"
    # positive control: an unparseable phrase changes NOTHING, so Bruce asks instead of inventing a moment.
    kept = goal_handler.resolve_temporal(
        GoalKind.schedule_event, {"start": SlotValue("whenever", Source.user_stated, turn_index=5)},
        timezone_name="America/Chicago", now=at)
    assert kept["start"].value == "whenever" and "end" not in kept


def test_a_real_operation_id_is_never_rewritten_into_the_open_goal():
    """Silently promoting a read into the open goal's send is a far worse failure than dropping a turn."""
    open_email_goal = {"id": str(uuid4()),
                       "goal": goal_slots.to_goal_jsonb({}, GoalKind.send_email, {})}
    named_read = _decision(IntentKind.actionable, caps=["gmail.get_message"])
    assert goal_handler.capability_for_turn(named_read, open_email_goal) == "gmail.get_message"
    # a turn that names NOTHING continues what is open — which is what a student means by "this".
    assert goal_handler.capability_for_turn(_decision(IntentKind.clarification), open_email_goal) == SEND
    assert goal_handler.capability_for_turn(_decision(IntentKind.clarification), None) == ""


def test_the_turn_index_keeps_rising_after_the_conversation_window_saturates():
    """`load_recent_turns` is a bounded window of 8, so the runtime's own position stops growing. If the
    index froze, a correction on turn 15 would TIE with the value it corrects (same trust, same index) and
    be settled by an arbitrary tie-break rather than by recency — which is the one thing this layer must
    never lose."""
    slots = {"recipient": SlotValue(TEACHER, Source.user_stated, turn_index=9)}
    octx = _octx(_decision(IntentKind.clarification),
                 open_goal={"id": str(uuid4()),
                            "goal": goal_slots.to_goal_jsonb({}, GoalKind.send_email, slots)})
    octx.turn_index = 9                                   # the window has saturated
    assert goal_handler.turn_index_for(octx) == 10
    # positive control: with nothing stored, the runtime's position is used as-is.
    assert goal_handler.turn_index_for(_octx(_decision(IntentKind.actionable, caps=[SEND]))) == 1


def test_the_exactly_once_key_is_derived_from_durable_state():
    """A key held in memory cannot survive a restart, and a send that cannot recognise its own retry is a
    second email."""
    run_id = str(uuid4())
    assert goal_handler.idempotency_key(run_id, SEND) == goal_handler.idempotency_key(run_id, SEND)
    assert goal_handler.idempotency_key(run_id, SEND) != goal_handler.idempotency_key(str(uuid4()), SEND)
    assert run_id in goal_handler.idempotency_key(run_id, SEND)


def test_a_capability_with_no_executor_is_declined_rather_than_half_collected(monkeypatch):
    """Collecting a title, a start and a timezone and only then admitting nothing can perform the call is
    the exchange this whole workstream exists to delete, so a slot-bearing capability with no
    CapabilityExecutor is refused up front.

    `calendar.create_event` used to be that capability — the row was missing and every calendar turn was
    declined (D3). Both declared kinds have an executor now, so the refusal is exercised by REMOVING the
    row: the mechanism is proven to still work, and the only reason it no longer fires in production is
    that the executor is genuinely there.
    """
    handler = goal_handler.GoalHandler()
    octx = _octx(_decision(IntentKind.actionable, caps=[CREATE_EVENT]))

    # THE FIX, asserted first: the capability that used to be refused is claimed.
    assert _run(handler.evaluate(octx)).disposition is conversation_outcomes.Disposition.claim

    monkeypatch.delitem(goal_handler._EXECUTORS, CREATE_EVENT)
    verdict = _run(handler.evaluate(octx))
    assert verdict.disposition is conversation_outcomes.Disposition.decline
    assert verdict.reason == goal_handler.NO_EXECUTOR
    # positive control: the same shape of turn on a capability that still HAS one is claimed.
    claimed = _run(handler.evaluate(_octx(_decision(IntentKind.actionable, caps=[SEND]))))
    assert claimed.disposition is conversation_outcomes.Disposition.claim


def test_the_kill_switch_turns_the_seam_off_without_a_deploy(monkeypatch):
    handler = goal_handler.GoalHandler()
    octx = _octx(_decision(IntentKind.actionable, caps=[SEND]))
    assert _run(handler.evaluate(octx)).disposition is conversation_outcomes.Disposition.claim
    monkeypatch.setenv("BRUCE_GOAL_SPINE_OFF", "1")
    off = _run(handler.evaluate(octx))
    assert off.disposition is conversation_outcomes.Disposition.decline
    assert off.reason == goal_handler.OFF


def test_a_background_mission_that_already_ran_owns_the_turn_alone():
    """Two lanes proposing the same send is how a student gets the same email twice."""
    handler = goal_handler.GoalHandler()
    octx = _octx(_decision(IntentKind.actionable, caps=[SEND]))
    octx.mission_lane_ran = True
    verdict = _run(handler.evaluate(octx))
    assert verdict.disposition is conversation_outcomes.Disposition.decline
    assert verdict.reason == goal_handler.MISSION_LANE


def test_decline_reasons_are_a_closed_vocabulary():
    """"How often did the goal seam decline, and for which reason" has to be countable. The transcript
    could not answer it because every refusal was a sentence in a reply rather than a value anywhere."""
    for reason in (goal_handler.OFF, goal_handler.MISSION_LANE, goal_handler.NO_EXECUTOR,
                   goal_handler.UNTOUCHED, goal_runtime.NOT_ACTIONABLE, goal_runtime.NO_CAPABILITY):
        assert reason in goal_handler.DECLINE_REASONS
    assert "because_it_felt_right" not in goal_handler.DECLINE_REASONS


# ==========================================================================================================
# THE LIVE PATH — real Postgres, real runtime, fake model + fake Gmail.
# ==========================================================================================================

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


def _decision(intent, *, rt=ResponseType.direct_answer, text="ok", caps=None, entities=None,
              needs_mission=False, proposed_goal=None):
    return ConversationDecision(
        intent=intent, response_type=rt, user_visible_response=text,
        extracted_entities=entities or [], required_capabilities=caps or [],
        needs_mission=needs_mission, proposed_goal=proposed_goal, risk_level=RiskLevel.none,
        confidence=0.9)


class _Msg:
    """The minimum an OutcomeContext handler reads off an inbound message, for the pure tests."""

    def __init__(self, text=None):
        self.text = text
        self.attachments = []
        self.channel_identity = PHONE
        self.reply_to_message_id = None


def _octx(decision, *, open_goal=None, continuation=None, text=None):
    return conversation_outcomes.OutcomeContext(
        user_id=uuid4(), decision=decision, capsule=object(), msg=_Msg(text), profile=object(),
        channel="self_hosted_imessage", pmid="p1", style=object(), store=object(),
        open_goal=open_goal, continuation=continuation, conversation_id=PHONE, turn_index=1)


class ScriptedReasoner:
    """A model that says exactly what the transcript's model said, turn by turn. Scripted rather than
    stubbed-constant, because the defect was per-turn: the SAME model emitted `needs_mission=false` and a
    free-text capability twenty-two times, and each answer was individually plausible."""

    provider = "fake"
    model = "fake"
    supports_vision = True

    def __init__(self, script: dict, default=None):
        self.script = script
        self.default = default or _decision(IntentKind.casual, text="ok")
        self.calls = 0

    async def decide(self, *, text, images, context):
        self.calls += 1
        decision = self.script.get((text or "").strip(), self.default)
        return ReasonResult(decision=decision, provider="fake", model="fake",
                            input_tokens=0, output_tokens=0, latency_ms=1)


async def _seed(uid, *, scopes=(CAL, GSEND, GREAD)):
    await users.ensure(uid, auth_provider="test")
    async with user_session(uid) as s:
        s.add(schema.Integration(
            user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
            scopes=list(scopes), refresh_token_encrypted=crypto.encrypt("rt"),
            selected_calendar_id="primary", status="connected"))


def _inbound(text, pmid):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=PHONE, text=text, attachments=[],
                          timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)


def _handlers(adapter):
    """The production pipeline with the FAKE provider injected into the one handler that reaches a
    provider. Everything else — selection, priority, the gate, the gateway — is the real thing."""
    return [goal_handler.GoalHandler(adapter=adapter) if h.name == "goal" else h
            for h in conversation_outcomes.default_handlers()]


class Conversation:
    """One student, one thread, N turns through the REAL inbound runtime."""

    def __init__(self, script, *, scopes=(CAL, GSEND, GREAD)):
        self.uid = uuid4()
        _run(_seed(self.uid, scopes=scopes))
        self.adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
        self.reasoner = ScriptedReasoner(script)
        self.channel = FakeChannel()
        self.n = 0
        self.replies: list[str] = []

    def say(self, text: str) -> str:
        self.n += 1
        out = _run(conversation_runtime.handle(
            self.channel, _inbound(text, f"m{self.n}"), user_id=self.uid, reply_target=PHONE,
            reasoner=self.reasoner, handlers=_handlers(self.adapter)))
        assert out.status == "processed", out.status
        reply = _run(self._last_reply())
        self.replies.append(reply)
        return reply

    async def _last_reply(self) -> str:
        async with user_session(self.uid) as s:
            rows = (await s.execute(
                select(schema.ConversationTurn)
                .where(schema.ConversationTurn.user_id == self.uid,
                       schema.ConversationTurn.role == "assistant")
                .order_by(schema.ConversationTurn.created_at.desc()).limit(1))).scalars().all()
        return rows[0].text if rows else ""

    def goals(self) -> list[dict]:
        """Every run that carries a typed goal, whatever its status — the rows the transcript never had."""
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid))).scalars().all()
            out = []
            for r in rows:
                kind, slots = goal_slots.from_goal_jsonb(r.goal if isinstance(r.goal, dict) else None)
                if kind is not None:
                    out.append({"id": str(r.id), "kind": kind, "status": r.status, "slots": slots,
                                "decision": r.active_decision})
            return out
        return _run(_read())

    def runs(self) -> int:
        async def _count():
            async with user_session(self.uid) as s:
                return len((await s.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid))).scalars().all())
        return _run(_count())


# The transcript, as a script. Turn 1 is the ask; turns 2-4 are the student answering Bruce's own
# questions (intent=clarification, naming no capability at all — the shape that used to be dropped);
# turn 5 is the yes. `needs_mission` is False on EVERY turn, exactly as it was live.
ASK = "can u email ms alvarez a thank you note"
ADDRESS = TEACHER
SUBJECT = "subject should be thank you"
BODY = "tell her thanks for the recommendation letter"
YES = "yes send it"

SCRIPT = {
    ASK: _decision(IntentKind.actionable, caps=[SEND], needs_mission=False,
                   proposed_goal="email ms alvarez a thank-you note",
                   text="sure, who should it go to?"),
    ADDRESS: _decision(IntentKind.clarification, text="got it"),
    SUBJECT: _decision(IntentKind.clarification, text="ok"),
    BODY: _decision(IntentKind.clarification, text="ok"),
    YES: _decision(IntentKind.approval, text="ok"),
}


def test_one_goal_across_the_whole_exchange_and_the_recipient_is_never_asked_for_twice(clean_db):
    """THE TRANSCRIPT, INVERTED. Five turns, ONE run, and the address given on turn 2 is still on the run
    at turn 5 — where twenty-two turns produced zero runs and a second request for a recipient."""
    c = Conversation(SCRIPT)

    c.say(ASK)
    goals = c.goals()
    assert len(goals) == 1, "the ask produced no durable goal at all"
    run_id = goals[0]["id"]
    assert goals[0]["kind"] is GoalKind.send_email

    c.say(ADDRESS)
    after_address = c.goals()
    assert len(after_address) == 1 and after_address[0]["id"] == run_id, "a second run was opened"
    assert after_address[0]["slots"]["recipient"].value == TEACHER
    assert after_address[0]["slots"]["recipient"].source is Source.user_stated

    reply = c.say(SUBJECT)
    assert "who" not in reply.lower(), f"asked for the recipient it already had: {reply!r}"

    c.say(BODY)
    final = c.goals()
    assert len(final) == 1 and final[0]["id"] == run_id
    assert final[0]["slots"]["recipient"].value == TEACHER, "the turn-2 address was lost"
    assert final[0]["slots"]["subject"].filled and final[0]["slots"]["body"].filled
    assert goal_slots.missing_required(GoalKind.send_email, final[0]["slots"]) == ()


def test_zero_provider_calls_before_the_student_confirms(clean_db):
    """A write that happens before the yes cannot be taken back by apologising for it."""
    c = Conversation(SCRIPT)
    for turn in (ASK, ADDRESS, SUBJECT, BODY):
        c.say(turn)
    assert c.adapter.send_calls == 0, "something reached Gmail before the student said yes"
    goal = c.goals()[0]
    assert goal["status"] == "awaiting_approval"
    assert (goal["decision"] or {}).get("status") == goal_handler.PENDING
    assert (goal["decision"] or {}).get("arguments_fingerprint"), "consent was not bound to the arguments"
    assert TEACHER in c.replies[-1], "the proposal did not show the student what would be sent"


def test_exactly_one_provider_call_after_the_confirmation_and_a_repeat_adds_none(clean_db):
    """The yes sends once. Saying it again is a no-op — not a second email, and not a second run."""
    c = Conversation(SCRIPT)
    for turn in (ASK, ADDRESS, SUBJECT, BODY):
        c.say(turn)

    receipt = c.say(YES)
    assert c.adapter.send_calls == 1 and len(c.adapter.messages) == 1
    assert TEACHER in receipt and "✅" in receipt, f"no honest receipt after a verified send: {receipt!r}"

    goals = c.goals()
    assert len(goals) == 1, "the send opened a second goal"
    assert goals[0]["status"] == "completed", "the goal never closed on a verified send"
    # TWO rows exist after a send and that is deliberate: the GOAL run is the conversation's durable memory
    # of the task, and `agent_loop.run_direct_action` keeps its own audit run for the one provider call.
    # Only the first carries slots, so only the first is a goal, and both are terminal.
    runs_after_send = c.runs()
    assert runs_after_send == 2

    again = c.say(YES)
    assert c.adapter.send_calls == 1, "a repeated confirmation sent a second email"
    assert len(c.adapter.messages) == 1
    assert c.runs() == runs_after_send, "a repeated confirmation opened new durable state"
    assert "✅" not in again, "a repeat claimed a completion that did not happen this turn"


def test_two_confirmations_arriving_at_once_still_send_exactly_once(clean_db):
    """Idempotency has to survive a RACE, not just a repeat. Two "yes"es landing together both read an
    awaiting_approval run, both mint evidence and both reach the executor — and the exactly-once key,
    derived from the run rather than from anything in memory, collapses them at the adapter's marker
    ledger to the one message that was actually sent."""
    c = Conversation(SCRIPT)
    for turn in (ASK, ADDRESS, SUBJECT, BODY):
        c.say(turn)

    async def _both():
        await asyncio.gather(
            conversation_runtime.handle(c.channel, _inbound(YES, "yes-a"), user_id=c.uid,
                                        reply_target=PHONE, reasoner=c.reasoner,
                                        handlers=_handlers(c.adapter)),
            conversation_runtime.handle(c.channel, _inbound(YES, "yes-b"), user_id=c.uid,
                                        reply_target=PHONE, reasoner=c.reasoner,
                                        handlers=_handlers(c.adapter)))

    _run(_both())
    assert len(c.adapter.messages) == 1, "a concurrent confirmation produced a second email"


def test_the_verified_send_actually_went_to_the_address_the_student_gave(clean_db):
    """The read-back is what makes a receipt honest, so the assertion is against what the provider holds —
    not against what Bruce said it did."""
    c = Conversation(SCRIPT)
    for turn in (ASK, ADDRESS, SUBJECT, BODY, YES):
        c.say(turn)
    sent = list(c.adapter.messages.values())
    assert len(sent) == 1
    headers = {h["name"]: h["value"] for h in sent[0]["payload"]["headers"]}
    assert headers["To"] == TEACHER
    assert "SENT" in sent[0]["labelIds"]


def test_needs_mission_false_still_creates_a_goal(clean_db):
    """The exact field that said "no" twenty-two times. The backend decides; it is never read."""
    c = Conversation({ASK: _decision(IntentKind.actionable, caps=[SEND], needs_mission=False,
                                     text="sure")})
    c.say(ASK)
    goals = c.goals()
    assert len(goals) == 1 and goals[0]["kind"] is GoalKind.send_email


def test_an_ordinary_conversational_turn_creates_no_goal(clean_db):
    """Paired with the test above so it cannot pass by the handler simply never creating anything. A
    hallucinated mission flag on a chat turn opens no durable state either."""
    chat = "whats 8 times 7"
    c = Conversation({chat: _decision(IntentKind.casual, text="56", needs_mission=True,
                                      proposed_goal="do my homework")})
    c.say(chat)
    assert c.goals() == []
    assert c.runs() == 0


def test_an_unconnected_account_is_named_before_anything_is_collected(clean_db):
    """`next_step` checks availability BEFORE missing slots on purpose: collecting a recipient and a
    subject and a body and only then admitting the account is not connected is the exchange that made
    Bruce feel broken. And the reply names the missing SCOPE — it never denies the ability."""
    c = Conversation(SCRIPT, scopes=(CAL,))          # connected for calendar, never granted gmail.send
    reply = c.say(ASK)
    assert c.adapter.send_calls == 0
    assert "permission" in reply.lower() and "gmail" in reply.lower()
    assert "i can't" not in reply.lower() and "i cant" not in reply.lower()
    goal = c.goals()[0]
    assert goal["status"] == "blocked"


def test_a_refusal_cancels_the_goal_and_sends_nothing(clean_db):
    """A refusal dominates everything, at any position in the message — and it must close the run rather
    than leave a confirmed-looking decision parked where a later "ok" could land on it."""
    stop = "actually nah dont send it"
    script = {**SCRIPT, stop: _decision(IntentKind.status_cancel_correction, text="ok")}
    c = Conversation(script)
    for turn in (ASK, ADDRESS, SUBJECT, BODY):
        c.say(turn)
    c.say(stop)
    assert c.adapter.send_calls == 0
    assert c.goals()[0]["status"] == "cancelled"


def test_an_amendment_re_proposes_instead_of_sending_the_old_draft(clean_db):
    """"send it to <other address>" reads as an approval to a yes/no resolver, because it contains "send
    it". Reading it as a yes would send the OLD draft to the OLD address, so an amendment is resolved
    first and the consent it looked like is never spent."""
    other = "coach@school.edu"
    change = f"send it to {other} instead"
    script = {**SCRIPT, change: _decision(IntentKind.clarification, text="ok")}
    c = Conversation(script)
    for turn in (ASK, ADDRESS, SUBJECT, BODY):
        c.say(turn)
    before = (c.goals()[0]["decision"] or {}).get("arguments_fingerprint")

    c.say(change)
    assert c.adapter.send_calls == 0, "an amendment was spent as a confirmation"
    goal = c.goals()[0]
    assert goal["slots"]["recipient"].value == other, "the correction was not applied"
    assert (goal["decision"] or {}).get("arguments_fingerprint") != before, \
        "the pending decision still points at the arguments the student just changed"

    c.say(YES)
    assert c.adapter.send_calls == 1
    headers = {h["name"]: h["value"] for h in list(c.adapter.messages.values())[0]["payload"]["headers"]}
    assert headers["To"] == other, "the yes sent the pre-correction draft"


def test_memory_is_offered_the_outcome_only_after_the_read_back(clean_db, monkeypatch):
    """`memory_finalize.after_verified_outcome` finally has a caller, and it is reachable ONLY from a path
    that already holds a verification result. "I emailed my teacher" is durable and true; "I am about to"
    is a slot, and slots stay in the goal where they can still be corrected.

    Asserted at the CALL rather than at the row on purpose. `after_verified_outcome` currently refuses
    every proposal it is handed — it builds `subject="self"` with `kind=episodic`, which
    `memory_writer.assess` rejects as NOT_USER_SPECIFIC, and `predicate="completed_action"` is not the
    namespaced `domain.relation` the same function requires. That is a defect inside `memory_finalize`,
    not in this seam, and asserting on the row would make this test pass or fail for reasons that have
    nothing to do with whether the completion path calls it.
    """
    from bruce_engine import memory_finalize
    calls: list[dict] = []
    real = memory_finalize.after_verified_outcome

    async def _spy(user_id, **kw):
        calls.append(kw)
        return await real(user_id, **kw)

    monkeypatch.setattr(memory_finalize, "after_verified_outcome", _spy)

    c = Conversation(SCRIPT)
    for turn in (ASK, ADDRESS, SUBJECT, BODY):
        c.say(turn)
    assert calls == [], "an outcome was remembered before anything was executed"

    c.say(YES)
    assert len(calls) == 1, "a verified send offered memory nothing"
    assert calls[0]["capability"] == SEND
    assert calls[0]["provider_entity_id"], "the receipt carried no provider entity id"
    assert TEACHER in calls[0]["summary"]


def test_the_router_is_skipped_only_when_the_turn_lands_on_work_in_flight(clean_db):
    """Routing asks "what kind of request is this text"; a continuation is answered by STATE. Skipping is
    the safety property, not an optimisation: classifying "yeah send it" would re-enter whatever lane the
    original request took, background send mission included."""
    c = Conversation(SCRIPT)
    first = _run(conversation_runtime.handle(
        c.channel, _inbound(ASK, "r1"), user_id=c.uid, reply_target=PHONE,
        reasoner=c.reasoner, handlers=_handlers(c.adapter)))
    assert first.execution_class is not None, "the router did not run for a fresh request"

    c.n = 1
    c.say(ADDRESS)
    second = _run(conversation_runtime.handle(
        c.channel, _inbound(SUBJECT, "r3"), user_id=c.uid, reply_target=PHONE,
        reasoner=c.reasoner, handlers=_handlers(c.adapter)))
    assert second.execution_class is None, "a continuation was still routed as a fresh request"
