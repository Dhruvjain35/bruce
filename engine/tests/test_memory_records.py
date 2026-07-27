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


def test_a_style_record_cannot_become_a_factual_memory_context():
    style = MemoryRecord(
        memory_id=uuid4(), user_id=uuid4(), kind=MemoryKind.style, subject=SELF,
        predicate="style.register", value="lowercase, short",
        evidence=Evidence(stated_span="yo whats up"), source_message_id="msg-1",
        source_type=SourceType.trusted_user_text, confidence=1.0, observed_at=NOW,
        last_confirmed_at=None, retention_policy=RetentionPolicy.season,
        sensitivity=Sensitivity.ordinary, user_editable=True, contradicted_by=None, superseded_by=None)
    assert style.kind not in FACTUAL
    with pytest.raises(ValueError, match="not a factual memory"):
        MemoryContext.of(style, reason_it_matters="x", now=NOW)


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


def test_a_reranker_may_reorder_and_drop_but_never_add():
    """#122's seam cannot widen the scope the deterministic stage decided on."""
    def _smuggle(_query, shortlist):
        return [*shortlist, MemoryContext(fact="someone else's", confidence=1.0, source=None,
                                          freshness=Freshness.current, reason_it_matters_now="")]
    with pytest.raises(ValueError, match="never add"):
        memory_retrieval._apply_rerank("q", [], _smuggle)
    assert memory_retrieval._apply_rerank("q", [], lambda q, s: []) == []


def test_the_semantic_stage_is_a_declared_seam_not_a_stub():
    """There is no placeholder ranker in this module — an identity-function stub is indistinguishable
    from a working one in every test that is not looking for it, and that is how it ships."""
    assert "rerank" in inspect.signature(MemoryRetriever.facts).parameters
    assert not any(n.startswith("_rank") or n.startswith("_embed")
                   for n in dir(memory_retrieval))


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


def test_a_forwarded_fact_is_rejected_by_the_database_too(_pg):
    """Belt and braces on rule 1: even bypassing the writer and the dataclass, `ck_memory_trusted_source`
    refuses the row."""
    uid = _new_user()

    async def _go():
        async with user_session(uid) as s:
            await s.execute(sa.insert(MEMORY_RECORDS).values(
                id=uuid4(), user_id=uid, kind="relationships", subject="ms delgado",
                predicate="school.teacher_of_record", value="chemistry", evidence={},
                source_message_id="m", source_type=SourceType.forwarded.value, confidence=1.0,
                observed_at=NOW, retention_policy="school_year", sensitivity="ordinary",
                user_editable=True, entity_key="ms delgado", domain="school"))

    with pytest.raises(Exception) as err:
        _run(_go())
    assert "ck_memory_trusted_source" in str(err.value)


def test_a_correction_supersedes_and_does_not_edit(_pg):
    uid = _new_user()
    original = _remember(uid)
    assert original is not None

    replacement = _run(memory_correction.MemoryCorrection(
        user_id=uid, target_memory_id=original.memory_id, new_value="AP chemistry",
        trusted_text="no she teaches AP chemistry", stated_span="she teaches AP chemistry",
        source_message_id="msg-2").apply())

    old = _raw(uid, original.memory_id)
    assert old.value == "chemistry", "history must survive the correction verbatim"
    assert old.superseded_by == replacement.memory_id
    assert old.contradicted_by == replacement.memory_id
    assert replacement.evidence.basis is Basis.corrected

    live = _run(MemoryRetriever(uid).facts())
    assert [c.fact for c in live] == ["ms delgado — teacher of record: AP chemistry"]


def test_editing_a_stored_memory_in_place_is_rejected_by_the_database(_pg):
    """The reason a correction cannot quietly become an overwrite in a later refactor."""
    uid = _new_user()
    rec = _remember(uid)

    async def _go():
        async with user_session(uid) as s:
            await s.execute(sa.update(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.id == rec.memory_id).values(value="AP chemistry"))

    with pytest.raises(Exception) as err:
        _run(_go())
    assert "append-only" in str(err.value)


def test_confirming_a_memory_is_allowed_and_refreshes_it(_pg):
    uid = _new_user()
    rec = _remember(uid, retention_policy=RetentionPolicy.school_year,
                    observed_at=NOW - timedelta(days=280))
    assert _run(memory_correction.confirm(uid, rec.memory_id, at=NOW)) is True
    assert _raw(uid, rec.memory_id).last_confirmed_at is not None


def test_a_forgotten_record_cannot_be_retrieved(_pg):
    uid = _new_user()
    rec = _remember(uid)
    assert _run(MemoryRetriever(uid).facts())

    assert _run(memory_correction.MemoryForget(user_id=uid, memory_id=rec.memory_id).apply()) == 1

    assert _run(MemoryRetriever(uid).facts()) == []
    assert _run(MemoryRetriever(uid).record(rec.memory_id)) is None
    row = _raw(uid, rec.memory_id)
    assert row.forgotten_at is not None
    assert (row.subject, row.predicate, row.value, row.evidence,
            row.reason_it_matters, row.entity_key) == (None, None, None, None, None, None)
    assert row.source_message_id == "msg-1", "content-free lineage survives, exactly as retention.py does"


def test_forgetting_a_claim_takes_its_superseded_history_with_it(_pg):
    """"forget that ms delgado is my teacher" is about the belief. Leaving the superseded row behind
    would satisfy a per-row forget and obviously fail the student."""
    uid = _new_user()
    original = _remember(uid)
    replacement = _run(memory_correction.MemoryCorrection(
        user_id=uid, target_memory_id=original.memory_id, new_value="AP chemistry",
        trusted_text="no she teaches AP chemistry", stated_span="she teaches AP chemistry").apply())

    erased = _run(memory_correction.MemoryForget(
        user_id=uid, memory_id=replacement.memory_id,
        scope=memory_correction.ForgetScope.claim).apply())
    assert erased == 2
    assert _raw(uid, original.memory_id).value is None


def test_a_forgotten_row_that_still_carries_content_cannot_be_stored(_pg):
    """Forgetting erases. A flag-only tombstone is one dropped WHERE clause from being undone, so the
    database refuses to hold one."""
    uid = _new_user()
    rec = _remember(uid)

    async def _go():
        async with user_session(uid) as s:
            await s.execute(sa.update(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.id == rec.memory_id).values(forgotten_at=NOW))

    with pytest.raises(Exception) as err:
        _run(_go())
    assert "erase content" in str(err.value) or "ck_memory_forgotten_redacted" in str(err.value)


def test_a_forgotten_fact_is_not_relearned_from_the_same_message(_pg):
    """What a student means by "forget that": gone, not gone-until-you-re-read-that-email. The message is
    still in their inbox and still re-processable, which is the whole argument for a tombstone."""
    uid = _new_user()
    rec = _remember(uid)
    _run(memory_correction.MemoryForget(user_id=uid, memory_id=rec.memory_id).apply())

    assert _remember(uid) is None                                   # same source message — refused
    again = _remember(uid, source_message_id="msg-later")           # they said it again, in a new one
    assert again is not None, "saying it again is the student choosing to re-teach it"
    assert len(_run(MemoryRetriever(uid).facts())) == 1


def test_cross_user_retrieval_is_impossible(_pg):
    a, b = _new_user(), _new_user()
    _remember(a)
    assert len(_run(MemoryRetriever(a).facts())) == 1
    assert _run(MemoryRetriever(b).facts()) == []

    async def _count_with_no_where(uid):
        # RLS, proved with the tenant predicate deliberately removed from the query.
        async with user_session(uid) as s:
            return (await s.execute(
                sa.select(sa.func.count()).select_from(MEMORY_RECORDS))).scalar()

    assert _run(_count_with_no_where(a)) == 1
    assert _run(_count_with_no_where(b)) == 0


def test_style_memory_cannot_surface_in_a_factual_query(_pg):
    uid = _new_user()
    _remember(uid)
    style = _run(memory_writer.record_style_signal(
        user_id=uid, relation="register", value="lowercase, no punctuation",
        trusted_text="yo whats up", stated_span="yo whats up", source_message_id="msg-3"))
    assert style is not None and style.kind is MemoryKind.style

    facts = _run(MemoryRetriever(uid).facts())
    assert len(facts) == 1 and "register" not in facts[0].fact
    signals = _run(MemoryRetriever(uid).style())
    assert [s.relation for s in signals] == ["register"]
    assert all(not isinstance(s, MemoryContext) for s in signals)


def test_a_stylistic_signal_cannot_produce_a_sensitive_trait_memory(_pg):
    """The end-to-end version of rule 4: the only thing the style door can produce is a style row, and no
    factual query can reach it — so there is no sequence of calls that turns "how she writes" into a
    stored claim about who she is."""
    uid = _new_user()
    signal = _run(memory_writer.record_style_signal(
        user_id=uid, relation="religion", value="mentions fasting",
        trusted_text="cant eat till sundown lol", stated_span="cant eat till sundown"))
    assert signal.kind is MemoryKind.style and signal.predicate == "style.religion"
    assert _run(MemoryRetriever(uid).facts()) == []
    assert _run(MemoryRetriever(uid).record(signal.memory_id)).kind not in FACTUAL
    # and the fact door refuses the same claim outright
    assert memory_writer.assess(_proposal(
        user_id=uid, kind=MemoryKind.profile, subject=SELF,
        predicate="profile.religion")).verdict == memory_writer.SENSITIVE_TRAIT


def test_the_shortlist_is_scoped_and_deterministic(_pg):
    uid = _new_user()
    for i in range(3):
        _remember(uid, subject=f"person {i}", source_message_id=f"msg-s{i}", observed_at=NOW)
    _remember(uid, subject="coach", predicate="sports.coach_of", value="cross country",
              source_message_id="msg-c", observed_at=NOW)

    by_domain = _run(MemoryRetriever(uid).facts(domains=["sports"]))
    assert [c.fact for c in by_domain] == ["coach — coach of: cross country"]
    by_entity = _run(MemoryRetriever(uid).facts(entities=["Person 1"]))
    assert len(by_entity) == 1 and by_entity[0].fact.startswith("person 1")

    first = [c.fact for c in _run(MemoryRetriever(uid).facts())]
    second = [c.fact for c in _run(MemoryRetriever(uid).facts())]
    assert first == second and len(first) == 4, "identical question, identical answer"


def test_a_retrieved_memory_can_explain_itself(_pg):
    uid = _new_user()
    _remember(uid)
    ctx = _run(MemoryRetriever(uid).facts())[0]
    assert ctx.reason_it_matters_now == "so i know who to email about chem"
    assert "something you told me" in ctx.source.where_did_that_come_from()
    assert "my teacher is Ms. Delgado" in ctx.source.where_did_that_come_from()
    assert ctx.freshness is Freshness.current
