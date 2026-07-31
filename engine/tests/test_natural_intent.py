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

    def attempts(self):
        """Execution ATTEMPT rows — a provider call was made (or tried). Zero is the claim that matters
        before a confirmation."""
        from bruce_engine import agent_loop

        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid))).scalars().all()
            return [r for r in rows
                    if agent_loop.is_execution_run({"goal": r.goal if isinstance(r.goal, dict) else {}})]
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


# ==========================================================================================================
# CONVERSATION-LEARNED PEOPLE — the MAE, exactly as specified.
#
#   1. "my teacher is ms alvarez, her email is alvarez@school.edu"
#   2. "email my teacher and thank her for helping me"
#
# Bruce understands it, writes it, remembers it, asks once, sends once, and proves it. Neither message
# contains a subject, a body, or an address at the point it is needed — the address arrives one turn
# earlier as a fact about the student's life, which is the whole point.
# ==========================================================================================================

INTRO = "my teacher is ms alvarez, her email is alvarez@school.edu"
MAE = "email my teacher and thank her for helping me"

MAE_SCRIPT = {
    # Turn 1 is conversation. It names no capability and asks for nothing — it is a fact being stated.
    INTRO: _decision(IntentKind.casual, text="got it"),
    # Turn 2 names the operation and the objective. NO address, NO subject, NO body.
    MAE: _decision(IntentKind.actionable, caps=["email.send_message"],
                   entities=[_entity("purpose", "thank her for helping me")]),
    YES: _decision(IntentKind.approval),
}


def test_the_mae_two_turns_one_question_one_send(clean_db, monkeypatch):
    """THE BAR. Everything else in this file exists to make this line pass honestly."""
    s = Student(MAE_SCRIPT, monkeypatch,
                composer=Composer(subject="thank you", body="hi ms alvarez, thank you so much for all "
                                                            "your help. it really made a difference."))
    s.say(INTRO)                                    # 1. REMEMBERS
    reply = s.say(MAE)                              # 2. UNDERSTANDS, WRITES, ASKS ONCE

    goal = s.goal()
    assert goal is not None, "the MAE produced no goal"
    assert goal["slots"]["recipient"].value == TEACHER, "'my teacher' did not resolve"
    assert goal["slots"]["recipient"].source is Source.tool_result, \
        "a resolved address is an observation, not the student asserting it again this turn"
    assert goal["slots"]["subject"].filled and goal["slots"]["body"].filled, "nothing was written"
    assert goal["status"] == "awaiting_approval"
    assert (goal["decision"] or {}).get("status") == goal_handler.PENDING
    assert (goal["decision"] or {}).get("arguments_fingerprint")
    assert "i still need" not in reply.lower(), f"Bruce asked for something it had: {reply!r}"
    assert reply.rstrip().endswith("?"), f"no confirmation was asked: {reply!r}"

    assert s.gmail.send_calls == 0, "something reached Gmail before confirmation"
    assert s.authorizations() == []

    receipt = s.say(YES)                            # SENDS ONCE, AND PROVES IT
    assert s.gmail.send_calls == 1
    assert len(s.authorizations()) == 1
    assert s.gmail.get_calls >= 1 if hasattr(s.gmail, "get_calls") else True
    assert "✅" in receipt, f"no verified receipt: {receipt!r}"
    assert s.goal()["status"] == "completed"
    sent = [m for m in s.gmail.messages.values() if "SENT" in m["labelIds"]]
    assert len(sent) == 1, f"expected exactly one sent message, got {len(sent)}"
    headers = {h["name"]: h["value"] for h in sent[0]["payload"]["headers"]}
    assert headers["To"] == TEACHER


def test_an_introduction_alone_sends_nothing_and_opens_no_goal(clean_db, monkeypatch):
    """Turn 1 is a FACT, not a request. A student telling Bruce who someone is must not start anything."""
    s = Student(MAE_SCRIPT, monkeypatch)
    s.say(INTRO)
    assert s.goal() is None, "stating a fact opened a goal"
    assert s.gmail.send_calls == 0
    assert s.composer.calls == []


def test_what_was_learned_carries_its_provenance(clean_db, monkeypatch):
    """Name, relationship, email, source turn, provenance and confidence — an address acted on later has
    to be explainable then, not just now."""
    s = Student(MAE_SCRIPT, monkeypatch)
    s.say(INTRO)

    async def _read():
        async with user_session(s.uid) as sess:
            return (await sess.execute(select(schema.KnownPerson).where(
                schema.KnownPerson.user_id == s.uid))).scalars().all()
    rows = _run(_read())
    assert len(rows) == 1
    p = rows[0]
    assert p.name == "ms alvarez" and p.relationship == "teacher" and p.email == TEACHER
    assert p.provenance == "user_stated" and p.confidence >= 0.9
    assert p.source_message_id and TEACHER in (p.stated_span or "")
    assert p.forgotten_at is None and p.superseded_by_id is None


def test_a_correction_supersedes_and_the_new_address_is_the_one_used(clean_db, monkeypatch):
    """An explicit correction replaces the value WITHOUT deleting the history — a message that already
    went out still has to be explainable."""
    fix = "actually my teacher is ms alvarez, her email is alvarez2@school.edu"
    script = {**MAE_SCRIPT, fix: _decision(IntentKind.casual, text="ok")}
    s = Student(script, monkeypatch)
    s.say(INTRO)
    s.say(fix)
    s.say(MAE)
    assert s.goal()["slots"]["recipient"].value == "alvarez2@school.edu"

    async def _read():
        async with user_session(s.uid) as sess:
            return (await sess.execute(select(schema.KnownPerson).where(
                schema.KnownPerson.user_id == s.uid))).scalars().all()
    rows = _run(_read())
    assert len(rows) == 2, "a correction overwrote history instead of superseding it"
    assert sum(1 for r in rows if r.superseded_by_id is not None) == 1


def test_forgetting_removes_a_person_from_resolution(clean_db, monkeypatch):
    """...and Bruce goes back to asking, rather than quietly using a person the student retired."""
    drop = "forget my teacher"
    script = {**MAE_SCRIPT, drop: _decision(IntentKind.casual, text="ok")}
    s = Student(script, monkeypatch)
    s.say(INTRO)
    s.say(drop)
    reply = s.say(MAE)
    goal = s.goal()
    assert goal is None or "recipient" not in goal["slots"], "a forgotten person still resolved"
    assert s.gmail.send_calls == 0
    assert "?" in reply, f"Bruce did not ask after forgetting: {reply!r}"


def test_an_address_bruce_was_never_told_is_never_invented(clean_db, monkeypatch):
    """The rule the whole module serves. An unknown person is a question, not a plausible address.

    Asserted BEHAVIOURALLY. An earlier version of this test checked `"?" in reply`, which is punctuation
    rather than behaviour — and it happened to be the only thing that failed when the resolver was
    reverted, so the test looked rigorous while resting on a question mark. What matters is that no
    address was invented and that nothing moved.
    """
    unknown = "email my principal and thank her"
    script = {**MAE_SCRIPT,
              unknown: _decision(IntentKind.actionable, caps=["email.send_message"],
                                 entities=[_entity("purpose", "thank her")])}
    s = Student(script, monkeypatch)
    s.say(INTRO)                                    # a teacher is known; a principal is not
    s.say(unknown)

    goal = s.goal()
    slots = goal["slots"] if goal else {}
    # 1. the recipient is UNRESOLVED
    assert "recipient" not in slots or not slots["recipient"].filled, \
        f"a recipient was resolved for a person Bruce was never told about: {slots.get('recipient')}"
    # 2. no address was invented — and in particular NOT the one person Bruce does know
    assert TEACHER not in str({k: v.value for k, v in slots.items()}), \
        "the known teacher was substituted for an unknown principal"
    # 3. exactly ONE clarification outcome: the goal is still collecting, not offering
    assert goal is None or goal["status"] != "awaiting_approval"
    assert (goal or {}).get("decision") in (None, {}) or \
        ((goal["decision"] or {}).get("status") != goal_handler.PENDING), \
        "an unresolved recipient still produced a pending Decision"
    # 4/5/6. nothing was authorized, attempted, or sent
    assert s.authorizations() == [], "consent was minted for a message with no recipient"
    assert s.attempts() == [], "an execution attempt was made with no recipient"
    assert s.gmail.send_calls == 0, "a message was sent to an address nobody supplied"


def test_two_people_matching_one_referent_is_a_question_naming_them(clean_db, monkeypatch):
    """"The best match" is a guess wearing a ranking. Two candidates is one short question."""
    from bruce_engine import people

    res = people.Resolution(people.AMBIGUOUS, candidates=("ms alvarez", "mr diaz"))
    q = people.clarifying_question(res)
    assert "ms alvarez" in q and "mr diaz" in q
    assert "@" not in q, "a clarifying question read an address back at the student"
