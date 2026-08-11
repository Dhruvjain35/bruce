"""WHAT THE SHADOW ROW IS ALLOWED TO HOLD, AND WHAT THE SHADOW IS ALLOWED TO DO.

Two rules, both structural, both previously broken in a way nothing failed on.

PRIVACY. "No turn text lives on the job" was true of the columns and FALSE of the JSONB. The observation
stored the model's `missing_information` and the executive's `validation_notes` verbatim. Both are model
prose: the model is asked for an operation id and is free to answer with a sentence, and it is free to
say WHOSE address is missing. So a telemetry table that no deletion reaches was accumulating names,
addresses and the substance of students' messages — in the one table built specifically so that would
never happen. The rule now is positive rather than a blocklist: booleans, counts, latencies, ids from
Bruce's own registry, and labels from a vocabulary this repo owns. Nothing a model wrote.

ISOLATION. The shadow observes and writes ONE row. It creates no goal, no Decision, no authorization
evidence, no execution attempt, no provider mutation and no alternate reply. `scripts/run_gates.py`
proves the imports are absent, which is the strongest available STATIC argument; this file is the
behavioural half — a full observation runs against real Postgres and every table the observer must not
touch is counted afterwards.

The privacy tests are deliberately adversarial: the reader hands back exactly the payload that used to
leak, and the assertion is that the student's own words cannot be found anywhere in what was persisted.
"""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import semantic_executive, semantic_shadow, tool_registry
from bruce_engine.db import user_session, worker_session
from bruce_engine.repositories import PostgresUserRepository
from bruce_engine.semantic_contracts import (Actionability, DecisionPolarity, Family, GoalCount,
                                             OperationFamily, SemanticTurn, TurnRole)

users = PostgresUserRepository()
CHANNEL, PMID = "imessage", "pm-privacy"

# The student's turn, and the things inside it that must never reach the telemetry table.
TEXT = "yeah go ahead and send it to mrs patel at patel@westhigh.edu about the retake"
SECRETS = ("patel@westhigh.edu", "mrs patel", "the retake", "westhigh")


class _Decision:
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"


class _LeakyReader:
    """A reading carrying exactly what used to be copied onto the job row.

    `missing_information` and `target_entities` are MODEL OUTPUT — free text, in the model's words, about
    the student's message. Under the previous `as_json` the first of these was stored verbatim.
    """
    provider, model = "fake", "leaky"

    async def read(self, body):
        return SemanticTurn(
            turn_role=TurnRole.decision_response, actionability=Actionability.executable,
            decision_polarity=DecisionPolarity.approve, goal_count=GoalCount.one,
            domain_candidates=(Family.communication,), operation_family=OperationFamily.send,
            target_entities=("mrs patel",),
            missing_information=("mrs patel's email address: patel@westhigh.edu",
                                 "what the retake message should say"),
            confidence=0.5)


# What THIS student could actually run at the moment of the turn — the per-user broker's answer, captured
# at the request boundary and persisted with the job. Never `tool_registry.specs(None)`: that is a global
# table identical for every user, and reading it as capability truth recorded false capability denials
# against a router that had correctly told an unconnected student Bruce has no hands.
REACHABLE = semantic_shadow.ReachableOperations(
    frozenset({"gmail.send_message", "calendar.create_event"}), established=True)


def _observed_row(uid: UUID, reader) -> tuple:
    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID, text=TEXT)
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), reachable=REACHABLE, store=store,
                                      tally=semantic_shadow.EnqueueLedger())
        observed = await semantic_shadow.claim_and_observe(worker_id="w1", store=store, triage=reader)
        return observed, next(iter(store.jobs.values()))

    return asyncio.run(_go())


# --- what may be persisted ---------------------------------------------------------------------------

def test_nothing_the_student_or_the_model_wrote_reaches_the_stored_observation(monkeypatch):
    """The whole persisted payload, searched for the student's own words.

    Asserted over the SERIALIZED row rather than field by field: a field-by-field check only covers the
    fields someone remembered, and the leak that shipped was in a field everyone had read past.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    uid = uuid4()

    observed, row = _observed_row(uid, _LeakyReader())
    assert observed.outcome == semantic_shadow.OK, "the reading did not land, so nothing was persisted"

    blob = json.dumps(row.observation).lower()
    for secret in SECRETS:
        assert secret not in blob, (
            f"{secret!r} was persisted to the shadow row — this table exists precisely so a second copy "
            f"of the student's words does not survive in a place a deletion never reaches")
    assert TEXT.lower() not in blob
    assert "@" not in blob, "an address-shaped string reached the telemetry row"

    # And the same rule on the short error column, which is a typed reason and never a message.
    assert row.last_error in (None, *semantic_shadow.HEALTH_BUCKETS)


def test_the_observation_stores_a_count_instead_of_what_was_missing(monkeypatch):
    """`missing_information` answers "could the executive proceed", and the COUNT answers that.

    The strings answer it too, and additionally say whose address Bruce does not have. Only one of those
    two facts is needed to decide whether the executive may be given authority.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    uid = uuid4()

    _observed, row = _observed_row(uid, _LeakyReader())
    exec_block = row.observation["exec"]

    assert exec_block["missing_count"] == 2, "the count of missing information was lost"
    assert "missing" not in exec_block, "the raw missing-information strings are still stored"
    assert all(isinstance(v, (str, int, float, bool, type(None), list))
               for v in exec_block.values())


def test_validation_reasons_are_stored_as_a_closed_vocabulary(monkeypatch):
    """Codes, not notes.

    The notes interpolate whatever the model proposed — an operation id that is really a sentence, a goal
    id, an address — so persisting them makes the row's contents depend on model output. Codes come from
    constants in this repo, so what CAN be stored is a fixed list somebody chose.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    uid = uuid4()

    _observed, row = _observed_row(uid, _LeakyReader())
    codes = row.observation["exec"]["validation_codes"]
    vocabulary = {v for k, v in vars(semantic_executive).items()
                  if k.startswith("CODE_") and isinstance(v, str)}

    assert "notes" not in row.observation["exec"], "the prose validation notes are still persisted"
    assert codes, ("this reading was downgraded — an affirm below the confidence floor — and the row "
                   "records no reason at all, so the shadow can no longer say WHY a turn was downgraded")
    assert set(codes) <= vocabulary, (
        f"{sorted(set(codes) - vocabulary)} is not a code this repo defines — anything outside the "
        f"vocabulary is text of unknown origin")
    assert semantic_executive.CODE_AFFIRM_BELOW_FLOOR in codes


# --- what the shadow may DO --------------------------------------------------------------------------

@pytest.fixture()
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None; db._sessionmaker = None
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.delenv("BRUCE_ROUTER_SEMANTIC", raising=False)
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    yield
    db._engine = None; db._sessionmaker = None


# Everything an observer must never create. Counted after a full observation, because "it cannot, it has
# no import" is an argument about the code and this is a statement about the database.
FORBIDDEN_TABLES = ("agent_runs", "agent_run_events", "authorization_evidence", "authorization_refusals",
                    "outbound_messages", "memory_records", "event_candidates", "missions")


async def _count(uid: UUID, table: str) -> int:
    """Counted under the OWNER's session, not the worker's.

    Not a detail: most of these tables are tenant-only, so a worker session sees zero rows in them
    whatever they contain, and every assertion below would pass vacuously. Anything the shadow created
    would be owned by this user, so this is the session that can actually see it — and the
    conversation_turns count in the same session is the control proving the session sees anything at all.
    """
    async with user_session(uid) as s:
        return int((await s.execute(sa_text(f"SELECT count(*) FROM {table}"))).scalar())


def test_a_full_observation_creates_nothing_but_its_own_row(clean_db, _pg):
    """One turn, read end to end against real Postgres, and then everything else is counted.

    The reading deliberately DERIVES A WRITE (`gmail.send_message` on an approved decision response): if
    the observer were ever going to create a goal, an authorization or a provider attempt, this is the
    turn that would do it. Nothing may exist afterwards except the shadow job row itself.

    The turn is enqueued with a REACHABLE SNAPSHOT that includes the send, because that is what makes the
    reading derive a capability at all: understanding may not name an operation this user cannot reach.
    Passing it explicitly is also the point — the snapshot belongs to the turn, and a test that relied on
    the global live registry here would be re-asserting the defect this suite's sibling file describes.
    """
    uid = uuid4()
    asyncio.run(users.ensure(uid, auth_provider="test"))

    class _ReadsAWrite:
        provider, model = "fake", "write"

        async def read(self, body):
            return SemanticTurn(turn_role=TurnRole.new_goal, actionability=Actionability.executable,
                                domain_candidates=(Family.communication,),
                                operation_family=OperationFamily.send, goal_count=GoalCount.one,
                                confidence=0.95)

    async def _go():
        async with user_session(uid) as s:
            # RETURNING the canonical turn id, because that id is what the shadow row now references and
            # what the reconciliation joins on. A hand-written turn whose id never reached `intake` would
            # reconcile as UNOBSERVED — correctly, which is why it has to be carried here too.
            turn_id = (await s.execute(sa_text(
                "INSERT INTO conversation_turns (user_id, channel, channel_identity, "
                "provider_message_id, role, text) VALUES (:uid, :ch, '+15550001111', :pmid, 'user', :t) "
                "RETURNING id"),
                {"uid": str(uid), "ch": CHANNEL, "pmid": PMID, "t": TEXT})).scalar()
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                      decision=_Decision(), reachable=REACHABLE,
                                      conversation_turn_id=turn_id,
                                      tally=semantic_shadow.EnqueueLedger())
        observed = await semantic_shadow.claim_and_observe(worker_id="w1", triage=_ReadsAWrite())
        after = {t: await _count(uid, t) for t in FORBIDDEN_TABLES}
        turns = await _count(uid, "conversation_turns")
        reconciliation = await semantic_shadow.reconcile(uid)
        async with worker_session() as s:
            row = dict((await s.execute(sa_text(
                f"SELECT status, outcome, observation, agrees, divergence, false_capability_denial, "
                f"reachable_operations, intake_disposition, last_error "
                f"FROM {semantic_shadow.TABLE} WHERE user_id = :uid"),
                {"uid": str(uid)})).mappings().one())
        return observed, after, row, turns, reconciliation

    observed, after, row, turns, reconciliation = asyncio.run(_go())

    assert observed.outcome == semantic_shadow.OK and observed.recorded is True, (
        "the observation did not complete, so this test proves nothing about what it refrained from")
    assert row["status"] == semantic_shadow.COMPLETED
    assert row["observation"]["exec"]["operation"] == "gmail.send_message", (
        "the reading did not derive a write — the strongest version of this test is the turn that would "
        "have created a goal and an authorization if the observer could create anything")
    for table, n in after.items():
        assert n == 0, (
            f"the shadow observation created {n} row(s) in {table} — an observer that can act is not a "
            f"shadow, and every metric collected under it becomes unsafe to trust")
    assert turns == 1, "the shadow wrote back to the conversation, or duplicated the student's turn"
    assert reconciliation["reconciled"] is True and reconciliation["unobserved"] == 0, (
        "the one trusted turn on this account was not accounted for against the turn ledger")

    blob = json.dumps(row["observation"]).lower()
    for secret in SECRETS:
        assert secret not in blob, f"{secret!r} reached the durable row on the real database path"
    assert row["last_error"] is None, "a successful observation recorded an error string"

    # THE PERSISTED SNAPSHOT IS REGISTRY IDS AND NOTHING ELSE. It is the one new column that carries a
    # list of strings, so it gets the same treatment as the observation: names from Bruce's own registry,
    # never anything a student or a model wrote.
    assert sorted(row["reachable_operations"]) == sorted(REACHABLE.operations)
    assert row["intake_disposition"] == semantic_shadow.ELIGIBLE
    snapshot_blob = json.dumps(row["reachable_operations"]).lower()
    for secret in SECRETS:
        assert secret not in snapshot_blob
    registered = {s.capability for s in tool_registry.specs(None)}
    assert set(row["reachable_operations"]) <= registered, (
        "the capability snapshot holds a string that is not a registry id")
