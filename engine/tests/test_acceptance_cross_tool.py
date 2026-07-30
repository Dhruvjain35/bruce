"""ACCEPTANCE — the wired brain spine, driven end to end, across two tools, against the real transcript.

WHAT THIS FILE IS FOR. Layers 1-6 of the spine (`goal_slots`, `transitions`, `turn_context`,
`continuation`, `goal_runtime`, `memory_finalize`) were built and tested green while nothing called them.
`goal_handler` is the call, and `conversation_runtime` now resolves a continuation BEFORE it routes. A
green unit suite over a wired seam still does not say whether a STUDENT gets a different outcome, so every
test below drives `conversation_runtime.handle` — real Postgres, real router, real continuation, real goal
runtime, real execution gate, real MutationGateway — and asserts on what the PROVIDER holds afterwards.
The only substitutions are the model (a scripted reasoner) and Google (the in-memory adapters that already
enforce Google's own send/read-back/409 rules). Nothing here touches the network.

THE FAILURE BEING MEASURED, verified in the database and not a hypothetical. A student asked Bruce to
email one named address a thank-you note. Turn 2 asked for the recipient turn 1 supplied. An inline reply
of "this" resolved nothing. The last of twenty-two turns said "i can't send messages for you" while
`tool_broker` was answering ok=True for `gmail.send_message` in the same process. Twenty-two turns, ZERO
missions, ZERO agent_runs.

WHAT IS PROVEN GREEN. On a five-turn exchange whose wording avoids the defects below, the spine does the
entire job the transcript failed at: ONE goal across five turns, the recipient and purpose from turn 1
still on the run at turn 5, the tone amended mid-flight, no question ever repeated for a slot already
filled, exactly ONE pending Decision, ZERO Gmail calls before the confirmation, exactly ONE send after it,
exactly one fetch-back, a terminal verified run — and two confirmations arriving at the same instant still
send once. A named calendar move writes the provider once, keeps the title, moves start and end together,
and refuses a stale "yes". A refusal closes the goal and every outstanding authorization, across tools.

HOW TO READ A RED TEST HERE. The rest of this file FAILS, and it is supposed to be able to: those tests
assert the outcome the student is owed, not the outcome the code currently produces. Each one is paired
with a positive control asserted FIRST, so a failure says "the machinery works, and this specific input
defeats it" rather than "something is broken somewhere". The named defects are:

  * D1 — a negation is matched anywhere in the message and always read as refusing THE OPERATION, so
    "...and send it dont show me draft" cancels the send, and "YES WRITE IT AND SEND IT NO MORE QUESTIONS"
    (the founder's last turn) cancels it too AND, through `user_action_boundary`, invalidates every
    outstanding authorization the student has in every conversation. This is a SCOPE defect, not a
    precedence one: refusal must keep dominating, and `continuation`'s resolution order must not move.
  * D2 — `continuation._STYLE` only sees a tone word that directly follows "make it", so "make it a
    professional email" changes no tone while "make it professional" does.
  * D3 — `calendar.create_event` has a declared slot set and no CapabilityExecutor, so the goal seam
    declines every calendar turn and a calendar goal never exists. Everything the calendar half of this
    program is supposed to prove (one goal id across a move, a replacement Decision, attendees retained)
    has nothing live to be proven against, and is proven here against `goal_runtime` directly instead.
  * D4 — `conversation_runtime._open_goal_row` picks the NEWEST typed run and nothing else, so with two
    goals open the older one is invisible: an email turn that names no capability is read as a calendar
    turn, "send it" cannot reach an email Decision underneath a newer calendar goal, and an inline reply
    pointing at the older goal does not change the choice.
  * D5 — `goal_handler.resolve_temporal` writes BOTH halves of a resolved phrase over the goal, so a
    date-only amendment ("move it to friday") replaces a 4-5pm slot with an all-day block and loses the
    hour the student had already set. `calendar_mutation.recompute` is the reference implementation: it
    keeps the base time and the original duration when only the date moves.

Nothing in this file logs message content, and no assertion is made against a log line.
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
from bruce_engine import (calendar_adapter, conversation_graph, conversation_outcomes,
                          conversation_runtime, crypto, entity_store, gmail_adapter, goal_handler,
                          goal_runtime, goal_slots, oauth_google, schema, world_state)
from bruce_engine import continuation as continuation_mod
from bruce_engine.conversation_contract import (ConversationDecision, ExtractedEntity, IntentKind,
                                                ResponseType, RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.goal_slots import GoalKind, SlotValue, Source
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.models import CalendarEvent
from bruce_engine.repositories import PostgresUserRepository

PHONE = "+15550142"
ACCOUNT = "student@example.com"
TEACHER = "alvarez@school.edu"
CAL = "https://www.googleapis.com/auth/calendar.events"
GSEND = "https://www.googleapis.com/auth/gmail.send"
GREAD = "https://www.googleapis.com/auth/gmail.readonly"
SEND = "gmail.send_message"
CREATE_EVENT = "calendar.create_event"
UPDATE_EVENT = "calendar.update_event"
TZ = "America/Chicago"

users = PostgresUserRepository()


def _run(c):
    return asyncio.run(c)


# ==========================================================================================================
# HARNESS — one student, one thread, N turns through the REAL inbound runtime.
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


class CountingGmail(gmail_adapter.FakeGmailAdapter):
    """The in-memory Gmail, plus a count of READ-BACKS. "one fetch-back verification" is an assertion about
    how many times Bruce went and looked, and the base fake only counts sends."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.get_calls = 0

    async def get(self, message_id: str):
        self.get_calls += 1
        return await super().get(message_id)


class CountingCalendar(calendar_adapter.FakeCalendarAdapter):
    """The in-memory calendar, plus counts of the two calls this suite reasons about: how many times the
    provider was told to change the event, and how many times the change was read back."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.update_calls = 0
        self.get_calls = 0

    async def update(self, *a, **kw):
        self.update_calls += 1
        return await super().update(*a, **kw)

    async def get(self, event_id: str):
        self.get_calls += 1
        return await super().get(event_id)


def _decision(intent, *, rt=ResponseType.direct_answer, text="ok", caps=None, entities=None,
              needs_mission=False, proposed_goal=None):
    return ConversationDecision(
        intent=intent, response_type=rt, user_visible_response=text,
        extracted_entities=entities or [], required_capabilities=caps or [],
        needs_mission=needs_mission, proposed_goal=proposed_goal, risk_level=RiskLevel.none,
        confidence=0.9)


def _entity(kind: str, value: str, normalized: str | None = None) -> ExtractedEntity:
    return ExtractedEntity(type=kind, value=value, normalized=normalized)


class ScriptedReasoner:
    """A model that answers each turn the way the transcript's model did. Scripted rather than constant,
    because the defect was per-turn: the same model emitted `needs_mission=false` and a free-text
    capability twenty-two times and each answer was individually plausible."""

    provider = "fake"
    model = "fake"
    supports_vision = True

    def __init__(self, script: dict, default=None):
        self.script = script
        self.default = default or _decision(IntentKind.clarification, text="ok")
        self.calls = 0

    async def decide(self, *, text, images, context):
        self.calls += 1
        return ReasonResult(decision=self.script.get((text or "").strip(), self.default),
                            provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)


async def _seed(uid):
    await users.ensure(uid, auth_provider="test")
    async with user_session(uid) as s:
        s.add(schema.Integration(
            user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
            scopes=[CAL, GSEND, GREAD], refresh_token_encrypted=crypto.encrypt("rt"),
            selected_calendar_id="primary", status="connected"))
    await world_state.set_timezone(uid, TZ, source="user_stated")


class Conversation:
    """One student, one thread. `say` is a complete inbound turn: the runtime, the router, the handler
    pipeline, the gate and the provider."""

    def __init__(self, script):
        self.uid = uuid4()
        _run(_seed(self.uid))
        self.gmail = CountingGmail(account=ACCOUNT)
        self.reasoner = ScriptedReasoner(script)
        self.channel = FakeChannel()
        self.n = 0
        # Message ids are unique per CONVERSATION, not per turn number: `outbound_messages` keys its
        # idempotency on the inbound id alone, so two students in one test replaying "m1" collide in the
        # database rather than in the code under test.
        self.tag = uuid4().hex[:8]

    # --- driving ------------------------------------------------------------------------------------------

    def _handlers(self):
        """The production pipeline with the FAKE provider injected into the one handler that reaches a
        provider. Selection, priority, the gate and the gateway are the real thing."""
        return [goal_handler.GoalHandler(adapter=self.gmail) if h.name == "goal" else h
                for h in conversation_outcomes.default_handlers()]

    def _inbound(self, text, pmid, *, reply_to=None):
        return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                              channel_identity=PHONE, text=text, attachments=[],
                              timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False,
                              reply_to_message_id=reply_to)

    def mid(self, turn: int) -> str:
        """The provider message id of one of this conversation's turns — what an inline reply points at."""
        return f"{self.tag}-m{turn}"

    def say(self, text: str, *, reply_to: str | None = None) -> str:
        self.n += 1
        msg = self._inbound(text, self.mid(self.n), reply_to=reply_to)
        # The message graph is upserted by `messaging_inbound` BEFORE it dispatches to the runtime, and the
        # runtime's authoritative-target rule reads it: without the node, an inline reply is an unresolvable
        # target and the turn fails closed before any handler runs. Doing it here keeps the harness at the
        # same starting state the production caller hands over.
        msg.user_id = self.uid
        _run(conversation_graph.ingest_inbound_message(msg))
        out = _run(conversation_runtime.handle(
            self.channel, msg, user_id=self.uid,
            reply_target=PHONE, reasoner=self.reasoner, handlers=self._handlers()))
        assert out.status == "processed", out.status
        return _run(self._last_reply())

    def say_twice_at_once(self, text: str) -> None:
        """The SAME turn arriving twice concurrently — a webhook redelivery, a double tap. Both reads see
        an awaiting_approval run, both mint evidence, both reach the executor."""
        async def _both():
            await asyncio.gather(
                conversation_runtime.handle(self.channel, self._inbound(text, f"{self.tag}-race-a"),
                                            user_id=self.uid,
                                            reply_target=PHONE, reasoner=self.reasoner,
                                            handlers=self._handlers()),
                conversation_runtime.handle(self.channel, self._inbound(text, f"{self.tag}-race-b"),
                                            user_id=self.uid,
                                            reply_target=PHONE, reasoner=self.reasoner,
                                            handlers=self._handlers()))
        _run(_both())

    # --- reading what actually happened ---------------------------------------------------------------------

    async def _last_reply(self) -> str:
        async with user_session(self.uid) as s:
            rows = (await s.execute(
                select(schema.ConversationTurn)
                .where(schema.ConversationTurn.user_id == self.uid,
                       schema.ConversationTurn.role == "assistant")
                .order_by(schema.ConversationTurn.created_at.desc()).limit(1))).scalars().all()
        return rows[0].text if rows else ""

    def goals(self) -> list[dict]:
        """Every run carrying a TYPED goal, whatever its status — the rows the transcript never had."""
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid)
                    .order_by(schema.AgentRun.created_at))).scalars().all()
            out = []
            for r in rows:
                kind, slots = goal_slots.from_goal_jsonb(r.goal if isinstance(r.goal, dict) else None)
                if kind is not None:
                    out.append({"id": str(r.id), "kind": kind, "status": r.status, "slots": slots,
                                "decision": r.active_decision})
            return out
        return _run(_read())

    def goal_of(self, kind: GoalKind) -> dict | None:
        return next((g for g in self.goals() if g["kind"] is kind), None)

    def authorizations(self) -> list[dict]:
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AuthorizationEvidenceRow).where(
                    schema.AuthorizationEvidenceRow.user_id == self.uid)
                    .order_by(schema.AuthorizationEvidenceRow.created_at))).scalars().all()
            return [{"id": str(r.authorization_id), "operation": r.operation,
                     "invalidated": r.invalidated_at is not None,
                     "consumed": r.consumed_at is not None} for r in rows]
        return _run(_read())

    def decision_ids(self) -> list[str]:
        """Every distinct decision this student was ever asked to answer. "exactly one pending Decision"
        is a claim about the canonical block on the run, not about how many times Bruce spoke."""
        seen = []
        for g in self.goals():
            block = g["decision"] or {}
            did = block.get("decision_id")
            if did and did not in seen:
                seen.append(did)
        return seen


def _sent_headers(gmail: CountingGmail) -> dict:
    sent = [m for m in gmail.messages.values() if "SENT" in m["labelIds"]]
    assert len(sent) == 1, f"expected exactly one sent message, found {len(sent)}"
    return {h["name"]: h["value"] for h in sent[0]["payload"]["headers"]}


# ==========================================================================================================
# THE FOUNDER'S EMAIL SEQUENCE — the five turns, in order.
# ==========================================================================================================
# Turn 1 hands over the recipient, the purpose and a tone. Turns 2 and 3 are the student pushing to skip
# the draft. Turn 4 is the inline reply of "this". Turn 5 is the all-caps demand. `needs_mission` is False
# on every turn, exactly as it was live, and no turn after the first names a capability at all — the shape
# that used to be dropped.

T1 = "can u email alvarez@school.edu a thank you note for the rec letter, make it heartfelt"
T2 = "make it a professional email and send it dont show me draft"
T3 = "js make it whatever u think is appropriate and send it alr"
T4 = "this"
T5 = "YES WRITE IT AND SEND IT NO MORE QUESTIONS"

# The two turns whose only difference is where the negation sits. Same intent, same request, same student.
T2_CLEAN = "make it professional and send it"
T5_CLEAN = "YES WRITE IT AND SEND IT"

_ASK = _decision(IntentKind.actionable, caps=[SEND], needs_mission=False,
                 proposed_goal="email ms alvarez a thank-you note",
                 text="sure, what should the subject line be?",
                 entities=[_entity("recipient_email", TEACHER),
                           _entity("purpose", "thank her for the recommendation letter"),
                           _entity("tone", "heartfelt")])
# The model DRAFTS on turn 3, because the student told it to. Both values are `model_derived`, which is
# what makes `guessed_required` insist on a confirmation before an irreversible send.
_DRAFTS = _decision(IntentKind.clarification, text="ok, here's what i've got",
                    entities=[_entity("subject", "thank you"),
                              _entity("body_text", "thank you so much for writing my recommendation "
                                                   "letter. it meant a lot.")])

SCRIPT = {
    T1: _ASK,
    T2: _decision(IntentKind.clarification, text="ok"),
    T2_CLEAN: _decision(IntentKind.clarification, text="ok"),
    T3: _DRAFTS,
    T4: _decision(IntentKind.clarification, text="ok"),
    T5: _decision(IntentKind.approval, text="ok"),
    T5_CLEAN: _decision(IntentKind.approval, text="ok"),
}


def test_the_exact_founder_sequence_ends_in_one_verified_send(clean_db):
    """THE ACCEPTANCE TEST. The five turns that produced nothing, replayed verbatim against the wired
    spine, asserting the outcome the student was owed: one goal, one run, the recipient and purpose kept,
    the tone changed to professional, one pending Decision, nothing sent before the yes and exactly one
    send after it.

    THIS TEST FAILS TODAY, at turn 2, and the reason is worth stating precisely rather than routing
    around. `decision_resolver._NEG` matches a negation ANYWHERE in the message and `continuation.resolve`
    gives a refusal absolute precedence — correctly, for "actually nah dont add it". But the scope of the
    negation is never established, so "send it dont show me draft" reads as a refusal OF THE SEND and
    `goal_handler._cancel` closes the run. Turn 5 does the same thing for the same reason: "NO MORE
    QUESTIONS" contains "no". The student's most emphatic instruction to proceed is the one Bruce reads as
    a stop.

    The paired control below (`..._without_the_scoped_negations_...`) runs the identical five turns with
    those two phrases replaced by ones that carry no negation, and it passes. So the spine does the whole
    job; the refusal SCOPE is the defect, and it lives in `decision_resolver`/`continuation`, not here.
    """
    c = Conversation(SCRIPT)

    c.say(T1)
    goals = c.goals()
    assert len(goals) == 1, "the ask produced no durable goal at all — the original failure"
    run_id = goals[0]["id"]
    assert goals[0]["kind"] is GoalKind.send_email
    assert goals[0]["slots"]["recipient"].value == TEACHER
    assert goals[0]["slots"]["purpose"].filled

    c.say(T2)
    after = c.goals()
    assert len(after) == 1 and after[0]["id"] == run_id, "a second run was opened"
    assert after[0]["status"] != "cancelled", (
        "turn 2 asked for a professional email and to skip the draft; Bruce read the 'dont' in "
        "'dont show me draft' as a refusal of the send and cancelled the goal")
    assert after[0]["slots"]["tone"].value == "professional", "the tone amendment was dropped"

    c.say(T3)
    c.say(T4, reply_to=c.mid(3))
    assert c.gmail.send_calls == 0, "something reached Gmail before the student confirmed"
    assert len(c.decision_ids()) == 1, "the student was asked to answer more than one Decision"

    c.say(T5)
    assert c.gmail.send_calls == 1, "the founder's last turn did not send the email"
    assert c.goals()[0]["id"] == run_id, "the send happened on a different run than the ask"
    assert _sent_headers(c.gmail)["To"] == TEACHER


def test_the_same_five_turns_without_the_scoped_negations_send_exactly_once(clean_db):
    """THE POSITIVE CONTROL, and the real proof that the spine is live. Identical five turns, identical
    script, with the two phrases whose negations govern something other than the operation swapped for
    equivalents that carry none: "make it professional and send it", "YES WRITE IT AND SEND IT".

    Everything the acceptance criteria ask for is asserted here: ONE goal, the SAME run across all five
    turns, the recipient from turn 1 and the purpose from turn 1 still present at turn 5, the tone changed
    to professional, no repeated question for anything already given, exactly ONE pending Decision, ZERO
    Gmail calls before the confirmation, exactly ONE send after it, exactly one fetch-back, and a terminal
    verified run.
    """
    c = Conversation(SCRIPT)

    c.say(T1)
    run_id = c.goals()[0]["id"]

    reply2 = c.say(T2_CLEAN)
    goal = c.goals()[0]
    assert len(c.goals()) == 1 and goal["id"] == run_id, "the amendment opened a second run"
    assert goal["status"] != "cancelled"
    assert goal["slots"]["tone"].value == "professional", "the tone amendment never reached the goal"
    assert goal["slots"]["tone"].source is Source.user_stated
    assert goal["slots"]["recipient"].value == TEACHER, "the turn-1 address was lost"
    # THE TRANSCRIPT'S SECOND TURN, INVERTED. The question is generated from `missing_required`, so it
    # cannot name a slot that is already filled.
    assert "who this should go to" not in reply2.lower(), f"asked again for the recipient: {reply2!r}"
    assert "what it's for" not in reply2.lower(), f"asked again for the purpose: {reply2!r}"

    reply3 = c.say(T3)
    assert c.gmail.send_calls == 0, "the draft turn reached Gmail before any confirmation"
    proposed = c.goals()[0]
    assert proposed["status"] == "awaiting_approval"
    assert (proposed["decision"] or {}).get("status") == goal_handler.PENDING
    assert (proposed["decision"] or {}).get("arguments_fingerprint"), \
        "consent was not bound to the arguments the student was shown"
    assert TEACHER in reply3, "the proposal did not show the student where it was going"

    # Turn 4: the inline reply carrying nothing but "this" — the pointer that resolved to nothing live.
    only_decision = c.decision_ids()[0]
    reply4 = c.say(T4, reply_to=c.mid(3))
    assert c.gmail.send_calls == 0, "a bare pointer was spent as a confirmation"
    assert c.decision_ids() == [only_decision], "the pointer minted a second Decision"
    assert (c.goals()[0]["decision"] or {}).get("status") == goal_handler.PENDING
    assert TEACHER in reply4, f"the pointer resolved to nothing, exactly as it did live: {reply4!r}"
    assert "who this should go to" not in reply4.lower()

    receipt = c.say(T5_CLEAN)
    assert c.gmail.send_calls == 1, "the confirmation did not send"
    assert c.gmail.get_calls == 1, f"expected exactly one fetch-back, got {c.gmail.get_calls}"
    assert "✅" in receipt and TEACHER in receipt, f"no honest receipt after a verified send: {receipt!r}"

    goals = c.goals()
    assert len(goals) == 1 and goals[0]["id"] == run_id, "five turns, more than one goal"
    # `completed` and not `succeeded`: `succeeded` is reachable only from `verifying` with an independent
    # read-back, and the direct-action lane's verified terminal is `completed` (`agent_loop._status_for`).
    # What acceptance actually requires is that the run is CLOSED and closed on a proof, and both hold.
    assert goals[0]["status"] == "completed", "the goal never closed on a verified send"
    assert goals[0]["status"] in goal_runtime._CLOSED_STATUSES
    assert goals[0]["slots"]["recipient"].value == TEACHER
    assert goals[0]["slots"]["purpose"].filled, "the purpose given on turn 1 did not survive to the send"
    assert goals[0]["slots"]["tone"].value == "professional"

    headers = _sent_headers(c.gmail)
    assert headers["To"] == TEACHER
    assert headers["Subject"] == "thank you"


def test_a_second_confirmation_arriving_at_the_same_instant_still_sends_once(clean_db):
    """Idempotency has to survive a RACE, not just a repeat. Two confirmations landing together both read
    an awaiting_approval run, both mint evidence and both reach the executor; the exactly-once key is
    derived from the RUN rather than from anything held in memory, so the adapter's marker ledger
    collapses them to the one message that was actually sent."""
    c = Conversation(SCRIPT)
    for turn in (T1, T2_CLEAN, T3):
        c.say(turn)
    assert c.gmail.send_calls == 0

    c.say_twice_at_once(T5_CLEAN)
    assert len(c.gmail.messages) == 1, "a concurrent confirmation produced a second email"
    assert len([g for g in c.goals()]) == 1, "a concurrent confirmation opened a second goal"


def test_changing_the_arguments_replaces_the_decision_rather_than_spending_it(clean_db):
    """A change made AFTER the offer produces a REPLACEMENT Decision, and the yes that follows is bound to
    what the student was last shown.

    "send it to coach@school.edu instead" contains "send it", so a yes/no resolver reads it as an approval;
    reading it that way would mail the old draft to the old address. The amendment wins, the arguments
    move, and the Decision is not merely re-fingerprinted — it is a NEW decision id, because the question
    the student is being asked is a different question.

    There is nothing to invalidate on the authorization side here: consent is minted at execution, so an
    offer that was never accepted leaves no authorization behind. The invalidation half of that criterion
    is proven where a real authorization exists — see the refusal test at the end of this file.
    """
    other = "coach@school.edu"
    change = f"send it to {other} instead"
    c = Conversation({**SCRIPT, change: _decision(IntentKind.clarification, text="ok")})
    for turn in (T1, T2_CLEAN, T3):
        c.say(turn)
    offered = c.goals()[0]["decision"] or {}
    assert offered.get("status") == goal_handler.PENDING and offered.get("arguments_fingerprint")

    c.say(change)
    assert c.gmail.send_calls == 0, "an amendment was spent as the confirmation it looks like"
    replaced = c.goals()[0]["decision"] or {}
    assert c.goals()[0]["slots"]["recipient"].value == other, "the correction never reached the goal"
    assert replaced.get("arguments_fingerprint") != offered.get("arguments_fingerprint"), \
        "the pending Decision still points at the arguments the student just changed"
    assert replaced.get("decision_id") != offered.get("decision_id"), \
        "a different question was asked under the old Decision's id"
    assert replaced.get("status") == goal_handler.PENDING

    c.say(T5_CLEAN)
    assert c.gmail.send_calls == 1
    assert _sent_headers(c.gmail)["To"] == other, "the yes sent the pre-correction draft"


def test_a_negation_that_governs_something_else_still_reads_as_refusing_the_operation(clean_db):
    """D1, isolated. The positive control is asserted first: a real refusal MUST cancel, and it does.

    FIXED at 0bf04af. `directive_scope` now reads what each negation GOVERNS, so
    "YES WRITE IT AND SEND IT NO MORE QUESTIONS" no longer cancels because it contains "no", and
    "dont show me draft" changes presentation instead of closing the Decision. The cancellation
    assertions below therefore PASS.

    What still fails is the final `send_calls == 1`: the confirmation no longer cancels the goal, and it
    does not execute either. That is a separate root cause in the execution seam, not in negation scope —
    the docstring said otherwise while the assertions were already correct, which is its own small lesson
    about trusting a test's prose over its asserts.
    """
    # POSITIVE CONTROL — an actual refusal cancels the goal and sends nothing.
    stop = "actually nah dont send it"
    control = Conversation({**SCRIPT, stop: _decision(IntentKind.status_cancel_correction, text="ok")})
    for turn in (T1, T2_CLEAN, T3):
        control.say(turn)
    control.say(stop)
    assert control.goals()[0]["status"] == "cancelled", "a plain refusal no longer stops the send"
    assert control.gmail.send_calls == 0

    # THE DEFECT — the same machinery fires on a turn that means the exact opposite.
    c = Conversation(SCRIPT)
    for turn in (T1, T2_CLEAN, T3):
        c.say(turn)
    assert c.goals()[0]["status"] == "awaiting_approval"

    c.say(T5)
    assert c.goals()[0]["status"] != "cancelled", (
        "a turn that means proceed must never cancel — regression guard for 0bf04af")
    assert c.gmail.send_calls == 1, "the most emphatic instruction in the transcript sent nothing"


def test_the_tone_amendment_is_lost_when_the_student_names_the_artifact(clean_db):
    """D2, isolated. `continuation._STYLE` reads a tone word only where it directly follows "make it", so
    "make it professional" patches `tone` and "make it a professional email" — the same instruction with
    the noun said out loud — patches nothing.

    The positive control runs first and passes, which is what makes the failure a statement about the
    pattern rather than about the amendment path.
    """
    control = Conversation(SCRIPT)
    control.say(T1)
    control.say(T2_CLEAN)
    assert control.goals()[0]["slots"]["tone"].value == "professional"   # positive control

    c = Conversation(SCRIPT)
    c.say(T1)
    c.say("make it a professional email and send it")     # same words, plus the noun; no negation
    assert c.goals()[0]["slots"]["tone"].value == "professional", (
        "naming the artifact ('a professional email') defeated the tone amendment that "
        "'make it professional' performs")


def test_a_conversational_turn_beside_an_open_goal_changes_nothing(clean_db):
    """The absence half of the acceptance criteria, paired with the tests above so it cannot pass by the
    handler simply never doing anything. A goal stays open across turns that have nothing to do with it,
    and claiming every one of those would turn "what's 8x7" into an interrogation about a subject line."""
    chat = "whats 8 times 7"
    c = Conversation({**SCRIPT, chat: _decision(IntentKind.casual, text="56", needs_mission=True,
                                                proposed_goal="do my homework")})
    c.say(T1)
    goal = c.goals()[0]
    c.say(chat)
    after = c.goals()
    assert len(after) == 1 and after[0]["id"] == goal["id"], "an unrelated turn opened durable state"
    assert after[0]["status"] == goal["status"]
    assert c.gmail.send_calls == 0


# ==========================================================================================================
# CALENDAR — the same spine, the other tool.
# ==========================================================================================================

def _calendar_conversation(script, adapter, monkeypatch) -> Conversation:
    """A student whose Google Calendar is connected and whose provider is the in-memory one. The adapter is
    injected where the production code builds it, so every layer above — the handler, the gate, the
    gateway, the read-back verification — is the real thing."""
    monkeypatch.setattr(calendar_adapter, "GoogleCalendarAdapter", lambda *a, **kw: adapter)
    return Conversation(script)


def _seed_event(c: Conversation, adapter: CountingCalendar, *, title: str, start: str, end: str) -> None:
    """An event that already exists on the calendar AND in Bruce's entity memory — the state the student is
    in after a verified create, which is the precondition for moving it."""
    _run(entity_store.record_event(
        c.uid, title=title, start=start, end=end, timezone=TZ, location=None,
        provider="google_calendar", provider_account_id=ACCOUNT, provider_event_id="evt_1",
        calendar_id="primary", source_message_ids=["seed"]))
    _run(adapter.insert(CalendarEvent(title=title, start=start, end=end, timezone=TZ), "evt_1"))


def _next_weekday(base: datetime.date, weekday: int) -> datetime.date:
    """The next date strictly after `base` falling on `weekday` (Mon=0). Used to build a start that is
    deliberately NOT a Friday, so "move it to friday" is a real change on any day this suite runs."""
    ahead = (weekday - base.weekday()) % 7 or 7
    return base + datetime.timedelta(days=ahead)


MOVE_NAMED = "move chess club to friday at 5pm"
MOVE_DEICTIC = "move it to friday"

_CAL_SCRIPT = {
    MOVE_NAMED: _decision(IntentKind.actionable, caps=[UPDATE_EVENT], text="ok"),
    MOVE_DEICTIC: _decision(IntentKind.actionable, caps=[UPDATE_EVENT], text="ok"),
    "yes": _decision(IntentKind.approval, text="ok"),
}


def test_moving_a_named_event_updates_the_provider_exactly_once_and_receipts_once(clean_db, monkeypatch):
    """The calendar half of the acceptance criteria, at the level the tree can actually reach today: the
    title is kept, the start AND the end move together, the provider is written exactly once, the reply is
    the one verified receipt, and a "yes" typed afterwards adds nothing.

    START AND END MOVING TOGETHER is the assertion that matters. A change is replacement-shaped and can name
    only one field, so a start moved on its own would leave the old end beside it — an event that starts on
    Friday and ends on Tuesday. `calendar_mutation.recompute` is the implementation that gets this right:
    it keeps the base clock and the ORIGINAL duration when only the date moves. The goal path's equivalent
    does not (see `test_moving_the_day_of_a_calendar_goal_keeps_the_clock_the_student_already_set`), which
    is why this test is worth having on the lane that works.
    """
    adapter = CountingCalendar(account=ACCOUNT)
    c = _calendar_conversation(_CAL_SCRIPT, adapter, monkeypatch)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = datetime.datetime.combine(_next_weekday(today, 1), datetime.time(16, 0))   # a Tuesday, 4pm
    end = start + datetime.timedelta(hours=2)
    _seed_event(c, adapter, title="chess club", start=start.isoformat(timespec="seconds"),
                end=end.isoformat(timespec="seconds"))

    reply = c.say(MOVE_NAMED)
    assert adapter.update_calls == 1, f"expected exactly one provider update, got {adapter.update_calls}"
    assert adapter.get_calls >= 1, "the move was never read back"
    assert "✅" in reply and "chess club" in reply.lower(), f"no verified receipt: {reply!r}"

    moved = adapter.events["evt_1"]
    assert moved["summary"] == "chess club", "the title was rewritten by a turn that only moved the time"
    new_start = datetime.datetime.fromisoformat(moved["start"]["dateTime"])
    new_end = datetime.datetime.fromisoformat(moved["end"]["dateTime"])
    assert new_start.weekday() == 4, f"the event did not land on a friday: {new_start}"
    assert new_start.hour == 17
    assert new_end - new_start == datetime.timedelta(hours=2), \
        "the end did not move with the start — the two-hour event was silently reshaped"

    # ONE authorization, bound to the recomputed time, and it is still the only one afterwards: a stale
    # "yes" must not silently mint a second consent for a write that already happened.
    grants = [a for a in c.authorizations() if a["operation"] == "update_event"]
    assert len(grants) == 1, "the update ran on something other than one authorization"
    assert not grants[0]["invalidated"]

    # A "yes" typed after a completed move is a stale approval. It must not write again.
    before = adapter.update_calls
    again = c.say("yes")
    assert adapter.update_calls == before, "a stale yes performed a second provider update"
    assert "✅" not in again, "a stale yes claimed a completion that did not happen this turn"
    assert len([a for a in c.authorizations() if a["operation"] == "update_event"]) == 1, \
        "a stale yes minted a second authorization to update the event"


def test_move_it_to_friday_lands_on_the_work_in_flight(clean_db, monkeypatch):
    """D3/D4, at the surface a student sees. The positive control — the same move with the event NAMED —
    is asserted first and passes; the deictic form the acceptance criteria specify does nothing at all.

    Two independent reasons, and both have to be fixed for this turn to work:

      * the goal spine cannot own it. `calendar.create_event` has a declared slot set and no
        CapabilityExecutor, so `goal_handler` DECLINES every calendar turn and no `schedule_event` goal is
        ever created. With no open goal there is nothing for `continuation` to patch `start` onto, even
        though the mapping for it exists and is tested (`_SLOT_FOR_ROLE[ROLE_TEMPORAL]`).
      * the legacy path cannot own it either. `entity_resolution._GENERIC_REF` deliberately refuses a lone
        "it" — correctly, since a bare "it" appears in almost any sentence — so `CalendarMutationHandler`
        finds no referent and declines. The refusal is right; what is missing is the layer above it that
        knows which event is in flight.

    So the student's move is understood by nothing and answered by the model's own reply.
    """
    def _student(monkeypatch_):
        adapter = CountingCalendar(account=ACCOUNT)
        conv = _calendar_conversation(_CAL_SCRIPT, adapter, monkeypatch_)
        today = datetime.datetime.now(datetime.timezone.utc).date()
        start = datetime.datetime.combine(_next_weekday(today, 1), datetime.time(16, 0))
        _seed_event(conv, adapter, title="chess club", start=start.isoformat(timespec="seconds"),
                    end=(start + datetime.timedelta(hours=1)).isoformat(timespec="seconds"))
        return conv, adapter

    control, control_adapter = _student(monkeypatch)
    control.say(MOVE_NAMED)
    assert control_adapter.update_calls == 1              # positive control: the named move works

    # The same event, the same intent — said the way a person says it once the thing is already on screen.
    c, adapter = _student(monkeypatch)
    c.say(MOVE_DEICTIC)
    assert adapter.update_calls == 1, (
        "'move it to friday' — the exact phrase the acceptance criteria name — reached no handler: the "
        "goal seam declines calendar for want of an executor and entity resolution refuses a bare 'it'")


def test_a_calendar_request_never_becomes_a_typed_goal(clean_db, monkeypatch):
    """D3, stated once, plainly, where it can be seen. The acceptance criteria for calendar — one goal id
    across a move, a replacement Decision when the arguments change, attendees retained — all presuppose a
    `schedule_event` goal. None is ever created, so none of them can hold.

    The positive control is the email half: the identical shape of turn, on a capability that HAS an
    executor, does open a typed goal. That is what makes this a statement about the missing executor and
    not about the seam.
    """
    handler = goal_handler.GoalHandler()

    # POSITIVE CONTROL — the seam claims a send.
    email_ctx = conversation_outcomes.OutcomeContext(
        user_id=uuid4(), decision=_decision(IntentKind.actionable, caps=[SEND]), capsule=object(),
        msg=object(), profile=object(), channel="self_hosted_imessage", pmid="p1", style=object(),
        store=object(), conversation_id=PHONE, turn_index=1)
    assert _run(handler.evaluate(email_ctx)).disposition is conversation_outcomes.Disposition.claim

    cal_ctx = conversation_outcomes.OutcomeContext(
        user_id=uuid4(), decision=_decision(IntentKind.actionable, caps=[CREATE_EVENT]), capsule=object(),
        msg=object(), profile=object(), channel="self_hosted_imessage", pmid="p2", style=object(),
        store=object(), conversation_id=PHONE, turn_index=1)
    verdict = _run(handler.evaluate(cal_ctx))
    assert verdict.disposition is conversation_outcomes.Disposition.claim, (
        f"the goal seam declines every calendar turn ({verdict.reason}): `calendar.create_event` has a "
        "declared slot set and no CapabilityExecutor, so a calendar goal cannot exist")


def test_a_calendar_goal_keeps_its_title_and_attendees_when_only_the_time_moves(clean_db):
    """What the calendar goal WOULD do, proven against the production runtime rather than the handler —
    `goal_runtime.ensure_goal` and `continuation.resolve` are exactly what `goal_handler` calls, and they
    are already complete for `schedule_event`. This is the part of the calendar acceptance criteria that
    holds today, and isolating it is what makes the missing piece precisely "an executor" rather than
    "calendar support".

    A list-valued slot is the reason `ROLE_RECIPIENT` is email-only: `slot_patch` replaces, and replacing
    `attendees` would delete everyone who was not named in the last message.

    RETAINED IS NOT THE SAME AS DELIVERED, and it is worth being exact about which one this proves.
    `attendees` is declared with no `tool_arg`, and `models.CalendarEvent` has no attendees field at all,
    so the list survives every turn of the conversation and would still not reach Google once an executor
    exists. That is a second, separate gap from D3 and it is a schema one, not a wiring one.
    """
    uid = uuid4()
    _run(_seed(uid))
    view = _run(goal_runtime.ensure_goal(uid, capability=CREATE_EVENT, conversation_id=PHONE,
                                         slots_in=_rehearsal_slots(), turn_index=1, decision=None))
    assert view.kind is GoalKind.schedule_event and view.missing == ()

    moved = _move_the_goal(uid, view, "move it to friday at 5pm")
    assert moved.run_id == view.run_id, "the move opened a second calendar goal"
    assert moved.slots["title"].value == "rehearsal", "a time change rewrote the title"
    assert list(moved.slots["attendees"].value) == ["mr.kim@school.edu", "sam@school.edu"], \
        "a time change dropped the attendees"
    assert moved.slots["timezone"].value == TZ
    new_start = datetime.datetime.fromisoformat(moved.slots["start"].value)
    new_end = datetime.datetime.fromisoformat(moved.slots["end"].value)
    assert new_start.weekday() == 4 and new_start.hour == 17, f"the start did not move: {new_start}"
    assert new_end.date() == new_start.date(), "the end was left on the old day"
    assert new_end > new_start


def _rehearsal_slots() -> dict:
    """A fully specified calendar goal: a title, a one-hour timed slot, a zone and two attendees."""
    return {"title": SlotValue("rehearsal", Source.user_stated, turn_index=1),
            "start": SlotValue("2026-08-04T16:00:00", Source.user_stated, turn_index=1),
            "end": SlotValue("2026-08-04T17:00:00", Source.user_stated, turn_index=1),
            "timezone": SlotValue(TZ, Source.user_stated, turn_index=1),
            "attendees": SlotValue(["mr.kim@school.edu", "sam@school.edu"], Source.user_stated,
                                   turn_index=1)}


def _move_the_goal(uid, view, text: str):
    """One amendment turn against an open calendar goal, through the production calls `goal_handler`
    makes: resolve the continuation, turn the student's phrase into a real moment in THEIR zone, fold it
    into the goal. `now` is pinned so "friday" means one specific friday no matter when this suite runs."""
    run = _run(goal_runtime.open_goal_of_kind(uid, GoalKind.schedule_event, conversation_id=PHONE))
    cont = continuation_mod.resolve(uid, text=text, reply_to_message_id=None, open_goal=run,
                                    pending_decision=None, recent_draft={"id": view.run_id},
                                    recent_tool_result=None)
    assert cont.kind is continuation_mod.ContinuationKind.amend
    assert set(cont.slot_patch) == {"start"}, "the move patched something other than the start"
    at = datetime.datetime(2026, 8, 4, 12, 0, tzinfo=datetime.timezone.utc)
    patched = goal_handler.resolve_temporal(
        GoalKind.schedule_event,
        {"start": SlotValue(cont.slot_patch["start"], Source.user_stated, turn_index=2)},
        timezone_name=TZ, now=at)
    return _run(goal_runtime.ensure_goal(uid, capability=CREATE_EVENT, conversation_id=PHONE,
                                         slots_in=patched, turn_index=2, decision=None))


def test_moving_the_day_of_a_calendar_goal_keeps_the_clock_the_student_already_set(clean_db):
    """"Move it to friday" changes the DAY. It says nothing about the hour, and a 4pm rehearsal moved to
    Friday is a 4pm rehearsal on Friday.

    The positive control is asserted first: "move it to friday at 5pm", which states an hour, resolves to
    a timed 5-6pm slot on Friday. Then the same instruction WITHOUT an hour turns the event into an
    all-day block — `temporal.resolve` correctly reports a date-only phrase as a whole day, and
    `goal_handler.resolve_temporal` writes both halves of it over a start and end the student had already
    pinned. The other calendar path already gets this right and is worth naming as the reference:
    `calendar_mutation.recompute` keeps the base time and the original duration when only the date moves.
    """
    uid = uuid4()
    _run(_seed(uid))
    view = _run(goal_runtime.ensure_goal(uid, capability=CREATE_EVENT, conversation_id=PHONE,
                                         slots_in=_rehearsal_slots(), turn_index=1, decision=None))

    timed = _move_the_goal(uid, view, "move it to friday at 5pm")            # positive control
    assert datetime.datetime.fromisoformat(timed.slots["start"].value).hour == 17

    dayless = uuid4()
    _run(_seed(dayless))
    fresh = _run(goal_runtime.ensure_goal(dayless, capability=CREATE_EVENT, conversation_id=PHONE,
                                          slots_in=_rehearsal_slots(), turn_index=1, decision=None))
    moved = _move_the_goal(dayless, fresh, MOVE_DEICTIC)
    start = moved.slots["start"].value
    assert len(str(start)) > 10, (
        f"'move it to friday' dropped the 4pm the student had already set and made the event all-day "
        f"({start!r} -> {moved.slots['end'].value!r})")
    assert datetime.datetime.fromisoformat(start).hour == 16, "the hour the student set was not kept"


# ==========================================================================================================
# TWO GOALS AT ONCE — the invariant the whole selection-by-kind design exists for.
# ==========================================================================================================

def _open_calendar_goal(uid) -> str:
    """A `schedule_event` goal created through the production runtime. Nothing in the live tree creates one
    (see D3), so the multi-goal invariants have to be set up through the same call `goal_handler` would
    make — `goal_runtime.ensure_goal` — rather than through a turn."""
    slots = {"title": SlotValue("rehearsal", Source.user_stated, turn_index=1),
             "start": SlotValue("2026-08-04T16:00:00", Source.user_stated, turn_index=1),
             "end": SlotValue("2026-08-04T17:00:00", Source.user_stated, turn_index=1),
             "timezone": SlotValue(TZ, Source.user_stated, turn_index=1)}
    view = _run(goal_runtime.ensure_goal(uid, capability=CREATE_EVENT, conversation_id=PHONE,
                                         slots_in=slots, turn_index=1, decision=None))
    return view.run_id


def test_an_email_goal_and_a_calendar_goal_never_exchange_slots(clean_db):
    """A student can be booking a rehearsal and writing to a teacher in the same thread. Selection is by
    KIND, never by "the newest run" — `goal_runtime.ensure_goal` looks the goal up with
    `open_goal_of_kind`, so a recipient cannot land in the calendar goal and a start time cannot land in
    the email. That half holds, and it is asserted below.

    What does NOT hold is getting the turn to the right goal in the first place. The positive control is
    the same turn with only the email goal open: the drafted subject and body land on it. With a NEWER
    calendar goal open they land nowhere, because `conversation_runtime._open_goal_row` hands the handler
    the newest typed run and `goal_handler.capability_for_turn` takes its kind for any turn that names no
    capability — so the email draft is read as a calendar turn and declined for want of an executor. No
    contamination, but the student's turn is silently dropped, which is the transcript's own failure mode.
    """
    control = Conversation(SCRIPT)
    control.say(T1)
    control.say(T3)
    assert control.goal_of(GoalKind.send_email)["slots"]["subject"].filled     # positive control

    c = Conversation(SCRIPT)
    c.say(T1)
    email_run = c.goal_of(GoalKind.send_email)["id"]
    calendar_run = _open_calendar_goal(c.uid)
    assert calendar_run != email_run

    c.say(T3)                              # the model drafts a subject + body for the EMAIL
    email = c.goal_of(GoalKind.send_email)
    calendar = c.goal_of(GoalKind.schedule_event)
    assert email["id"] == email_run and calendar["id"] == calendar_run
    # NO CONTAMINATION — this part is real and holds.
    assert "recipient" not in calendar["slots"], "an email address landed in the calendar goal"
    assert "subject" not in calendar["slots"], "an email subject landed in the calendar goal"
    assert calendar["slots"]["title"].value == "rehearsal", "the calendar goal was rewritten"
    assert "start" not in email["slots"], "a calendar slot landed in the email goal"
    assert email["slots"]["recipient"].value == TEACHER
    # AND THE TURN STILL HAS TO ARRIVE.
    assert "subject" in email["slots"], (
        "the drafted subject reached neither goal: with a newer calendar goal open, an email turn that "
        "names no capability is read as a calendar turn and declined")


def test_send_it_resolves_the_email_decision_even_with_a_newer_calendar_goal_open(clean_db):
    """D4. The positive control is asserted first: with only the email goal open, the confirmation sends.
    Then the identical confirmation is typed with a NEWER calendar goal open, and it reaches nothing.

    `conversation_runtime._open_goal_row` returns the newest run that declares any typed kind, and both the
    pending decision and the draft handed to `continuation.resolve` are read off that single row. The email
    goal's Decision is not merely outranked — it is never looked at, so a clean "yes" resolves nothing and
    `goal_handler.capability_for_turn` reads the calendar goal's kind and declines the turn for want of an
    executor. The student's confirmation lands on the floor.

    `goal_runtime.resolve_capability` already refuses to guess when two kinds are open and says so in its
    docstring; what is missing is that the runtime never asks it, and nothing disambiguates by Decision.
    """
    control = Conversation(SCRIPT)
    for turn in (T1, T2_CLEAN, T3):
        control.say(turn)
    control.say(T5_CLEAN)
    assert control.gmail.send_calls == 1                   # positive control: the yes sends

    c = Conversation(SCRIPT)
    for turn in (T1, T2_CLEAN, T3):
        c.say(turn)
    assert (c.goal_of(GoalKind.send_email)["decision"] or {}).get("status") == goal_handler.PENDING
    _open_calendar_goal(c.uid)                             # a NEWER goal of a different kind

    c.say(T5_CLEAN)
    assert c.gmail.send_calls == 1, (
        "a confirmation of the email Decision reached nothing because a newer calendar goal was open — "
        "the runtime picks one open goal by recency and reads the pending Decision only off that row")
    assert c.goal_of(GoalKind.schedule_event)["status"] != "cancelled", \
        "the email confirmation was applied to the calendar goal instead"


def test_an_inline_reply_beats_recency_when_two_goals_are_open(clean_db):
    """D4, the second half. When two goals are open, the student's own inline reply is the ONLY unambiguous
    statement of which one they mean — it names a message, and that message belongs to one goal. The
    runtime hands the reply reference to `continuation.resolve`, which uses it as an artifact anchor, but
    the CHOICE of run was already made by recency before the reference was read.

    The positive control is the same reply with only one goal open, where it resolves as a reference and
    keeps the pending Decision intact.
    """
    control = Conversation(SCRIPT)
    for turn in (T1, T2_CLEAN, T3):
        control.say(turn)
    only = control.decision_ids()[0]
    control_reply = control.say(T4, reply_to=control.mid(3))
    assert control.decision_ids() == [only]                # positive control: the pointer resolves
    assert TEACHER in control_reply, "the pointer did not land on the email proposal"

    c = Conversation(SCRIPT)
    for turn in (T1, T2_CLEAN, T3):
        c.say(turn)
    email_run = c.goal_of(GoalKind.send_email)["id"]
    _open_calendar_goal(c.uid)                             # newer, and about something else entirely

    reply = c.say(T4, reply_to=c.mid(3))                   # "this", replying to the EMAIL proposal
    assert c.goal_of(GoalKind.send_email)["id"] == email_run
    # The ASSERTION THAT MATTERS is that the turn arrived somewhere. Checking only that the email goal is
    # still pending would pass just as well if the pointer reached nothing at all, which is exactly what
    # happens: the run is chosen by recency before the reply reference is ever consulted.
    assert TEACHER in reply, (
        "an inline reply pointing at the email proposal was answered by the model instead: the runtime "
        "picked the newer calendar goal by recency and never looked at the reply reference")


def test_a_refusal_closes_the_open_goal_and_every_outstanding_authorization(clean_db, monkeypatch):
    """ONE student, BOTH tools, one refusal. A calendar move leaves a real authorization row behind and an
    email goal is parked awaiting approval; "actually never mind" has to close both.

    `record_refusal` is deliberately unconditional about scope — it closes work in every conversation, not
    only the one the refusal was typed in — because asking a student which thread they meant while
    something irreversible is queued is not a conversation anyone wants to have. The positive control is
    asserted first and is what stops the "every authorization is closed" assertion from passing over an
    empty table.
    """
    stop = "actually never mind, dont send anything"
    adapter = CountingCalendar(account=ACCOUNT)
    c = _calendar_conversation(
        {**SCRIPT, **_CAL_SCRIPT, stop: _decision(IntentKind.status_cancel_correction, text="ok")},
        adapter, monkeypatch)
    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = datetime.datetime.combine(_next_weekday(today, 1), datetime.time(16, 0))
    _seed_event(c, adapter, title="chess club", start=start.isoformat(timespec="seconds"),
                end=(start + datetime.timedelta(hours=1)).isoformat(timespec="seconds"))

    c.say(MOVE_NAMED)                                    # a real provider write -> a real authorization row
    for turn in (T1, T2_CLEAN, T3):                      # and an email parked on the student's yes
        c.say(turn)

    # POSITIVE CONTROL — there is something to close, and it is open.
    before = c.authorizations()
    assert before, "no authorization was ever recorded, so the assertion below would prove nothing"
    assert any(not a["invalidated"] for a in before)
    assert c.goal_of(GoalKind.send_email)["status"] == "awaiting_approval"
    assert c.gmail.send_calls == 0

    c.say(stop)
    assert c.goal_of(GoalKind.send_email)["status"] == "cancelled"
    assert c.gmail.send_calls == 0, "a refusal still let the send through"
    after = c.authorizations()
    assert len(after) == len(before)
    assert all(a["invalidated"] or a["consumed"] for a in after), \
        "a refusal left an authorization open for a later turn to spend"
