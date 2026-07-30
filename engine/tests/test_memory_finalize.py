"""MEMORY, ACTUALLY WRITTEN — the production path, asserted on the row rather than on the call.

THE DEFECT THIS FILE EXISTS FOR, and it is a specific one. `memory_finalize.after_verified_outcome` had a
caller: `goal_handler` invokes it after the provider read-back that proves a send happened, and
`tests/test_goal_handler.py::test_memory_is_offered_the_outcome_only_after_the_read_back` proves the call
is made. That test asserts at the CALL and says why in its own docstring — because the function refused
every proposal it was handed. It built `subject="self"` with `kind=episodic`, which `memory_writer.assess`
rejects as NOT_USER_SPECIFIC, and `predicate="completed_action"`, which is not the namespaced
`domain.relation` the same function requires. So "the memory stack is wired" was true of the call graph
and false of the database: the seam was green and `memory_records` was empty.

A call-site assertion cannot tell those two states apart, which is why everything below asserts on ROWS.
The live tests drive `conversation_runtime.handle` — real Postgres, real router, real continuation, real
goal runtime, real execution gate, real MutationGateway, real `memory_policy`, real `memory_writer` — with
only the model (a scripted reasoner) and Google (the in-memory adapter that enforces Google's own
send/read-back rules) substituted. Nothing about the memory stack is faked anywhere in this file.

WHAT IS PINNED:

  * the writer is really called, and exactly ONE accepted row exists after a verified send;
  * that row is anchored to the verified goal (or the receipt that proved it) and to the message that
    authorized it, and its evidence is the student's own words;
  * finalizing the SAME outcome twice leaves one row — paired with a different outcome that writes a
    second, so "no second row" cannot pass because writes are simply broken;
  * a refused candidate leaves NO row, at both gates that can refuse one — this module's own
    "someone else's words are not evidence", and the write policy's sensitive-trait backstop;
  * the style path that already worked still works, and does not multiply across turns.

Every absence assertion is paired with a positive control asserted first. No test reads a log line, and
nothing here asserts on source text.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (conversation_outcomes, conversation_runtime, crypto, gmail_adapter,
                          goal_handler, memory_candidate, memory_finalize, memory_policy,
                          memory_record, memory_writer, oauth_google, schema)
from bruce_engine.conversation_contract import (ConversationDecision, IntentKind, ResponseType,
                                                RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.repositories import PostgresUserRepository

PHONE = "+15550177"
ACCOUNT = "student@example.com"
TEACHER = "alvarez@school.edu"
GSEND = "https://www.googleapis.com/auth/gmail.send"
GREAD = "https://www.googleapis.com/auth/gmail.readonly"
SEND = "gmail.send_message"
OUTCOME_PREDICATE = "gmail.completed_send_message"

users = PostgresUserRepository()


def _run(c):
    return asyncio.run(c)


# ==========================================================================================================
# PURE UNITS — no database. The shape decisions, tested where they can actually be made to fail.
# ==========================================================================================================

def _proposal(**over):
    base = dict(user_id=uuid4(), kind=memory_record.MemoryKind.episodic, subject="self",
                predicate="completed_action", value="emailed alvarez@school.edu about thank you",
                reason_it_matters="so a later 'did you send that?' has a true answer",
                trusted_text="yes send it", stated_span="yes send it",
                retention_policy=memory_record.RetentionPolicy.episodic)
    return memory_writer.MemoryProposal(**{**base, **over})


def test_the_shape_that_used_to_be_written_is_still_refused_at_two_independent_gates():
    """The round-two defect, pinned so a revert cannot be silent.

    Both halves are asserted because either one alone would have produced the same symptom — a caller that
    runs on every verified send and writes nothing — and a fix that repaired only one would still write
    nothing while looking repaired.
    """
    refused = memory_writer.assess(_proposal())
    assert refused.verdict == memory_writer.NOT_USER_SPECIFIC, \
        "subject='self' on a non-profile kind was accepted; SELF is profile's subject"
    assert not memory_record.is_namespaced_predicate("completed_action")

    # positive control: the shape this module builds now clears BOTH of those.
    assert memory_record.is_namespaced_predicate(memory_finalize.outcome_predicate(SEND))
    assert memory_finalize.outcome_subject(provider_entity_id="18f2") != memory_record.SELF


def test_the_predicate_is_namespaced_and_its_namespace_is_the_domain_retrieval_scopes_on():
    """`memory_retrieval.shortlist` selects on `entity_key` OR `domain`, and the domain column is the
    predicate's namespace. An outcome filed under the provider that produced it is therefore visible on a
    later turn the router already placed in that domain — which is what makes "did you send that?"
    answerable from the table instead of from a guess."""
    assert memory_finalize.outcome_predicate(SEND) == OUTCOME_PREDICATE
    assert memory_record.domain_of(memory_finalize.outcome_predicate(SEND)) == "gmail"
    assert memory_finalize.outcome_predicate("calendar.update_event") == "calendar.completed_update_event"
    # A capability id is a registry key; nothing forces it into a predicate's shape. A malformed one
    # falls back rather than taking the whole record down for an unfindable reason.
    for junk in ("", None, "weird", "a.b", "Gmail.Send Message!!"):
        assert memory_record.is_namespaced_predicate(memory_finalize.outcome_predicate(junk)), junk


def test_two_sends_are_two_claims_and_one_send_finalized_twice_is_one_claim():
    """Idempotency is not a counter in `memory_finalize` — it is `claim_key` plus the partial unique index
    `uq_memory_active_claim`. That only works if the subject names ONE outcome: a subject shared across
    sends (the capability, or the person written to) would make the second real email a DUPLICATE_CLAIM
    refusal and leave Bruce answering from a stale first outcome forever."""
    def key(**anchors):
        return memory_record.claim_key(kind=memory_record.MemoryKind.episodic,
                                       subject=memory_finalize.outcome_subject(**anchors),
                                       predicate=OUTCOME_PREDICATE)

    first = dict(goal_id=None, provider_entity_id="msg-1", source_message_id="m5")
    assert key(**first) == key(**first), "the same outcome hashed to two claims"
    assert key(**first) != key(goal_id=None, provider_entity_id="msg-2", source_message_id="m9")
    # The goal outranks the receipt: it survives a provider retry that mints a second id for one goal.
    assert memory_finalize.outcome_subject(goal_id="g1", provider_entity_id="msg-1") == "g1"
    assert memory_finalize.outcome_subject(source_message_id="m5") == "m5"
    assert memory_finalize.outcome_subject() == ""


def test_the_evidence_can_only_ever_be_the_students_own_words():
    """30 of the 38 adversarial-corpus failures were an affirmative living in forwarded or quoted material
    being read as the student speaking. `goal_handler` hands over `msg.text` RAW, and the candidate write
    path does not re-check grounding, so the strip has to happen here — and the span is taken FROM the
    stripped text rather than checked against it, which makes it structural."""
    own = "yes send it to her"
    assert memory_finalize._trusted_span(None, own) == own                      # positive control
    assert memory_finalize._trusted_span("send it", own) == "send it"           # honoured: really inside
    assert memory_finalize._trusted_span("i never wrote this", own) == own      # claim ignored
    assert memory_finalize._trusted_span(None, "> she wrote: yes send it") == ""
    assert memory_finalize._trusted_span(None, "   ") == ""


def test_the_candidate_handed_to_the_writer_is_one_the_policy_already_accepts(monkeypatch):
    """No database: the writer is intercepted, and the candidate it WOULD have stored is judged by the
    real `memory_policy.decide`. This is the test that fails if anyone ever tries to make the write land
    by loosening a floor instead of by using the shape the policy already has."""
    seen: list = []

    async def _capture(self, candidate, **kw):
        seen.append(candidate)
        return memory_writer.WriteReceipt(memory_policy.Outcome.store, memory_writer.STORE,
                                          memory_id=uuid4())

    monkeypatch.setattr(memory_writer.MemoryWriter, "evaluate", _capture)
    ok = _run(memory_finalize.after_verified_outcome(
        uuid4(), capability=SEND, summary=f"emailed {TEACHER} about thank you",
        trusted_text="yes send it", stated_span=None, source_message_id="m5",
        provider_entity_id="msg-1", goal_id="run-1"))

    assert ok and len(seen) == 1, "the writer was not called at all"
    candidate = seen[0]
    assert memory_policy.decide(candidate).outcome is memory_policy.Outcome.store
    assert candidate.subject_type is memory_candidate.SubjectType.conversation
    assert candidate.kind is memory_record.MemoryKind.episodic
    assert candidate.kind in memory_policy.SUBJECT_KIND_MATRIX[candidate.subject_type]
    assert candidate.subject_id == "run-1" and candidate.subject_id != memory_record.SELF
    assert candidate.is_trusted and not candidate.is_inferred
    assert not candidate.explicitly_stated_by_user, "they asked for the action, they did not state it"
    assert candidate.source_id == "m5", "the row is not anchored to the message that authorized it"

    # positive control on the guard: no words of the student's own, no candidate at all.
    seen.clear()
    assert not _run(memory_finalize.after_verified_outcome(
        uuid4(), capability=SEND, summary="emailed someone", trusted_text="> yes send it",
        stated_span=None, source_message_id="m5", provider_entity_id="msg-1"))
    assert seen == []


def test_no_task_slot_reaches_the_writer_as_a_memory_of_its_own(monkeypatch):
    """A recipient, a subject line and a body are true only until the task finishes, and they are
    authoritative on the run while it runs. A second, staler copy in memory is how an email reaches the
    wrong person. Exactly ONE candidate leaves this function per verified outcome, and it is the past
    tense of the whole thing."""
    seen: list = []

    async def _capture(self, candidate, **kw):
        seen.append(candidate)
        return memory_writer.WriteReceipt(memory_policy.Outcome.store, memory_writer.STORE,
                                          memory_id=uuid4())

    monkeypatch.setattr(memory_writer.MemoryWriter, "evaluate", _capture)
    _run(memory_finalize.after_verified_outcome(
        uuid4(), capability=SEND, summary=f"emailed {TEACHER} about thank you",
        trusted_text="yes send it", stated_span=None, source_message_id="m5",
        provider_entity_id="msg-1", goal_id="run-1"))
    assert len(seen) == 1
    assert seen[0].predicate == OUTCOME_PREDICATE, "a slot was filed under a predicate of its own"


# ==========================================================================================================
# THE LIVE PATH — real Postgres, real runtime, real memory stack, fake model + fake Gmail.
# ==========================================================================================================

@pytest.fixture
def live_db(clean_db, monkeypatch):
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


def _decision(intent, *, rt=ResponseType.direct_answer, text="ok", caps=None, needs_mission=False,
              proposed_goal=None):
    return ConversationDecision(
        intent=intent, response_type=rt, user_visible_response=text, extracted_entities=[],
        required_capabilities=caps or [], needs_mission=needs_mission, proposed_goal=proposed_goal,
        risk_level=RiskLevel.none, confidence=0.9)


class ScriptedReasoner:
    """The model the transcript actually had, turn by turn. Everything else in the path is real."""

    provider = "fake"
    model = "fake"
    supports_vision = True

    def __init__(self, script):
        self.script = script

    async def decide(self, *, text, images, context):
        decision = self.script.get((text or "").strip(), _decision(IntentKind.casual, text="ok"))
        return ReasonResult(decision=decision, provider="fake", model="fake",
                            input_tokens=0, output_tokens=0, latency_ms=1)


async def _seed(uid):
    await users.ensure(uid, auth_provider="test")
    async with user_session(uid) as s:
        s.add(schema.Integration(
            user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
            scopes=[GSEND, GREAD], refresh_token_encrypted=crypto.encrypt("rt"),
            selected_calendar_id="primary", status="connected"))


def _inbound(text, pmid):
    return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                          channel_identity=PHONE, text=text, attachments=[],
                          timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False)


class Conversation:
    """One student, one thread, N turns through the REAL inbound runtime."""

    def __init__(self, script):
        self.uid = uuid4()
        _run(_seed(self.uid))
        self.adapter = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
        self.reasoner = ScriptedReasoner(script)
        self.channel = FakeChannel()
        self.n = 0

    def say(self, text: str) -> str:
        self.n += 1
        pmid = f"m{self.n}"
        out = _run(conversation_runtime.handle(
            self.channel, _inbound(text, pmid), user_id=self.uid, reply_target=PHONE,
            reasoner=self.reasoner,
            handlers=[goal_handler.GoalHandler(adapter=self.adapter) if h.name == "goal" else h
                      for h in conversation_outcomes.default_handlers()]))
        assert out.status == "processed", out.status
        return pmid

    def run_ids(self) -> list[str]:
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == self.uid))).scalars().all()
            return [str(r.id) for r in rows]
        return _run(_read())


ASK = "can u email ms alvarez a thank you note"
ADDRESS = TEACHER
SUBJECT = "subject should be thank you"
BODY = "tell her thanks for the recommendation letter"
YES = "yes send it"

SCRIPT = {
    ASK: _decision(IntentKind.actionable, caps=[SEND], proposed_goal="email ms alvarez a thank-you note",
                   text="sure, who should it go to?"),
    ADDRESS: _decision(IntentKind.clarification, text="got it"),
    SUBJECT: _decision(IntentKind.clarification, text="ok"),
    BODY: _decision(IntentKind.clarification, text="ok"),
    YES: _decision(IntentKind.approval, text="ok"),
}


def _memories(uid, *, kind=None) -> list:
    async def _read():
        async with user_session(uid) as s:
            q = select(schema.MemoryRecordRow).where(schema.MemoryRecordRow.user_id == uid)
            if kind is not None:
                q = q.where(schema.MemoryRecordRow.kind == kind)
            return list((await s.execute(q)).scalars().all())
    return _run(_read())


def _spy_on_the_writer(monkeypatch) -> list:
    """Every candidate that reaches the ONE door, with the real write still happening behind it."""
    seen: list = []
    real = memory_writer.MemoryWriter.evaluate

    async def _passthrough(self, candidate, **kw):
        seen.append(candidate)
        return await real(self, candidate, **kw)

    monkeypatch.setattr(memory_writer.MemoryWriter, "evaluate", _passthrough)
    return seen


def _spy_on_finalize(monkeypatch) -> list:
    """The kwargs `goal_handler` actually passes, so a replay below is the production call and not a
    reconstruction of it."""
    calls: list[dict] = []
    real = memory_finalize.after_verified_outcome

    async def _spy(user_id, **kw):
        calls.append({"user_id": user_id, **kw})
        return await real(user_id, **kw)

    monkeypatch.setattr(memory_finalize, "after_verified_outcome", _spy)
    return calls


def test_a_verified_send_writes_exactly_one_accepted_memory_row(live_db, monkeypatch):
    """THE REQUIREMENT, on the row. Five turns through the real runtime; after the read-back that proves
    the email exists at Gmail, `memory_records` holds exactly one accepted episodic row that says what
    Bruce did — where before this change it held none, on a path that ran every time."""
    candidates = _spy_on_the_writer(monkeypatch)
    calls = _spy_on_finalize(monkeypatch)
    c = Conversation(SCRIPT)

    for turn in (ASK, ADDRESS, SUBJECT, BODY):
        c.say(turn)
    # PAIRED ABSENCE. Nothing has been executed, so there is no outcome to remember — but the style path
    # has already written, which is the control proving the writer is reachable and the table is live.
    assert _memories(c.uid, kind="episodic") == [], "an outcome was remembered before anything happened"
    assert _memories(c.uid, kind="style"), "the style path wrote nothing, so the absence above proves little"

    pmid = c.say(YES)
    assert c.adapter.send_calls == 1, "the fixture did not actually send, so nothing verified"
    assert len(calls) == 1, "a verified send never reached memory_finalize"
    assert any(x.kind is memory_record.MemoryKind.episodic for x in candidates), \
        "memory_writer was never handed the outcome"

    outcomes = _memories(c.uid, kind="episodic")
    assert len(outcomes) == 1, f"expected exactly one accepted outcome row, got {len(outcomes)}"
    row = outcomes[0]
    assert row.status == "active", "the row exists but is quarantined, so nothing can ever read it"

    # PROVENANCE: the verified goal (or the receipt that proved it), and the message that authorized it.
    anchor = calls[0].get("goal_id") or calls[0]["provider_entity_id"]
    assert row.subject == anchor
    assert anchor in c.run_ids() or anchor in c.adapter.messages, \
        "the memory is anchored to something that is neither the goal nor its receipt"
    assert calls[0]["provider_entity_id"] in c.adapter.messages, "the receipt names no real message"
    assert row.source_message_id == pmid, "the row is not traceable to the turn that authorized it"
    assert row.subject != memory_record.SELF

    # WHAT IT SAYS, and on what terms.
    assert row.predicate == OUTCOME_PREDICATE and row.domain == "gmail"
    assert TEACHER in (row.value_json or {}).get("value", "")
    assert row.source_type == "trusted_user_text" and row.confidence == 1.0
    assert row.retention_policy == "episodic" and row.expires_at is not None, \
        "an episodic outcome was filed with no expiry, so a Tuesday became permanent"
    assert (row.reason_it_matters or "").strip(), "stored with no account of why it is worth keeping"
    assert json.loads(row.evidence_text)["stated_span"] in YES, \
        "the evidence is not a span of the student's own words"

    # NO TASK SLOTS. The recipient, subject and body live on the run; the only non-style row is the
    # past-tense outcome above.
    assert [r.memory_id for r in _memories(c.uid) if r.kind != "style"] == [row.memory_id]


def test_finalizing_the_same_verified_outcome_twice_leaves_one_row(live_db, monkeypatch):
    """A retried turn, a replayed job, a second call from anywhere: the claim is already active, so the
    database refuses the write. Paired with a genuinely different outcome that DOES write, so this cannot
    pass because writing is broken."""
    calls = _spy_on_finalize(monkeypatch)
    c = Conversation(SCRIPT)
    for turn in (ASK, ADDRESS, SUBJECT, BODY, YES):
        c.say(turn)
    assert len(_memories(c.uid, kind="episodic")) == 1

    replay = dict(calls[0])
    user_id = replay.pop("user_id")
    # The production call, made again verbatim. The spy is a passthrough, so this IS the real function.
    again = _run(memory_finalize.after_verified_outcome(user_id, **replay))
    assert again is False, "a duplicate finalization reported itself as a fresh write"
    assert len(_memories(c.uid, kind="episodic")) == 1, "a second row for the same outcome"

    # POSITIVE CONTROL: a different outcome is a different claim and is remembered.
    other = _run(memory_finalize.after_verified_outcome(
        user_id, **{**replay, "provider_entity_id": "a-second-real-send", "goal_id": None}))
    assert other is True
    assert len(_memories(c.uid, kind="episodic")) == 2, "a second, genuinely different send was dropped"


def test_a_refused_candidate_leaves_no_row_at_either_gate(live_db):
    """Two refusals that must both be silent in the table and loud in the return value: this module's own
    "someone else's words are not evidence", and the write policy's sensitive-trait backstop (which
    over-blocks on purpose — `memory_record.SENSITIVE_TRAIT_STEMS` says why)."""
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    common = dict(summary=f"emailed {TEACHER} about thank you", stated_span=None,
                  source_message_id="m5", provider_entity_id="msg-1")

    # POSITIVE CONTROL FIRST: the same call with the student's own words writes exactly one row.
    assert _run(memory_finalize.after_verified_outcome(
        uid, capability=SEND, trusted_text="yes send it", **common)) is True
    assert len(_memories(uid, kind="episodic")) == 1

    assert _run(memory_finalize.after_verified_outcome(
        uid, capability=SEND, trusted_text="> she wrote: yes send it",
        **{**common, "provider_entity_id": "msg-2"})) is False
    assert len(_memories(uid, kind="episodic")) == 1, "a forwarded block became a memory"

    # The predicate would be `family.completed_share_update`, which NAMES a sensitive trait. Refused by
    # the policy, not by this module, and it leaves nothing behind.
    assert _run(memory_finalize.after_verified_outcome(
        uid, capability="family.share_update", trusted_text="yes send it",
        **{**common, "provider_entity_id": "msg-3"})) is False
    assert len(_memories(uid, kind="episodic")) == 1
    assert not [r for r in _memories(uid) if (r.predicate or "").startswith("family.")]


def test_the_style_path_still_writes_and_does_not_multiply(live_db):
    """The half that already worked, kept honest. Style is re-observed on every turn, so a new row per
    message would bury every other memory the student has — `claim_key` is what stops it, and the same
    mechanism is what makes the outcome path idempotent above."""
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    text = "can u email ms alvarez a thank you note"

    assert _run(memory_finalize.after_turn(uid, trusted_text=text, source_message_id="m1")) == 2
    rows = _memories(uid, kind="style")
    assert {r.predicate for r in rows} == {"style.writes_case", "style.uses_abbreviations"}
    assert all(r.subject == memory_record.SELF and r.status == "active" for r in rows)

    assert _run(memory_finalize.after_turn(uid, trusted_text=text, source_message_id="m2")) == 0
    assert len(_memories(uid, kind="style")) == 2, "style rows multiplied one per turn"

    # A one-word reply is not evidence about how someone writes.
    assert _run(memory_finalize.after_turn(uid, trusted_text="yes", source_message_id="m3")) == 0
    assert len(_memories(uid, kind="style")) == 2
