"""WHICH TURNS SHADOW ACTUALLY SEES, AND THE LEDGER THAT PROVES IT.

THE DEFECT. `semantic_shadow.enqueue` was called from exactly one place: inside the `else` of
`if resolved_continuation:` in `conversation_runtime`. So every resolved continuation and every ambiguous
goal selection skipped shadow completely — no disposition, no row, no trace. That is the approve /
reject / cancel / continuation population: the turns where Bruce actually acts, and precisely the turns an
authority decision must be made on. A sample missing them over-represents easy turns in exactly the
direction that argues FOR granting the executive authority.

AND NOTHING COULD NOTICE. The intake counter defined `eligible` as the sum of the dispositions it had
recorded, so `eligible == enqueued + everything else` was an identity — true by construction, and true
the entire time an entire population was invisible to it. A ledger whose denominator is its own numerator
cannot fail.

So this file asserts two different things, and the second is the one that matters:

  COVERAGE       every trusted inbound turn of a real conversation — the ask, the amendment, the
                 approval — leaves exactly one shadow row, whichever production lane answered it
  RECONCILIATION the count comes from `conversation_turns`, which shadow does not write, so a turn that
                 never reached intake is a GAP rather than an invariant agreeing with itself

The regression test at the bottom puts the call back inside the non-continuation branch and proves the
reconciliation FAILS. Without it, "reconciled is True" is a claim no experiment has ever contradicted.

Everything here runs against real Postgres through the real `conversation_runtime.handle`, because the
defect was a call site in that function: an in-memory reconstruction of the branch structure would prove
something about the reconstruction.
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (conversation_graph, conversation_outcomes, conversation_runtime, crypto,
                          gmail_adapter, goal_handler, oauth_google, schema, semantic_shadow,
                          world_state)
from bruce_engine.conversation_contract import (ConversationDecision, ExtractedEntity, IntentKind,
                                                ResponseType, RiskLevel)
from bruce_engine.conversation_model import ReasonResult
from bruce_engine.db import user_session
from bruce_engine.messaging import ChannelKind, FakeChannel, InboundMessage
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

PHONE = "+15550188"
ACCOUNT = "student@example.com"
TEACHER = "alvarez@school.edu"
CAL = "https://www.googleapis.com/auth/calendar.events"
GSEND = "https://www.googleapis.com/auth/gmail.send"
GREAD = "https://www.googleapis.com/auth/gmail.readonly"
TZ = "America/Chicago"
CHANNEL = ChannelKind.self_hosted_imessage.value

# The founder's sequence, shortened to the three turns that exercise the three lanes: a routed ask, a
# continuation that amends work in flight, and an approval that spends the pending Decision. The last two
# are the ones that used to leave no trace at all.
T_ASK = "can u email alvarez@school.edu a thank you note for the rec letter, make it heartfelt"
T_AMEND = "make it professional and send it"
T_APPROVE = "YES WRITE IT AND SEND IT"


def _run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(
        db, "create_async_engine",
        lambda url, **kw: (kw.pop("poolclass", None),
                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    # AUTHORITY STAYS OFF. This whole file is about what shadow OBSERVES; the moment the executive can
    # decide, none of these rows are evidence about anything.
    monkeypatch.delenv("BRUCE_ROUTER_SEMANTIC", raising=False)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _decision(intent, *, rt=ResponseType.direct_answer, text="ok", caps=None, entities=None,
              proposed_goal=None):
    return ConversationDecision(
        intent=intent, response_type=rt, user_visible_response=text,
        extracted_entities=entities or [], required_capabilities=caps or [],
        needs_mission=False, proposed_goal=proposed_goal, risk_level=RiskLevel.none, confidence=0.9)


def _entity(kind, value, normalized=None):
    return ExtractedEntity(type=kind, value=value, normalized=normalized)


SCRIPT = {
    T_ASK: _decision(IntentKind.actionable, caps=["gmail.send_message"],
                     proposed_goal="email ms alvarez a thank-you note",
                     text="sure, what should the subject line be?",
                     entities=[_entity("recipient_email", TEACHER),
                               _entity("purpose", "thank her for the recommendation letter"),
                               _entity("tone", "heartfelt")]),
    T_AMEND: _decision(IntentKind.clarification, text="ok, here's what i've got",
                       entities=[_entity("subject", "thank you"),
                                 _entity("body_text", "thank you for writing my letter.")]),
    T_APPROVE: _decision(IntentKind.approval, text="ok"),
}


class _Scripted:
    provider = model = "fake"
    supports_vision = True

    def __init__(self, script):
        self.script = script

    async def decide(self, *, text, images, context):
        return ReasonResult(
            decision=self.script.get((text or "").strip(),
                                     _decision(IntentKind.clarification, text="ok")),
            provider="fake", model="fake", input_tokens=0, output_tokens=0, latency_ms=1)


async def _seed(uid, *, connected: bool = True):
    await users.ensure(uid, auth_provider="test")
    if connected:
        async with user_session(uid) as s:
            s.add(schema.Integration(
                user_id=uid, provider=oauth_google.PROVIDER, provider_account_id=ACCOUNT,
                scopes=[CAL, GSEND, GREAD], refresh_token_encrypted=crypto.encrypt("rt"),
                selected_calendar_id="primary", status="connected"))
    await world_state.set_timezone(uid, TZ, source="user_stated")


class Conversation:
    """One student, one thread, N turns through the REAL runtime — the same harness shape the acceptance
    suite uses, because the call site under test only exists inside `conversation_runtime.handle`."""

    def __init__(self, *, connected: bool = True):
        self.uid = uuid4()
        _run(_seed(self.uid, connected=connected))
        self.gmail = gmail_adapter.FakeGmailAdapter(account=ACCOUNT)
        self.reasoner = _Scripted(SCRIPT)
        self.channel = FakeChannel()
        self.n = 0
        self.tag = uuid4().hex[:8]

    def _handlers(self):
        return [goal_handler.GoalHandler(adapter=self.gmail) if h.name == "goal" else h
                for h in conversation_outcomes.default_handlers()]

    def _inbound(self, text, pmid, *, reply_to=None):
        return InboundMessage(provider_message_id=pmid, channel=ChannelKind.self_hosted_imessage,
                              channel_identity=PHONE, text=text, attachments=[],
                              timestamp=datetime.datetime.now(datetime.timezone.utc), is_group=False,
                              reply_to_message_id=reply_to)

    def say(self, text: str, *, pmid: str | None = None) -> str:
        self.n += 1
        msg = self._inbound(text, pmid or f"{self.tag}-m{self.n}")
        msg.user_id = self.uid
        _run(conversation_graph.ingest_inbound_message(msg))
        out = _run(conversation_runtime.handle(self.channel, msg, user_id=self.uid,
                                               reply_target=PHONE, reasoner=self.reasoner,
                                               handlers=self._handlers()))
        return out.status

    # --- what the database holds afterwards ---------------------------------------------------------

    def shadow_rows(self) -> list[dict]:
        async def _read():
            async with user_session(self.uid) as s:
                rows = (await s.execute(sa_text(
                    "SELECT provider_message_id, status, intake_disposition, router_snapshot, "
                    "reachable_operations FROM semantic_shadow_jobs "
                    "WHERE user_id = :uid ORDER BY created_at"), {"uid": str(self.uid)})).mappings().all()
            return [dict(r) for r in rows]
        return _run(_read())

    def turn_count(self) -> int:
        async def _read():
            async with user_session(self.uid) as s:
                return int((await s.execute(sa_text(
                    "SELECT count(*) FROM conversation_turns WHERE user_id = :uid AND role = 'user'"),
                    {"uid": str(self.uid)})).scalar())
        return _run(_read())

    def reconcile(self) -> dict:
        return _run(semantic_shadow.reconcile(self.uid))

    def sent(self) -> int:
        return self.gmail.send_calls

    def runs(self) -> int:
        async def _read():
            async with user_session(self.uid) as s:
                return int((await s.execute(sa_text(
                    "SELECT count(*) FROM agent_runs WHERE user_id = :uid"),
                    {"uid": str(self.uid)})).scalar())
        return _run(_read())


def _paths(rows) -> list[str]:
    return [(r["router_snapshot"] or {}).get("production_path") for r in rows]


# --- coverage ----------------------------------------------------------------------------------------

def test_every_trusted_turn_is_observed_including_the_continuation_and_the_approval(clean_db):
    """THE POPULATION THAT WAS MISSING, on a real three-turn conversation.

    The ask is routed. The amendment lands on work in flight and never reaches the classifier. The
    approval spends the pending Decision. Before this change only the FIRST of those three left a row,
    and the two that were dropped are the ones where Bruce acts.
    """
    c = Conversation()
    for text in (T_ASK, T_AMEND, T_APPROVE):
        assert c.say(text) == "processed"

    rows = c.shadow_rows()
    paths = _paths(rows)

    assert c.turn_count() == 3, "the harness did not produce three trusted turns"
    assert len(rows) == 3, (
        f"{len(rows)} shadow rows for 3 trusted turns — the turns answered from state were never offered "
        f"for observation at all, which is the population an authority decision most depends on")
    assert all(r["intake_disposition"] == semantic_shadow.ELIGIBLE for r in rows)
    assert all(r["status"] == semantic_shadow.PENDING for r in rows), (
        "an eligible turn was written in a status no worker will claim")
    assert semantic_shadow.PATH_ROUTED in paths, "no turn was recorded as routed"
    assert any(p in (semantic_shadow.PATH_CONTINUATION, semantic_shadow.PATH_GOAL_AMBIGUOUS)
               for p in paths), (
        "no turn recorded a work-in-flight lane, so this test is not exercising the population it is "
        "about — either the conversation no longer resolves a continuation, or the lane label was lost")


def test_observing_the_continuation_population_changes_nothing_about_it(clean_db, monkeypatch):
    """Widening the sample must not widen the blast radius.

    The continuation and the approval are exactly the turns where an observer with side effects would do
    real damage — a second goal, a second send — and they are turns the observer had never touched
    before. So the same three turns are run TWICE, once with shadow on and once with it off, and the
    durable outcome of the conversation must be identical: the same runs, the same single send, the same
    replies. A before/after comparison inside one conversation could not show this, because production
    legitimately acts on these turns; the control is the same conversation without an observer.
    """
    def _drive():
        c = Conversation()
        for text in (T_ASK, T_AMEND, T_APPROVE):
            assert c.say(text) == "processed"
        return c

    observed = _drive()

    monkeypatch.delenv("BRUCE_SEMANTIC_SHADOW", raising=False)
    control = _drive()

    assert observed.runs() == control.runs(), (
        "the conversation produced a different number of durable runs with the observer attached")
    assert observed.sent() == control.sent() == 1, "the approved send did not happen exactly once"
    assert observed.turn_count() == control.turn_count() == 3
    assert len(observed.shadow_rows()) == 3
    assert control.shadow_rows() == [], "the kill switch was off and rows were written anyway"
    assert observed.reconcile()["reconciled"] is True


def test_capability_truth_is_persisted_per_user_on_every_observed_turn(clean_db):
    """The snapshot is a fact about THIS account, taken at the request boundary and stored with the turn.

    Two students say the same words in the same minute; one connected Google and one never did. Under the
    global live registry both rows would carry the same nine capabilities, and the unconnected student's
    turn would go on to record a false capability denial against a router that told them the truth.
    """
    connected = Conversation(connected=True)
    disconnected = Conversation(connected=False)
    connected.say(T_ASK)
    disconnected.say(T_ASK)

    a = connected.shadow_rows()[0]["reachable_operations"]
    b = disconnected.shadow_rows()[0]["reachable_operations"]

    assert a is not None and b is not None, "capability truth was not established for a healthy probe"
    assert "gmail.send_message" in a, "a connected, send-scoped student cannot reach the send"
    assert b == [], "a student with no Google connection was recorded as able to reach live capabilities"
    assert a != b, (
        "both students got the same reachable set — that is what the global registry returns for "
        "everybody, and it is the defect this column exists to remove")


def test_a_redelivered_turn_is_observed_once(clean_db):
    """A webhook redelivery is a duplicate OBSERVATION if it lands, not just a duplicate row: the same
    turn would be counted twice in the agreement rate the authority decision reads."""
    c = Conversation()
    c.say(T_ASK, pmid="pm-same")
    c.say(T_ASK, pmid="pm-same")

    assert len(c.shadow_rows()) == 1, "one turn produced two observations"
    assert c.reconcile()["reconciled"] is True


# --- exclusions --------------------------------------------------------------------------------------

def test_a_turn_with_no_student_words_is_excluded_with_a_typed_persisted_reason(clean_db):
    """A transport/system event still has to be ACCOUNTED for.

    It is not eligible — there is nothing for a language reader to read — but an exclusion that leaves no
    trace is indistinguishable from a turn that was dropped, and telling those two apart is the only
    thing the reconciliation can do. So the row exists, in a status no worker will ever claim, carrying
    the reason.
    """
    c = Conversation()
    c.say("")

    rows = c.shadow_rows()
    assert len(rows) == 1, "an excluded turn left no trace at all"
    assert rows[0]["status"] == semantic_shadow.EXCLUDED
    assert rows[0]["intake_disposition"] == semantic_shadow.EXCLUDED_NO_TRUSTED_TEXT
    assert rows[0]["reachable_operations"] is None, (
        "capability truth was recorded for a turn nobody will ever read")

    r = c.reconcile()
    assert r["reconciled"] is True and r["excluded"] == 1 and r["eligible"] == 0
    assert r["by_exclusion"] == {semantic_shadow.EXCLUDED_NO_TRUSTED_TEXT: 1}


def test_a_turn_that_is_only_someone_elses_words_is_excluded_as_untrusted(clean_db):
    """The exclusion that is a privacy rule rather than a coverage one.

    This message is nothing but a forwarded block: the student typed no words at all. The worker reads
    `conversation_turns.text` RAW, so observing it would hand a stranger's sentences to the executive as
    if the student had typed them — the exact substitution `input_envelope` exists to prevent, and one
    this repository has already shipped twice. A telemetry path does not get an exemption from it.

    It also proves the runtime hands `classify_turn` the TRUSTED text: this message has plenty of
    characters in `msg.text`, so a classifier reading the raw body would call it eligible.
    """
    c = Conversation()
    c.say("--- forwarded message ---\nfrom: coach@school.edu\nyes go ahead and add it to the calendar")

    rows = c.shadow_rows()
    assert len(rows) == 1 and rows[0]["status"] == semantic_shadow.EXCLUDED
    assert rows[0]["intake_disposition"] == semantic_shadow.EXCLUDED_UNTRUSTED_ONLY, (
        "a turn containing none of the student's own words was queued for observation — the reader would "
        "have been handed a forwarded 'yes go ahead' as if the student had said it")
    assert c.reconcile()["reconciled"] is True


def test_an_excluded_turn_is_never_claimed_by_the_worker(clean_db):
    """The status has to be more than a label. A worker that claimed an excluded row would read the turn
    it was excluded for — which for `excluded_untrusted_only` means handing a stranger's forwarded words
    to the executive as if the student had typed them."""
    c = Conversation()
    c.say("")

    observed = _run(semantic_shadow.claim_and_observe(worker_id="w1"))
    assert observed is None, "the worker claimed a turn that was explicitly excluded from observation"


# --- the independent reconciliation ------------------------------------------------------------------

def test_reconciliation_counts_turns_from_the_ledger_shadow_does_not_write(clean_db):
    """The denominator has to come from somewhere else, and this is the proof that it does.

    A trusted turn is inserted into `conversation_turns` with no shadow row behind it — the exact residue
    a missing intake call leaves. The in-process counter cannot see it (nothing was offered), so if
    reconciliation still says everything adds up, it is reading its own bookkeeping.
    """
    c = Conversation()
    c.say(T_ASK)

    async def _orphan():
        async with user_session(c.uid) as s:
            await s.execute(sa_text(
                "INSERT INTO conversation_turns (user_id, channel, channel_identity, "
                "provider_message_id, role, text) "
                "VALUES (:uid, :ch, :ident, 'pm-never-offered', 'user', 'hey did u see this')"),
                {"uid": str(c.uid), "ch": CHANNEL, "ident": PHONE})
    _run(_orphan())

    r = c.reconcile()
    assert r["trusted_inbound_turns"] == 2
    assert r["eligible"] == 1 and r["excluded"] == 0
    assert r["unobserved"] == 1, "a turn with no shadow row of any kind was not reported as a gap"
    assert r["reconciled"] is False, (
        "the reconciliation passed while a trusted turn had no disposition anywhere — which is exactly "
        "the state the whole continuation population was in, undetected")


def test_moving_intake_back_inside_the_routing_branch_fails_reconciliation(clean_db, monkeypatch):
    """THE REGRESSION TEST FOR THE ORIGINAL DEFECT, reproduced as behaviour rather than described.

    The old code called shadow only from the `else` of `if resolved_continuation:`. That is reproduced
    here by making `intake` a no-op for exactly the turns that lane skipped — the ones production
    answered from state — and driving the same three-turn conversation through the real runtime.

    If the reconciliation is real, it fails. If it is the old sum-of-my-own-dispositions identity, it
    passes and this test is the thing that says so.
    """
    real_intake = semantic_shadow.intake

    async def _routed_only(user_id, **kw):
        # The old call site, in one line: a turn production answered from state never reached shadow.
        path = getattr(kw.get("decision"), "production_path", semantic_shadow.PATH_ROUTED)
        if path != semantic_shadow.PATH_ROUTED:
            return False
        return await real_intake(user_id, **kw)

    monkeypatch.setattr(conversation_runtime.semantic_shadow, "intake", _routed_only)

    c = Conversation()
    for text in (T_ASK, T_AMEND, T_APPROVE):
        c.say(text)

    rows = c.shadow_rows()
    r = c.reconcile()

    assert c.turn_count() == 3, "the harness did not produce three trusted turns"
    assert len(rows) < 3, (
        "the reproduction did not actually skip anything — every turn took the routed lane, so this test "
        "is not exercising the defect it exists to catch")
    assert r["unobserved"] == 3 - len(rows)
    assert r["reconciled"] is False, (
        "turns answered from state were dropped by intake and the reconciliation still reported that "
        "everything added up — which is what shipped: an invariant whose denominator was the sum of its "
        "own numerators cannot fail, and it did not, for the entire safety-critical population")
