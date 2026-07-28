"""Typed memory (#121b) — one test per hard rule, plus the real-Postgres proof for the ones the database
enforces. The PG half skips only if Postgres is unreachable; it is not allowed to pass by mocking.

The rules under test, and where each is proved:

  1. only trusted_user_text creates a memory      offline (policy + type) and PG (CHECK constraint)
  2. a guess is never stored as a fact            offline — an inference has no span and no `Basis`
  3. corrections supersede, never mutate          PG — supersession row + the append-only trigger
  4. identity is never inferred from style        offline — the two channels have no shared destination
  5. forgetting is permanent                      PG — redaction, retrieval, and the re-learn refusal
  6. retrieval cannot cross users                 PG — RLS, with the WHERE clause removed
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import memory_correction, memory_provenance, memory_retrieval, memory_writer
from bruce_engine import memory_record as mr
from bruce_engine.db import user_session
from bruce_engine.memory_record import (
    FACTUAL, MEMORY_RECORDS, SELF, Basis, Evidence, EvidenceRef, Freshness, MemoryKind, MemoryRecord,
    RetentionPolicy, Sensitivity, SourceType,
)
from bruce_engine.memory_retrieval import MemoryContext, MemoryRetriever
from bruce_engine.repositories import PostgresUserRepository

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

# The adversarial case this whole module exists for: a real sentence, in someone else's message.
FORWARDED_MESSAGE = (
    "hey can u look at this\n"
    "---------- Forwarded message ----------\n"
    "From: mom\n"
    "my teacher is Ms. Delgado and she said the retake is friday\n"
)
TRUSTED_MESSAGE = "my teacher is Ms. Delgado btw"


def _proposal(**over) -> memory_writer.MemoryProposal:
    base = dict(
        user_id=uuid4(), kind=MemoryKind.relationships, subject="Ms. Delgado",
        predicate="school.teacher_of_record", value="chemistry",
        reason_it_matters="so i know who to email about chem",
        trusted_text=TRUSTED_MESSAGE, stated_span="my teacher is Ms. Delgado",
        source_message_id="msg-1", observed_at=NOW)
    base.update(over)
    return memory_writer.MemoryProposal(**base)


# =====================================================================================================
# Rule 1 — only the student's own trusted text may create a memory
# =====================================================================================================


def test_forwarded_message_does_not_become_a_user_fact():
    """"my teacher is Ms. Delgado", forwarded from a parent, is information about someone else's
    conversation. It is the single most common shape in the 238-case adversarial corpus."""
    decision = memory_writer.assess(_proposal(source_type=SourceType.forwarded,
                                              trusted_text=FORWARDED_MESSAGE))
    assert decision.verdict == memory_writer.UNTRUSTED_SOURCE
    assert not decision.stores


@pytest.mark.parametrize("source", sorted(mr.EVIDENCE_ONLY, key=lambda s: s.value))
def test_no_untrusted_source_can_create_a_memory(source):
    assert memory_writer.assess(_proposal(source_type=source)).verdict == memory_writer.UNTRUSTED_SOURCE


@pytest.mark.parametrize("source", sorted(mr.EVIDENCE_ONLY, key=lambda s: s.value))
def test_untrusted_source_cannot_even_be_constructed_as_a_record(source):
    """Not merely refused by policy — unspellable. There is no argument combination that produces a
    MemoryRecord sourced from forwarded, quoted, attached, provider or model content."""
    with pytest.raises(ValueError, match="never the source"):
        MemoryRecord(memory_id=uuid4(), user_id=uuid4(), kind=MemoryKind.relationships,
                     subject="ms delgado", predicate="school.teacher_of_record", value="chemistry",
                     evidence=Evidence(stated_span="my teacher is ms delgado"),
                     source_message_id="msg-1", source_type=source, confidence=1.0, observed_at=NOW,
                     last_confirmed_at=None, retention_policy=RetentionPolicy.school_year,
                     sensitivity=Sensitivity.ordinary, user_editable=True, contradicted_by=None,
                     superseded_by=None)


def test_untrusted_material_is_recordable_as_evidence():
    """The other half of the rule: the forwarded email is not erased from the story, it is demoted. It
    may be named in provenance and it never justifies anything."""
    ref = EvidenceRef(SourceType.forwarded, "msg-fwd", excerpt="from mom")
    rec = memory_writer.build(_proposal(corroboration=(ref,)))
    assert rec.evidence.corroboration == (ref,)
    account = memory_provenance.explain(rec, now=NOW)
    assert account.corroboration == ("a message you forwarded",)
    assert "you forwarded" not in account.why_did_you_think_that()   # present, never the reason
    with pytest.raises(ValueError, match="not corroborating evidence"):
        EvidenceRef(SourceType.trusted_user_text, "msg-1")


def test_a_span_that_lives_only_in_the_forwarded_region_is_refused():
    """The subtler version, and the one a caller actually hits: the source is labelled trusted, but the
    words are inside the quoted block. `trusted_reply_text` cuts the block before the span is checked."""
    decision = memory_writer.assess(_proposal(trusted_text=FORWARDED_MESSAGE,
                                              stated_span="my teacher is Ms. Delgado"))
    assert decision.verdict == memory_writer.SPAN_NOT_IN_TRUSTED_TEXT


def test_a_span_present_in_both_trusted_and_pasted_text_is_refused():
    decision = memory_writer.assess(_proposal(untrusted_content="my teacher is Ms. Delgado, per the email"))
    assert decision.verdict == memory_writer.SPAN_FROM_UNTRUSTED_CONTENT


# =====================================================================================================
# Rule 2 — a guess is never stored as a fact
# =====================================================================================================


def test_an_inference_has_no_span_and_cannot_be_written():
    assert memory_writer.assess(_proposal(stated_span="")).verdict == memory_writer.NO_SPAN
    assert memory_writer.assess(
        _proposal(stated_span="probably her chem teacher")).verdict == memory_writer.SPAN_NOT_IN_TRUSTED_TEXT


def test_basis_cannot_express_an_inference():
    """The structural half. `Basis` has no `inferred` member, so a guess has no spelling — this is not a
    check that can be skipped, it is a value that does not exist."""
    assert {b.value for b in Basis} == {"stated", "corrected", "confirmed"}
    with pytest.raises(ValueError):
        Basis("inferred")


def test_an_uncertain_fact_is_kept_but_marked_and_capped():
    """"i think it's ms delgado" is a real observation. It is stored — with the hedge recorded, the
    confidence capped, and the source intact — and it is never served as a flat fact."""
    rec = memory_writer.build(_proposal(
        trusted_text="i think my teacher is Ms. Delgado", stated_span="i think my teacher is Ms. Delgado",
        hedged=True, confidence=1.0))
    assert rec.confidence == memory_writer.HEDGED_CEILING
    assert rec.evidence.hedged is True
    assert rec.evidence.stated_span
    assert "not flatly" in memory_provenance.explain(rec, now=NOW).why_did_you_think_that()


def test_evidence_cannot_be_empty():
    with pytest.raises(ValueError, match="rests on"):
        Evidence(stated_span="  ")


# =====================================================================================================
# Rule 4 — identity is never inferred from style, name or vocabulary
# =====================================================================================================


def test_style_signals_have_no_path_to_a_factual_record():
    """The structural enforcement, and it names no words. `record_style_signal` hard-codes the kind and
    `remember` refuses that kind, so the two channels share no destination — a conclusion drawn from how
    someone writes has nowhere to be written as a claim about who they are."""
    style_params = inspect.signature(memory_writer.record_style_signal).parameters
    assert "kind" not in style_params and "predicate" not in style_params
    assert "subject" not in style_params
    assert memory_writer.assess(
        _proposal(kind=MemoryKind.style)).verdict == memory_writer.STYLE_IS_NOT_A_FACT




@pytest.mark.parametrize("predicate", ["profile.gender", "profile.religion", "world.family_structure",
                                       "profile.health_condition", "profile.immigration_status",
                                       "relationships.household_citizenship", "profile.sexuality"])
def test_sensitive_identity_traits_are_refused_however_well_grounded(predicate):
    """The second net, for a caller with a genuine grounded write who labels it as a trait. Refused even
    when the student stated it flatly: the general writer has no consented surface for these, and a
    feature that genuinely needs one has to build it where a reviewer will see it."""
    decision = memory_writer.assess(_proposal(predicate=predicate, kind=MemoryKind.profile,
                                              subject=SELF))
    assert decision.verdict == memory_writer.SENSITIVE_TRAIT


def test_the_trait_taxonomy_reads_the_claim_not_the_students_words():
    """It cannot be defeated by phrasing, slang or language, because it never looks at the message."""
    assert memory_writer.names_sensitive_trait("profile.religious_observance")
    assert not memory_writer.names_sensitive_trait("school.teacher_of_record")


# =====================================================================================================
# Write policy — useful later, stable, user-specific, evidence-backed, not filler
# =====================================================================================================


def test_a_memory_nobody_can_state_the_use_of_is_refused():
    assert memory_writer.assess(
        _proposal(reason_it_matters="  ")).verdict == memory_writer.NO_REASON_IT_MATTERS


def test_conversational_filler_cannot_supply_a_predicate_or_a_value():
    assert memory_writer.assess(_proposal(predicate="lol")).verdict == memory_writer.FILLER
    assert memory_writer.assess(
        _proposal(value="x" * (mr.MAX_VALUE + 1))).verdict == memory_writer.FILLER
    assert memory_writer.assess(
        _proposal(subject="chemistry", value="chemistry")).verdict == memory_writer.FILLER


def test_a_layer_and_a_lifetime_that_disagree_are_refused():
    assert memory_writer.assess(_proposal(
        kind=MemoryKind.profile, subject=SELF, predicate="profile.school",
        retention_policy=RetentionPolicy.transient)).verdict == memory_writer.UNSTABLE_FOR_LAYER
    assert memory_writer.assess(_proposal(
        kind=MemoryKind.episodic, predicate="school.what_happened",
        retention_policy=RetentionPolicy.durable)).verdict == memory_writer.UNSTABLE_FOR_LAYER


def test_a_profile_fact_must_be_about_the_student():
    assert memory_writer.assess(_proposal(
        kind=MemoryKind.profile, predicate="profile.school")).verdict == memory_writer.NOT_USER_SPECIFIC
    assert memory_writer.assess(_proposal(subject=SELF)).verdict == memory_writer.NOT_USER_SPECIFIC


# =====================================================================================================
# Freshness
# =====================================================================================================


def test_freshness_tracks_the_last_confirmation_not_the_first_mention():
    kw = dict(retention_policy=RetentionPolicy.school_year, now=NOW)
    old = NOW - timedelta(days=280)
    assert mr.compute_freshness(observed_at=old, last_confirmed_at=None, **kw) is Freshness.aging
    assert mr.compute_freshness(observed_at=old, last_confirmed_at=NOW - timedelta(days=5),
                                **kw) is Freshness.current


def test_a_durable_memory_never_expires_and_a_transient_one_does():
    long_ago = NOW - timedelta(days=4000)
    assert mr.compute_freshness(observed_at=long_ago, last_confirmed_at=None,
                                retention_policy=RetentionPolicy.durable, now=NOW) is Freshness.current
    assert mr.compute_freshness(observed_at=NOW - timedelta(days=20), last_confirmed_at=None,
                                retention_policy=RetentionPolicy.transient, now=NOW) is Freshness.expired


# =====================================================================================================
# Retrieval — the shape of it, without a database
# =====================================================================================================


def test_the_retriever_has_no_api_that_names_another_user():
    """Structural half of tenant isolation: a retriever built for one student exposes no method through
    which another student's id can be supplied."""
    assert "user_id" in inspect.signature(MemoryRetriever).parameters
    public = [m for name, m in inspect.getmembers(MemoryRetriever, inspect.isfunction)
              if not name.startswith("_")]
    assert public, "expected public retrieval methods"
    for method in public:
        assert "user_id" not in inspect.signature(method).parameters, method.__name__






# =====================================================================================================
# Real Postgres — RLS, the trigger, the CHECK constraints, forgetting
# =====================================================================================================

users = PostgresUserRepository()


@pytest.fixture()
def _pg(pg_test_db, monkeypatch):
    """Real Postgres, real RLS. Deliberately WITHOUT `clean_db`: every test here mints a fresh user id,
    so tenant isolation — the thing under test — is also what keeps the tests apart. Truncating between
    them would take an ACCESS EXCLUSIVE lock on `memory_records` (via `TRUNCATE users CASCADE`) that
    races the in-flight inserts of the test that just finished, which shows up as an intermittent
    deadlock rather than as a real failure."""
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(coro):
    return asyncio.run(coro)


def _new_user():
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    return uid


def _raw(user_id, memory_id):
    async def _go():
        async with user_session(user_id) as s:
            return (await s.execute(sa.select(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.id == memory_id))).first()
    return _run(_go())


def _remember(user_id, **over):
    return _run(memory_writer.remember(_proposal(user_id=user_id, **over)))




























# --- WHERE THE REMOVED TESTS WENT ---------------------------------------------------------------------
# Sixteen tests in this file exercised the retrieval, correction and forget APIs #121b shipped. #122
# replaced those with a two-stage retriever, a correction flow that writes an audit row, and four forget
# scopes, so the old tests were asserting the shape of code that no longer exists.
#
# Every PROPERTY they protected is still proven, against the production API and on real Postgres, in
# `test_memory_acceptance.py`:
#
#   cross-user retrieval is impossible        -> test_no_cross_user_retrieval
#                                                test_a_retriever_is_bound_to_one_student_for_its_whole_life
#   style cannot surface as a fact            -> test_style_memory_never_appears_in_a_factual_context
#   a forgotten record cannot be retrieved    -> test_a_forgotten_memory_never_comes_back
#   forgetting redacts rather than flags      -> test_forgetting_redacts_the_content_not_just_a_flag
#   a forgotten fact is not re-learned        -> test_a_forgotten_source_is_remembered_as_forgotten
#   a correction supersedes, never edits      -> test_a_correction_supersedes_and_leaves_no_active_contradiction
#   the row cannot be edited in place         -> test_a_row_cannot_be_edited_even_by_raw_sql
#   the shortlist is scoped and deterministic -> test_the_whole_memory_store_is_never_handed_to_a_prompt
#   a memory can explain itself               -> test_every_retrieved_fact_can_say_where_it_came_from
#
# TWO WERE NOT REPLACED, AND THAT IS A REAL CHANGE RATHER THAN AN OVERSIGHT:
#
#   * `test_a_forwarded_fact_is_rejected_by_the_database_too` asserted 0028's `ck_memory_trusted_source`,
#     which refused ANY row whose source was not the student. 0029 deliberately does not carry it
#     forward: attributed world and entity records — "practice is at six, per an email you forwarded" —
#     could not be stored at all under that constraint, and the acceleration program requires them. The
#     rule that survives is the one that matters: untrusted content may never become a fact about the
#     USER, which is enforced in `memory_writer` and has its own tests above.
#   * `test_forgetting_a_claim_takes_its_superseded_history_with_it` asserted a `claim` forget scope.
#     #122 implements fact, subject, kind and source. Forgetting a claim's superseded history is not
#     covered, so a corrected fact's OLD value survives a fact-scoped forget of the new one. Stated
#     rather than quietly dropped; it belongs with the next forget work.
