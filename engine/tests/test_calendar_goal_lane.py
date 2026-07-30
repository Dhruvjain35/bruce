"""THE CALENDAR GOAL LANE, end to end — the chain D3 left with nothing to run on.

`goal_slots` declared a full slot set for `calendar.create_event`, `CalendarCreateExecutor` was written
and tested, and `goal_handler._EXECUTORS` had no row joining the two. So the seam DECLINED every calendar
turn with `capability_has_no_executor`, a `schedule_event` goal could never exist, and every calendar
acceptance criterion — one goal id across a move, a replacement Decision, attendees retained — had nothing
live to hold. One registry row is the whole fix; this file is the proof that the row is enough.

WHAT IS PROVEN, link by link, against the real thing:

    Decision -> AuthorizationEvidence -> ExecutionAttempt -> MutationGateway
             -> provider create -> fetch-back -> Receipt

Real Postgres, the real runtime, the real continuation, the real execution gate and the real
MutationGateway. The only substitutions are the model (a scripted reasoner) and Google (the in-memory
adapter that already enforces Google's own insert/409/read-back rules). Every assertion is about what the
PROVIDER and the DATABASE hold afterwards, never about what a handler believed.

The two absence assertions matter as much as the presence ones: nothing reaches the provider before the
student says yes, and two confirmations arriving together produce ONE event — the deterministic provider
event id is derived from the run, so Google's own 409 collapses the second insert into a read-back.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (agent_loop, calendar_adapter, conversation_graph, conversation_outcomes,
                          conversation_runtime, crypto, entity_store, goal_handler, goal_slots,
                          oauth_google, schema, world_state)
from bruce_engine.conversation_contract import (ConversationDecision, ExtractedEntity, IntentKind,
                                                ResponseType, RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.goal_slots import GoalKind
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.repositories import PostgresUserRepository

PHONE = "+15550177"
ACCOUNT = "student@example.com"
TZ = "America/Chicago"
CAL = "https://www.googleapis.com/auth/calendar.events"
CREATE_EVENT = "calendar.create_event"

ASK = "put rehearsal on my calendar friday at 4pm"
YES = "yes"

users = PostgresUserRepository()


def _run(c):
    return asyncio.run(c)


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


class CountingCalendar(calendar_adapter.FakeCalendarAdapter):
    """The in-memory calendar plus the two counts this file reasons about: how many times the provider was
    told to create, and how many times the result was read back."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.get_calls = 0

    async def get(self, event_id: str):
        self.get_calls += 1
        return await super().get(event_id)


def _entity(kind: str, value: str) -> ExtractedEntity:
    return ExtractedEntity(type=kind, value=value, normalized=None)


def _decision(intent, *, text="ok", caps=(), entities=()):
    return ConversationDecision(
        intent=intent, response_type=ResponseType.direct_answer, user_visible_response=text,
        extracted_entities=list(entities), required_capabilities=list(caps), needs_mission=False,
        proposed_goal="put rehearsal on the calendar", risk_level=RiskLevel.none, confidence=0.9)


SCRIPT = {
    ASK: _decision(IntentKind.actionable, caps=[CREATE_EVENT],
                   entities=[_entity("title", "rehearsal"), _entity("start", "friday at 4pm")]),
    YES: _decision(IntentKind.approval),
}


class Scripted:
    provider = model = "fake"
    supports_vision = True

    def __init__(self, script):
        self.script = script
        self.calls = 0

    async def decide(self, *, text, images, context):
        self.calls += 1
        return ReasonResult(decision=self.script.get((text or "").strip(),
                                                     _decision(IntentKind.clarification)),
                            provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)


class Student:
    """One student, one thread, N turns through the REAL inbound runtime with a fake Google behind it."""

    def __init__(self, monkeypatch):
        self.uid = uuid4()
        _run(self._seed())
        self.cal = CountingCalendar(account=ACCOUNT)
        # Injected where production BUILDS the adapter, so every layer above it — the handler, the gate,
        # the gateway, the read-back — is the real thing.
        monkeypatch.setattr(calendar_adapter, "GoogleCalendarAdapter", lambda *a, **kw: self.cal)
        self.reasoner = Scripted(SCRIPT)
        self.channel = FakeChannel()
        self.tag = uuid4().hex[:8]
        self.n = 0

    async def _seed(self):
        await users.ensure(self.uid, auth_provider="test")
        async with user_session(self.uid) as s:
            s.add(schema.Integration(
                user_id=self.uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
                scopes=[CAL], refresh_token_encrypted=crypto.encrypt("rt"),
                selected_calendar_id="primary", status="connected"))
        await world_state.set_timezone(self.uid, TZ, source="user_stated")

    def _inbound(self, text, pmid):
        return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                              channel_identity=PHONE, text=text, attachments=[],
                              timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)

    def _handlers(self):
        return [goal_handler.GoalHandler(adapter=self.cal) if h.name == "goal" else h
                for h in conversation_outcomes.default_handlers()]

    def say(self, text: str) -> str:
        self.n += 1
        msg = self._inbound(text, f"{self.tag}-m{self.n}")
        msg.user_id = self.uid
        _run(conversation_graph.ingest_inbound_message(msg))
        out = _run(conversation_runtime.handle(self.channel, msg, user_id=self.uid, reply_target=PHONE,
                                               reasoner=self.reasoner, handlers=self._handlers()))
        assert out.status == "processed", out.status
        return _run(self._last_reply())

    def say_twice_at_once(self, text: str) -> None:
        """The SAME confirmation arriving twice concurrently — a webhook redelivery, a double tap."""
        async def _both():
            await asyncio.gather(
                conversation_runtime.handle(self.channel, self._inbound(text, f"{self.tag}-race-a"),
                                            user_id=self.uid, reply_target=PHONE,
                                            reasoner=self.reasoner, handlers=self._handlers()),
                conversation_runtime.handle(self.channel, self._inbound(text, f"{self.tag}-race-b"),
                                            user_id=self.uid, reply_target=PHONE,
                                            reasoner=self.reasoner, handlers=self._handlers()))
        _run(_both())

    # --- reading what actually happened ---------------------------------------------------------------

    async def _last_reply(self) -> str:
        async with user_session(self.uid) as s:
            rows = (await s.execute(
                select(schema.ConversationTurn)
                .where(schema.ConversationTurn.user_id == self.uid,
                       schema.ConversationTurn.role == "assistant")
                .order_by(schema.ConversationTurn.created_at.desc()).limit(1))).scalars().all()
        return rows[0].text if rows else ""

    def _rows(self) -> list[dict]:
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid)
                    .order_by(schema.AgentRun.created_at))).scalars().all()
            return [{"id": str(r.id), "status": r.status, "goal": r.goal or {},
                     "decision": r.active_decision, "domain": r.domain,
                     "verification": r.verification_result, "last": r.last_tool_result} for r in rows]
        return _run(_read())

    def goals(self) -> list[dict]:
        out = []
        for r in self._rows():
            kind, slots = goal_slots.from_goal_jsonb(r["goal"])
            if kind is not None:
                out.append({**r, "kind": kind, "slots": slots})
        return out

    def attempts(self) -> list[dict]:
        return [r for r in self._rows() if agent_loop.is_execution_run(r)]

    def authorizations(self) -> list[dict]:
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AuthorizationEvidenceRow).where(
                    schema.AuthorizationEvidenceRow.user_id == self.uid))).scalars().all()
            return [{"id": str(r.authorization_id), "operation": r.operation, "provider": r.provider,
                     "type": r.authorization_type, "fingerprint": r.arguments_fingerprint,
                     "arguments": r.normalized_arguments, "decision_id": r.decision_id,
                     "consumed": r.consumed_at is not None, "attempt": r.consumed_by_attempt,
                     "invalidated": r.invalidated_at is not None} for r in rows]
        return _run(_read())

    def entities(self) -> list[dict]:
        return _run(entity_store.active_events(self.uid))


# ==========================================================================================================
# THE CHAIN
# ==========================================================================================================

def test_a_calendar_ask_opens_a_typed_goal_and_touches_no_provider(clean_db, monkeypatch):
    """LINK 0. The seam CLAIMS a calendar turn now, and the claim is worth nothing if it reaches Google
    before the student agrees. The absence is asserted at the adapter's own counter."""
    s = Student(monkeypatch)
    reply = s.say(ASK)

    goals = s.goals()
    assert len(goals) == 1, "the calendar ask produced no durable goal — D3, exactly as it was"
    goal = goals[0]
    assert goal["kind"] is GoalKind.schedule_event
    assert goal["slots"]["title"].value == "rehearsal"
    assert goal["status"] == "awaiting_approval"

    assert s.cal.insert_calls == 0, "something reached the calendar before the student confirmed"
    assert s.authorizations() == [], "consent was minted before anything was confirmed"
    assert "rehearsal" in reply.lower(), f"the proposal did not say what it was offering: {reply!r}"


def test_the_confirmation_carries_the_whole_chain_to_a_verified_event(clean_db, monkeypatch):
    """LINKS 1-7, each asserted where it is actually recorded.

    Decision -> AuthorizationEvidence -> ExecutionAttempt -> MutationGateway -> provider create ->
    fetch-back -> Receipt. Nothing here trusts a handler's own account of what it did.
    """
    s = Student(monkeypatch)
    s.say(ASK)

    # --- 1. DECISION: an exact operation on screen, fingerprinted, still pending.
    block = s.goals()[0]["decision"] or {}
    assert block.get("status") == goal_handler.PENDING
    assert block.get("capability") == CREATE_EVENT
    offered_fingerprint = block.get("arguments_fingerprint")
    assert offered_fingerprint, "consent was not bound to the arguments the student was shown"
    decision_id = block.get("decision_id")

    receipt = s.say(YES)

    # --- 2. AUTHORIZATION EVIDENCE: one record, answering THAT decision, bound to THOSE arguments.
    authz = s.authorizations()
    assert len(authz) == 1, f"expected exactly one authorization, got {len(authz)}"
    ev = authz[0]
    assert (ev["provider"], ev["operation"]) == ("google_calendar", "create_event")
    assert ev["type"] == "decision_approval", "a create ran on something other than the student's yes"
    assert ev["decision_id"] == decision_id, "the consent answers a different question than was asked"
    assert ev["fingerprint"] == offered_fingerprint, "the yes was spent on arguments nobody was shown"
    assert not ev["invalidated"]

    # --- 3. EXECUTION ATTEMPT: a durable row recording the provider call, linked to the goal it served.
    attempts = s.attempts()
    assert len(attempts) == 1, f"expected exactly one execution attempt, got {len(attempts)}"
    goal = s.goals()[0]
    assert agent_loop.parent_run_id_of(attempts[0]) == goal["id"], \
        "the attempt is not linked to the goal it was made for"
    assert goal_slots.SLOT_KEY not in attempts[0]["goal"], "the attempt grew a second copy of the slots"

    # --- 4. MUTATION GATEWAY: the ONLY thing that consumes an authorization, and only after the call
    #        returns. A consumed record scoped to the attempt key is the gateway's own signature.
    assert ev["consumed"], "the authorization was never spent — the gateway did not perform this write"
    assert ev["attempt"] == goal_handler.idempotency_key(goal["id"], CREATE_EVENT), \
        "the authorization was consumed by an attempt other than this goal's"

    # --- 5. PROVIDER CREATE: exactly one insert, with the arguments consent was bound to.
    assert s.cal.insert_calls == 1, f"expected exactly one create, got {s.cal.insert_calls}"
    created = list(s.cal.events.values())
    assert len(created) == 1
    assert created[0]["summary"] == "rehearsal"
    assert ev["arguments"]["title"] == "rehearsal"

    # --- 6. FETCH-BACK: Bruce went and looked, and the run records the verification rather than a hope.
    assert s.cal.get_calls >= 1, "the create was never read back"
    assert (goal["verification"] or {}).get("verified") is True, "the run claims no verified read-back"
    assert (goal["last"] or {}).get("provider_entity_id"), "no provider entity id was recorded"

    # --- 7. RECEIPT: said only after that read-back, and the event exists in Bruce's own memory of the
    #        student's world so a later "move it" has something to resolve against.
    assert "✅" in receipt and "rehearsal" in receipt.lower(), f"no honest receipt: {receipt!r}"
    assert goal["status"] == "completed", "the goal never closed on a verified create"
    titles = [e["title"].lower() for e in s.entities()]
    assert "rehearsal" in titles, "the created event is not in Bruce's entity memory"


def test_the_created_event_lands_at_the_time_the_student_said_in_their_own_zone(clean_db, monkeypatch):
    """"friday at 4pm" is 4pm where the STUDENT is. A resolver that used the process's zone would place
    every event hours off with no visible error at all."""
    s = Student(monkeypatch)
    s.say(ASK)
    s.say(YES)

    event = list(s.cal.events.values())[0]
    start = datetime.datetime.fromisoformat(event["start"]["dateTime"])
    assert start.weekday() == 4, f"the event did not land on a friday: {start}"
    assert start.hour == 16, f"the 4pm the student said was not kept: {start}"
    # NAIVE on purpose, and it is the student's local time. Google places a tz-less dateTime in the
    # calendar's own timezone, so the resolution that happened in `America/Chicago` is what lands. A
    # process running in UTC would have written 16:00 UTC here, which is 11am for this student.
    assert event["start"]["dateTime"].endswith("T16:00:00")
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    assert start.date() != utc_now.date() or start.hour != utc_now.hour


def test_two_confirmations_at_the_same_instant_create_exactly_one_event(clean_db, monkeypatch):
    """Exactly-once has to survive a RACE, not just a repeat. Both confirmations read an
    awaiting_approval run, both mint evidence and both reach the executor; the provider event id is
    derived from the RUN, so Google's own 409 collapses the second insert into a read-back."""
    s = Student(monkeypatch)
    s.say(ASK)
    assert s.cal.insert_calls == 0

    s.say_twice_at_once(YES)

    assert len(s.cal.events) == 1, "a concurrent confirmation produced a second calendar event"
    assert len([g for g in s.goals()]) == 1, "a concurrent confirmation opened a second goal"
    assert len(s.entities()) == 1, "a concurrent confirmation recorded the event twice"


def test_a_repeated_confirmation_after_the_create_adds_nothing(clean_db, monkeypatch):
    """The sequential twin of the race. A stale yes must not mint a second consent or a second event."""
    s = Student(monkeypatch)
    s.say(ASK)
    s.say(YES)
    before = (s.cal.insert_calls, len(s.authorizations()))

    again = s.say(YES)

    assert (s.cal.insert_calls, len(s.authorizations())) == before, \
        "a stale yes performed a second create or minted a second authorization"
    assert "✅" not in again, f"a stale yes claimed a completion that did not happen: {again!r}"


def test_a_calendar_goal_and_the_gate_agree_about_which_account(clean_db, monkeypatch):
    """The executor binds the connected account before it builds anything, so a create can never land on a
    calendar the student did not connect."""
    s = Student(monkeypatch)
    s.say(ASK)
    s.say(YES)
    assert s.cal.account == ACCOUNT
    assert s.entities()[0]["provider_account_id"] == ACCOUNT


def test_the_seam_still_refuses_a_slot_bearing_capability_with_no_executor(clean_db, monkeypatch):
    """The registry row is the fix and the REFUSAL is still the rule. Remove the row and the seam declines
    exactly as it did before — so this file is measuring one missing entry, not a relaxed boundary."""
    monkeypatch.delitem(goal_handler._EXECUTORS, CREATE_EVENT)
    s = Student(monkeypatch)
    s.say(ASK)
    assert s.goals() == [], "a goal was opened for a capability nothing can perform"
    assert s.cal.insert_calls == 0
