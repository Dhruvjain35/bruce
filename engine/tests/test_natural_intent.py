"""NATURAL INTENT — the student states an objective, Bruce writes the parts nobody dictates.

WHAT THIS REPLACES. Bruce asked for every empty required slot, which is right for an address and absurd
for a subject line. "email sam and thank him for helping me with the project" became an interrogation:
who, then what subject, then what body — three questions to send one thank-you note. The student had
already said everything that mattered.

THE TAXONOMY IS THE FIX, and it is one question per slot: if Bruce filled this wrongly and the student
approved without noticing, what happens?

    GENERATABLE  a clumsy sentence they read on screen and reject      subject, body, event title
    RESOLVABLE   a letter to a real stranger                           recipient, attendees
    STATED       something that cannot be taken back or inspected      an event's start, a delete target

Generatable slots are WRITTEN. Resolvable and stated slots are ASKED for. Everything composed lands as
`model_derived`, so `_needs_confirmation` is guaranteed to put it on screen — which is the entire reason
writing it is safe.

NO MESSAGE IN THIS FILE SPELLS OUT "subject" OR "body". That is the point: a suite whose inputs dictate
the fields cannot tell you whether Bruce can handle a person talking normally.
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
from bruce_engine import (conversation_graph, conversation_outcomes, conversation_runtime, crypto,
                          gmail_adapter, goal_handler, goal_runtime, goal_slots, oauth_google, schema,
                          world_state)
from bruce_engine.conversation_contract import (ConversationDecision, ExtractedEntity, IntentKind,
                                                ResponseType, RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.goal_slots import Fill, GoalKind, SlotValue, Source
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.repositories import PostgresUserRepository

PHONE = "+15550166"
ACCOUNT = "student@example.com"
TEACHER = "alvarez@school.edu"
TZ = "America/Chicago"
SEND = "gmail.send_message"

users = PostgresUserRepository()


def _run(c):
    return asyncio.run(c)


# ==========================================================================================================
# THE TAXONOMY ITSELF — pure, no database.
# ==========================================================================================================

def test_every_required_slot_is_classified_and_the_default_is_the_safe_one():
    """An unclassified slot defaults to STATED, so a new required slot is ASKED for rather than quietly
    invented. Getting that default backwards is how a future tool starts making things up."""
    assert goal_slots._SlotDecl("anything").fill is Fill.stated
    for kind in GoalKind:
        for spec in goal_slots.slot_specs(kind):
            assert isinstance(spec.fill, Fill), f"{kind.value}.{spec.name} is unclassified"


def test_the_recipient_is_never_generatable_and_the_subject_always_is():
    """The load-bearing line of the whole taxonomy. A wrong subject is embarrassing; a wrong recipient is
    a letter to a stranger, and no amount of fluency makes inventing one acceptable."""
    assert goal_slots.spec_for(GoalKind.send_email, "recipient").fill is Fill.resolvable
    assert goal_slots.spec_for(GoalKind.send_email, "subject").fill is Fill.generatable
    assert goal_slots.spec_for(GoalKind.send_email, "body").fill is Fill.generatable
    # ...and on the other product, the same test with the answers the other way round.
    assert goal_slots.spec_for(GoalKind.schedule_event, "start").fill is Fill.stated
    assert goal_slots.spec_for(GoalKind.schedule_event, "title").fill is Fill.generatable
    assert goal_slots.spec_for(GoalKind.schedule_event, "attendees").fill is Fill.resolvable


def test_a_missing_recipient_is_asked_for_and_a_missing_subject_is_not():
    gaps = goal_slots.classify_fill(GoalKind.send_email, ("recipient", "subject", "body"))
    assert gaps[Fill.resolvable] == ("recipient",)
    assert set(gaps[Fill.generatable]) == {"subject", "body"}
    assert goal_slots.blocking_gap(GoalKind.send_email, ("recipient", "subject")) == ("recipient",)
    assert goal_slots.generatable_gap(GoalKind.send_email, ("recipient", "subject")) == ("subject",)


def test_the_mechanism_is_generic_across_products():
    """Not an email shortcut. The same two calls answer for a calendar goal, and a third product answers
    by declaring `fill` on its slots and changing no code here."""
    assert goal_slots.blocking_gap(GoalKind.schedule_event, ("title", "start")) == ("start",)
    assert goal_slots.generatable_gap(GoalKind.schedule_event, ("title", "start")) == ("title",)


# ==========================================================================================================
# THROUGH THE REAL RUNTIME — vague requests, real Postgres, a fake Google, a scripted composer.
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


class Composer:
    """A scripted draft writer. Records exactly what it was asked, so a test can assert Bruce handed it
    the objective and the tone rather than the raw message."""

    name = "scripted"

    def __init__(self, subject="thanks for the help", body="hey, thank you so much for the help. it "
                                                            "made a real difference. appreciate you."):
        self.subject, self.body = subject, body
        self.calls: list[dict] = []

    async def compose(self, *, objective, recipient, tone="", context=""):
        self.calls.append({"objective": objective, "recipient": recipient, "tone": tone,
                           "context": context})
        return {"subject": self.subject, "body": self.body}


class Silent(Composer):
    """A composer that cannot write. The honest degradation is a question, never a blank subject line."""

    async def compose(self, **kw):
        self.calls.append(kw)
        return {}


def _entity(kind, value):
    return ExtractedEntity(type=kind, value=value, normalized=None)


def _decision(intent, *, text="ok", caps=(), entities=()):
    return ConversationDecision(
        intent=intent, response_type=ResponseType.direct_answer, user_visible_response=text,
        extracted_entities=list(entities), required_capabilities=list(caps), needs_mission=False,
        proposed_goal=None, risk_level=RiskLevel.none, confidence=0.9)


class Scripted:
    provider = model = "fake"
    supports_vision = True

    def __init__(self, script):
        self.script, self.calls = script, 0

    async def decide(self, *, text, images, context):
        self.calls += 1
        return ReasonResult(decision=self.script.get((text or "").strip(),
                                                     _decision(IntentKind.clarification)),
                            provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)


class Student:
    def __init__(self, script, monkeypatch, composer=None):
        self.uid = uuid4()
        _run(self._seed())
        self.gmail = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
        self.composer = composer or Composer()
        self.reasoner = Scripted(script)
        self.channel = FakeChannel()
        self.tag = uuid4().hex[:8]
        self.n = 0

    async def _seed(self):
        await users.ensure(self.uid, auth_provider="test")
        async with user_session(self.uid) as s:
            s.add(schema.Integration(
                user_id=self.uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
                scopes=["https://www.googleapis.com/auth/gmail.send",
                        "https://www.googleapis.com/auth/gmail.readonly"],
                refresh_token_encrypted=crypto.encrypt("rt"), selected_calendar_id="primary",
                status="connected"))
        await world_state.set_timezone(self.uid, TZ, source="user_stated")

    def say(self, text):
        self.n += 1
        msg = InboundMessage(provider_message_id=f"{self.tag}-m{self.n}",
                             channel=ChannelKind.self_hosted_imessage, channel_identity=PHONE,
                             text=text, attachments=[],
                             timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)
        msg.user_id = self.uid
        _run(conversation_graph.ingest_inbound_message(msg))
        handlers = [goal_handler.GoalHandler(adapter=self.gmail, composer=self.composer)
                    if h.name == "goal" else h for h in conversation_outcomes.default_handlers()]
        out = _run(conversation_runtime.handle(self.channel, msg, user_id=self.uid, reply_target=PHONE,
                                               reasoner=self.reasoner, handlers=handlers))
        assert out.status == "processed", out.status
        return _run(self._reply())

    async def _reply(self):
        async with user_session(self.uid) as s:
            rows = (await s.execute(
                select(schema.ConversationTurn)
                .where(schema.ConversationTurn.user_id == self.uid,
                       schema.ConversationTurn.role == "assistant")
                .order_by(schema.ConversationTurn.created_at.desc()).limit(1))).scalars().all()
        return rows[0].text if rows else ""

    def goal(self):
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid)
                    .order_by(schema.AgentRun.created_at))).scalars().all()
            for r in rows:
                kind, slots = goal_slots.from_goal_jsonb(r.goal if isinstance(r.goal, dict) else None)
                if kind is GoalKind.send_email:
                    return {"id": str(r.id), "status": r.status, "slots": slots,
                            "decision": r.active_decision}
            return None
        return _run(_read())

    def authorizations(self):
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AuthorizationEvidenceRow).where(
                    schema.AuthorizationEvidenceRow.user_id == self.uid))).scalars().all()
            return list(rows)
        return _run(_read())


# --- "email <address> and thank him for helping me" --------------------------------------------------------

VAGUE = "email dhruvhydrox@gmail.com and thank him for helping me with the project"
YES = "yes"
SCRIPT = {
    # The model names the operation, the person and the OBJECTIVE — and no subject, no body, because a
    # person talking normally does not supply those.
    VAGUE: _decision(IntentKind.actionable, caps=["email.send_message"],
                     entities=[_entity("email", "dhruvhydrox@gmail.com"),
                               _entity("purpose", "thank him for helping me with the project")]),
    YES: _decision(IntentKind.approval),
}


def test_a_vague_request_is_written_not_interrogated(clean_db, monkeypatch):
    """THE HEADLINE. Recipient plus purpose is enough; the subject and body are written."""
    s = Student(SCRIPT, monkeypatch)
    reply = s.say(VAGUE)

    goal = s.goal()
    assert goal is not None, "no goal was created from a natural request"
    assert goal["slots"]["recipient"].value == "dhruvhydrox@gmail.com"
    assert goal["slots"]["subject"].filled and goal["slots"]["body"].filled, \
        "Bruce did not write the parts it is allowed to write"
    assert goal["slots"]["subject"].source is Source.model_derived, \
        "a written draft must be model_derived, or nothing forces it onto the screen"

    # The reply SHOWS the draft (so it contains the word "subject:") — what it must never do is ASK for
    # one. `goal_runtime.clarifying_question` is the only thing that asks, and it says "i still need".
    low = reply.lower()
    assert "i still need" not in low, f"Bruce asked for something it could write: {reply!r}"
    for asked in ("what should the subject", "what should it say", "what do you want it to say"):
        assert asked not in low, f"Bruce asked for something it could write: {reply!r}"
    assert goal["status"] == "awaiting_approval", "the turn did not reach a confirmation"
    assert s.gmail.send_calls == 0
    assert s.authorizations() == []


def test_the_confirmation_is_asked_explicitly_and_once(clean_db, monkeypatch):
    s = Student(SCRIPT, monkeypatch)
    reply = s.say(VAGUE)
    goal = s.goal()
    assert goal["status"] == "awaiting_approval"
    block = goal["decision"] or {}
    assert block.get("status") == goal_handler.PENDING
    assert block.get("arguments_fingerprint"), "the draft was not frozen into the Decision"
    assert reply.rstrip().endswith("?"), f"Bruce did not ask for confirmation: {reply!r}"


def test_the_generated_draft_is_frozen_into_the_decision(clean_db, monkeypatch):
    """The student approves WHAT THEY READ. The fingerprint is taken over the arguments that would reach
    Gmail, so a draft rewritten after the offer cannot be sent on the old yes."""
    s = Student(SCRIPT, monkeypatch)
    s.say(VAGUE)
    goal = s.goal()
    from bruce_engine import authorization_evidence as ae
    expect = ae.fingerprint(ae.normalize_arguments("gmail", "send_message", {
        "to": goal["slots"]["recipient"].value,
        "subject": goal["slots"]["subject"].value,
        "body": goal["slots"]["body"].value}))
    assert (goal["decision"] or {}).get("arguments_fingerprint") == expect


def test_the_composer_is_given_the_objective_not_the_raw_message(clean_db, monkeypatch):
    """The objective may have been stated three turns ago. Re-reading only the latest message is how
    "make it shorter" became a brand-new subjectless request in the transcript."""
    s = Student(SCRIPT, monkeypatch)
    s.say(VAGUE)
    assert len(s.composer.calls) == 1, "the draft was composed more than once"
    call = s.composer.calls[0]
    assert "thank him for helping me" in call["objective"]
    assert call["recipient"] == "dhruvhydrox@gmail.com"


def test_zero_gmail_calls_before_confirmation_then_exactly_one_after(clean_db, monkeypatch):
    s = Student(SCRIPT, monkeypatch)
    s.say(VAGUE)
    assert s.gmail.send_calls == 0, "something reached Gmail before the student confirmed"
    assert s.authorizations() == []

    receipt = s.say(YES)
    assert s.gmail.send_calls == 1, "the confirmation did not send"
    assert len([a for a in s.authorizations()]) == 1, "more than one authorization was minted"
    assert "✅" in receipt, f"no verified receipt: {receipt!r}"
    assert s.goal()["status"] == "completed"


def test_a_repeated_confirmation_sends_nothing_more(clean_db, monkeypatch):
    s = Student(SCRIPT, monkeypatch)
    s.say(VAGUE)
    s.say(YES)
    before = (s.gmail.send_calls, len(s.authorizations()))
    s.say(YES)
    assert (s.gmail.send_calls, len(s.authorizations())) == before


# --- tone, and the one question that IS still owed ----------------------------------------------------------

TONE_ASK = "email alvarez@school.edu and let her know i'll be 10 minutes late, make it professional"
TONE_SCRIPT = {
    TONE_ASK: _decision(IntentKind.actionable, caps=["email.send_message"],
                        entities=[_entity("email", TEACHER),
                                  _entity("purpose", "let her know i'll be 10 minutes late"),
                                  _entity("tone", "professional")]),
}


def test_a_stated_tone_reaches_the_composer_and_is_retained(clean_db, monkeypatch):
    s = Student(TONE_SCRIPT, monkeypatch, composer=Composer(subject="running late", body="hi, i'll be "
                                                            "about ten minutes late today. apologies."))
    reply = s.say(TONE_ASK)
    assert s.composer.calls[0]["tone"] == "professional"
    assert s.goal()["slots"]["tone"].value == "professional"
    assert "i still need" not in reply.lower(), f"a stated tone still produced a question: {reply!r}"
    assert s.goal()["status"] == "awaiting_approval"


NO_RECIPIENT = "email my teacher and thank her for helping me"
NO_RECIPIENT_SCRIPT = {
    # No address anywhere: not in the text, not in the entities. Nothing in this tree can resolve
    # "my teacher" — see `Fill.resolvable` — so this is the one question that is still owed.
    NO_RECIPIENT: _decision(IntentKind.actionable, caps=["email.send_message"],
                            entities=[_entity("purpose", "thank her for helping me")]),
}


def test_an_unresolvable_person_is_one_short_question_and_nothing_else(clean_db, monkeypatch):
    """THE HONEST LIMIT, asserted rather than hidden. A recipient is RESOLVABLE and nothing can resolve
    "my teacher" yet, so Bruce asks — once, for the address, and not for a subject line."""
    s = Student(NO_RECIPIENT_SCRIPT, monkeypatch)
    reply = s.say(NO_RECIPIENT)
    assert s.composer.calls == [], "a draft was written before Bruce knew who it was for"
    assert s.gmail.send_calls == 0
    goal = s.goal()
    assert goal is not None and goal["status"] != "awaiting_approval"
    assert "subject" not in reply.lower(), f"asked for a subject as well: {reply!r}"
    assert reply.count("?") <= 1, f"more than one question: {reply!r}"


def test_a_composer_that_cannot_write_asks_instead_of_proposing_a_blank(clean_db, monkeypatch):
    """The honest degradation. An email with an empty subject line must never reach a confirmation."""
    s = Student(SCRIPT, monkeypatch, composer=Silent())
    reply = s.say(VAGUE)
    goal = s.goal()
    assert goal["status"] != "awaiting_approval", "a blank draft was offered for confirmation"
    assert (goal["decision"] or {}).get("status") != goal_handler.PENDING
    assert s.gmail.send_calls == 0
    assert reply.strip(), "Bruce said nothing at all"


def test_a_written_draft_never_overwrites_what_the_student_said(clean_db, monkeypatch):
    """A composed value is `model_derived`; a stated one outranks it. The student's own words win, which
    is what makes a later correction work."""
    stated = dict(SCRIPT)
    stated[VAGUE] = _decision(
        IntentKind.actionable, caps=["email.send_message"],
        entities=[_entity("email", "dhruvhydrox@gmail.com"),
                  _entity("purpose", "thank him"), _entity("subject_line", "my own subject")])
    s = Student(stated, monkeypatch)
    s.say(VAGUE)
    assert s.goal()["slots"]["subject"].value == "my own subject"
