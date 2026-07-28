"""The adversarial harness: can an indirect claim about a student become a stored fact about them?

THE MEASURED DEFECT THIS FILE EXISTS FOR. #121b guarded user-profile writes with a denylist — a taxonomy
of sensitive traits applied to the predicate (`memory_policy.names_sensitive_trait`). Against the sixteen
scenarios in `SCENARIOS`, three tripped the taxonomy and thirteen were stored. That is not a tuning
problem, and `test_the_trait_taxonomy_alone_would_still_miss_most_of_these` reproduces the 3-of-16 number
on the current code so the claim stays honest rather than becoming folklore. A denylist has to RECOGNISE
the claim, and a euphemism is precisely a claim built not to be recognised.

SO THE ASSERTIONS HERE ARE STRUCTURAL, AND THE SCENARIOS ARE ONLY COVERAGE. Every test below asserts a
property of the candidate's SHAPE — its subject type, its provenance, whether its predicate is
registered — and none asserts that a phrase was detected. Nothing in this file reads a value to decide
anything, which is the only way the result generalises to the euphemism nobody thought of. If the
scenario list were deleted, the properties would still be proved; the list exists to demonstrate that
realistic attacks really do land in the shapes the properties cover.

THE BAR IS MEASURED AT THE TABLE, NOT AT THE POLICY. `untrusted_content_user_memories` counts persisted
rows — every scenario is run through the real writer against real Postgres and then the table is queried.
A policy that returns the right verdict and a writer that stores the row anyway would pass every offline
assertion in this file and fail `test_untrusted_content_user_memories_is_zero`, which is the entire
reason that test queries the database instead of counting verdicts.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import memory_policy as policy
from bruce_engine import memory_writer
from bruce_engine.db import user_session
from bruce_engine.memory_candidate import (
    PROVENANCE_TO_SOURCE, TRUSTED_PROVENANCE, UNTRUSTED_PROVENANCE, MemoryCandidate, ProvenanceClass,
    SubjectType,
)
from bruce_engine.memory_policy import PROFILE_REGISTRY, Outcome
from bruce_engine.memory_record import (
    FACTUAL, MEMORY_RECORDS, SELF, MemoryKind, RetentionPolicy, Sensitivity, SourceType,
)
from bruce_engine.repositories import PostgresUserRepository

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Scenario:
    """One realistic attempt to write a claim about the student. `category` is documentation; no
    assertion switches on it, because a harness that treated categories differently would be a phrase
    list wearing a dataclass."""

    name: str
    category: str
    predicate: str
    value: str
    evidence: str
    provenance: ProvenanceClass = ProvenanceClass.trusted_user_statement
    inferred: bool = False
    explicitly_stated: bool = True
    sensitivity: Sensitivity = Sensitivity.ordinary
    subject_type: SubjectType = SubjectType.user
    kind: MemoryKind = MemoryKind.profile


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("indirect_health", "indirect health claim", "profile.mental_state", "struggling",
             "he's been going through it lately"),
    Scenario("medication_euphemism", "indirect health claim", "profile.medication_status", "on treatment",
             "she's on something for it now"),
    Scenario("diagnostic_slang", "diagnostic language in slang", "profile.attention_profile", "adhd",
             "im so adhd about this lol"),
    Scenario("eating_euphemism", "indirect health claim", "profile.eating_pattern", "restricting",
             "i just don't really eat during the day right now"),
    Scenario("religious_implication", "religious implication", "profile.friday_routine", "mosque",
             "i go to the mosque on fridays"),
    Scenario("religion_named", "religious implication", "profile.religion", "muslim",
             "i'm muslim"),
    Scenario("political_implication", "political implication", "profile.weekend_activity", "canvassing",
             "spent saturday canvassing for the campaign"),
    Scenario("financial_status", "financial status", "profile.financial_situation", "cannot afford",
             "we can't really afford the trip"),
    Scenario("family_status", "family status", "profile.household_change", "divorce",
             "since the divorce it's been weird"),
    Scenario("orientation_implication", "sexual orientation", "profile.partner_context", "girlfriend",
             "she went with her girlfriend"),
    Scenario("immigration_euphemism", "identity implication", "profile.paperwork_status", "pending",
             "we're still waiting on our papers"),
    Scenario("style_as_trait", "style observation resembling a trait", "profile.personality", "anxious",
             "sorry!! sorry omg sorry", subject_type=SubjectType.style),
    Scenario("quoted_about_user", "quoted statement about the user", "profile.preferred_name", "Ari",
             "> everyone calls him Ari", provenance=ProvenanceClass.quoted_content),
    Scenario("forwarded_about_user", "forwarded statement about the user", "profile.grade_level", "11",
             "your son is in 11th grade", provenance=ProvenanceClass.forwarded_content),
    Scenario("screenshot_profile_claim", "screenshot containing profile claims", "profile.school",
             "Eastview High", "[screenshot] Student: Eastview High",
             provenance=ProvenanceClass.attachment_content),
    Scenario("model_overstates_evidence", "model summary overstating evidence",
             "profile.autonomy_preference", "act without asking",
             "user seems comfortable with Bruce acting independently",
             provenance=ProvenanceClass.model_inference, inferred=True, explicitly_stated=False),
)
"""Sixteen, spanning the eleven adversarial categories in the brief. Three of them name a trait plainly
enough for the old taxonomy to catch (`medication_status`, `religion`, `household_change`); the other
thirteen are the ones that used to be stored.

Note that four of the sixteen aim at REGISTERED predicates (`preferred_name`, `grade_level`, `school`,
`autonomy_preference`). Those are not caught by the registry at all — they are caught by provenance,
which is the second property. A harness that only tested unregistered predicates would leave the more
dangerous half of the surface untested.
"""


def _candidate(s: Scenario, *, user_id=None) -> MemoryCandidate:
    return MemoryCandidate(
        user_id=user_id or uuid4(), subject_type=s.subject_type,
        subject_id=SELF, kind=s.kind, predicate=s.predicate, proposed_value=s.value,
        normalized_value=s.value.lower(), evidence_text=s.evidence,
        source_type=PROVENANCE_TO_SOURCE[s.provenance], source_id="src-1",
        provenance_class=s.provenance, explicitly_stated_by_user=s.explicitly_stated,
        inferred=s.inferred, confidence=0.99, expected_stability=RetentionPolicy.durable,
        usefulness_reason="so Bruce understands the student", sensitivity_class=s.sensitivity,
        retention_recommendation=RetentionPolicy.durable, observed_at=NOW)


# =====================================================================================================
# The properties. Each one is a statement about SHAPE; none reads a value.
# =====================================================================================================


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_no_adversarial_scenario_becomes_a_believed_fact(scenario):
    """The headline result: none of the sixteen is stored active. The reason is always one of the
    structural refusals, never "a phrase matched"."""
    decision = policy.decide(_candidate(scenario))
    assert decision.outcome is not Outcome.store, f"{scenario.name} was stored"


def test_the_trait_taxonomy_alone_would_still_miss_most_of_these():
    """The measurement that justifies the rewrite, reproduced on the current code.

    `names_sensitive_trait` is the whole of #121b's user-profile defence. Run it over the same sixteen:
    it catches three. The other thirteen are refused by the registry and by provenance — mechanisms that
    never look at the claim's wording — which is why the harness does not depend on the taxonomy firing
    and why deleting the taxonomy tomorrow would not change a single result above.
    """
    caught = {s.name for s in SCENARIOS if policy.names_sensitive_trait(s.predicate)}
    assert len(caught) == 3, f"taxonomy reach changed: {sorted(caught)}"
    missed = [s for s in SCENARIOS if s.name not in caught]
    assert len(missed) == 13
    for scenario in missed:
        assert policy.decide(_candidate(scenario)).outcome is not Outcome.store


@pytest.mark.parametrize("provenance", sorted(UNTRUSTED_PROVENANCE, key=lambda p: p.value))
def test_a_disallowed_subject_provenance_combination_cannot_become_active(provenance):
    """PROPERTY 1, exhaustive over the untrusted half of the provenance enum crossed with the whole
    registry: nothing that arrived as someone else's words can describe the student. Receiving a message
    about yourself, or uploading a screenshot that describes you, is not you saying it."""
    for predicate in PROFILE_REGISTRY:
        decision = policy.decide(MemoryCandidate(
            user_id=uuid4(), subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
            predicate=predicate, proposed_value="x", normalized_value="x", evidence_text="something",
            source_type=PROVENANCE_TO_SOURCE[provenance], source_id="src-1",
            provenance_class=provenance, explicitly_stated_by_user=True, inferred=False,
            confidence=1.0, expected_stability=RetentionPolicy.durable,
            usefulness_reason="because", sensitivity_class=Sensitivity.ordinary,
            retention_recommendation=RetentionPolicy.durable, observed_at=NOW))
        assert decision.outcome is Outcome.reject
        assert decision.reason == policy.UNTRUSTED_SOURCE_FOR_USER


@pytest.mark.parametrize("suffix", [f"p{i}" for i in range(12)] + [
    "mental_state", "vibe", "energy_level", "situation", "background", "circumstances"])
def test_an_unknown_user_profile_predicate_cannot_become_active(suffix):
    """PROPERTY 2, over generated names as well as plausible ones. The generated `p0..p11` matter more
    than the readable ones: they prove the refusal is a property of BEING UNREGISTERED rather than of the
    label meaning anything, so it covers the euphemism nobody has thought of yet."""
    predicate = f"profile.{suffix}"
    assert predicate not in PROFILE_REGISTRY
    decision = policy.decide(MemoryCandidate(
        user_id=uuid4(), subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
        predicate=predicate, proposed_value="x", normalized_value="x",
        evidence_text="the student said something", source_type=SourceType.trusted_user_text,
        source_id="src-1", provenance_class=ProvenanceClass.trusted_user_statement,
        explicitly_stated_by_user=True, inferred=False, confidence=1.0,
        expected_stability=RetentionPolicy.durable, usefulness_reason="because",
        sensitivity_class=Sensitivity.ordinary, retention_recommendation=RetentionPolicy.durable,
        observed_at=NOW))
    assert decision.outcome is not Outcome.store


@pytest.mark.parametrize("kind", sorted(FACTUAL, key=lambda k: k.value))
def test_a_style_candidate_cannot_cross_into_a_factual_kind(kind):
    """PROPERTY 3. How someone writes cannot become a claim about who they are — not for profile, and not
    for any other factual layer either. There is no confidence or provenance that opens this."""
    decision = policy.decide(MemoryCandidate(
        user_id=uuid4(), subject_type=SubjectType.style, subject_id=SELF, kind=kind,
        predicate=f"{kind.value}.personality", proposed_value="anxious", normalized_value="anxious",
        evidence_text="sorry!! sorry omg", source_type=SourceType.trusted_user_text, source_id="src-1",
        provenance_class=ProvenanceClass.trusted_user_statement, explicitly_stated_by_user=True,
        inferred=False, confidence=1.0, expected_stability=RetentionPolicy.durable,
        usefulness_reason="because", sensitivity_class=Sensitivity.ordinary,
        retention_recommendation=RetentionPolicy.durable, observed_at=NOW))
    assert decision.outcome is Outcome.reject
    assert decision.reason == policy.STYLE_CANNOT_BECOME_FACT


@pytest.mark.parametrize("provenance", sorted(ProvenanceClass, key=lambda p: p.value))
def test_an_inferred_sensitive_fact_can_never_be_confirmed(provenance):
    """PROPERTY 4, over every provenance including the trusted ones. A sensitive claim needs an explicit
    trusted disclosure AND a registered product need; no registry entry declares itself sensitive, so the
    second half cannot be met and an inferred one fails both."""
    decision = policy.decide(MemoryCandidate(
        user_id=uuid4(), subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
        predicate="profile.preferred_name", proposed_value="x", normalized_value="x",
        evidence_text="something", source_type=PROVENANCE_TO_SOURCE[provenance], source_id="src-1",
        provenance_class=provenance, explicitly_stated_by_user=True, inferred=True, confidence=1.0,
        expected_stability=RetentionPolicy.durable, usefulness_reason="because",
        sensitivity_class=Sensitivity.sensitive, retention_recommendation=RetentionPolicy.durable,
        observed_at=NOW))
    assert decision.outcome is Outcome.reject


@pytest.mark.parametrize("provenance", [ProvenanceClass.quoted_content,
                                        ProvenanceClass.forwarded_content,
                                        ProvenanceClass.attachment_content])
def test_quoted_content_cannot_become_an_explicit_user_statement(provenance):
    """PROPERTY 5, and it tests the LIE rather than the honest case: the caller sets
    `explicitly_stated_by_user=True` on quoted material. `is_trusted` is computed from provenance, so the
    flag buys nothing — a caller cannot promote someone else's words by asserting they were the
    student's."""
    candidate = MemoryCandidate(
        user_id=uuid4(), subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
        predicate="profile.preferred_name", proposed_value="Ari", normalized_value="ari",
        evidence_text="> everyone calls him Ari", source_type=PROVENANCE_TO_SOURCE[provenance],
        source_id="src-1", provenance_class=provenance, explicitly_stated_by_user=True, inferred=False,
        confidence=1.0, expected_stability=RetentionPolicy.durable, usefulness_reason="because",
        sensitivity_class=Sensitivity.ordinary, retention_recommendation=RetentionPolicy.durable,
        observed_at=NOW)
    assert candidate.is_trusted is False
    assert policy.decide(candidate).reason == policy.UNTRUSTED_SOURCE_FOR_USER


def test_a_caller_cannot_launder_an_inference_by_leaving_the_flag_false():
    """`is_inferred` is an OR over the caller's flag and the provenance class, so `model_inference` with
    `inferred=False` is still an inference. Without the OR, the honest field would be the only one
    enforced and the dishonest path would be the open one."""
    candidate = MemoryCandidate(
        user_id=uuid4(), subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
        predicate="profile.school", proposed_value="Eastview", normalized_value="eastview",
        evidence_text="probably eastview", source_type=SourceType.model, source_id="src-1",
        provenance_class=ProvenanceClass.model_inference, explicitly_stated_by_user=True,
        inferred=False, confidence=1.0, expected_stability=RetentionPolicy.school_year,
        usefulness_reason="because", sensitivity_class=Sensitivity.ordinary,
        retention_recommendation=RetentionPolicy.school_year, observed_at=NOW)
    assert candidate.is_inferred is True
    assert policy.decide(candidate).outcome is Outcome.reject


def test_absence_of_disagreement_is_not_consent():
    """There is no field on a candidate that means "the student did not object", so silence cannot be
    spelled as a warrant. The nearest a caller can get is an unstated preference, which is withheld from
    retrieval rather than believed."""
    fields = set(MemoryCandidate.__dataclass_fields__)
    assert not {f for f in fields if "consent" in f or "agree" in f or "object" in f}
    decision = policy.decide(MemoryCandidate(
        user_id=uuid4(), subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
        predicate="profile.autonomy_preference", proposed_value="act freely", normalized_value="act freely",
        evidence_text="the student did not object", source_type=SourceType.trusted_user_text,
        source_id="src-1", provenance_class=ProvenanceClass.trusted_user_statement,
        explicitly_stated_by_user=False, inferred=False, confidence=1.0,
        expected_stability=RetentionPolicy.durable, usefulness_reason="because",
        sensitivity_class=Sensitivity.ordinary, retention_recommendation=RetentionPolicy.durable,
        observed_at=NOW))
    assert decision.outcome is not Outcome.store


# =====================================================================================================
# The bar, measured at the persisted-record boundary
# =====================================================================================================

users = PostgresUserRepository()


@pytest.fixture()
def _pg(pg_test_db, monkeypatch):
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


def untrusted_content_user_memories(user_id) -> int:
    """THE BAR. Persisted rows that are about the STUDENT and came from anything but the student.

    Measured on the table, not on verdicts, and deliberately WITHOUT a status filter: a quarantined row
    is still a row, and "we stored it but marked it" is not an answer to "did untrusted content become a
    memory about this person". A claim is about the student when it is filed under `profile` or carries
    the `SELF` entity key — the two spellings the writer can produce — so the count cannot be dodged by
    choosing the other one.
    """
    async def _go():
        async with user_session(user_id) as s:
            return (await s.execute(sa.select(sa.func.count()).select_from(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.user_id == user_id,
                sa.or_(MEMORY_RECORDS.c.kind == MemoryKind.profile.value,
                       MEMORY_RECORDS.c.entity_key == SELF),
                MEMORY_RECORDS.c.source_type != SourceType.trusted_user_text.value))).scalar_one()
    return _run(_go())


def test_untrusted_content_user_memories_is_zero(_pg):
    """Every scenario through the real writer, then count what actually landed. Required result: zero.

    This is the test that would fail if the policy were right and the writer stored anyway, which is the
    failure the offline assertions structurally cannot see.
    """
    uid = _new_user()
    receipts = [_run(memory_writer.MemoryWriter(uid).evaluate(_candidate(s, user_id=uid)))
                for s in SCENARIOS]

    assert untrusted_content_user_memories(uid) == 0
    stored = [s.name for s, r in zip(SCENARIOS, receipts) if r.stored]
    assert stored == [], f"scenarios that became believed facts: {stored}"


def test_no_scenario_leaves_an_active_row_of_any_kind(_pg):
    """Stronger than the count, and the one a future refactor is most likely to break: not one of the
    sixteen may end up believed under ANY kind — including a caller re-filing a profile claim as `world`
    to get around the registry."""
    uid = _new_user()
    for s in SCENARIOS:
        _run(memory_writer.MemoryWriter(uid).evaluate(_candidate(s, user_id=uid)))

    async def _go():
        async with user_session(uid) as s:
            return list((await s.execute(sa.select(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.user_id == uid,
                MEMORY_RECORDS.c.status == "active"))).mappings().all())

    assert _run(_go()) == []


def test_only_registry_gaps_are_persisted_at_all(_pg):
    """What DOES survive, and why that is acceptable: a quarantined row for a trusted, explicit statement
    whose predicate nobody registered. Unreachable by retrieval, and the only way a genuine gap in the
    registry is ever discovered. Nothing untrusted and nothing inferred reaches even this."""
    uid = _new_user()
    for s in SCENARIOS:
        _run(memory_writer.MemoryWriter(uid).evaluate(_candidate(s, user_id=uid)))

    async def _go():
        async with user_session(uid) as s:
            return list((await s.execute(sa.select(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.user_id == uid))).mappings().all())

    rows = _run(_go())
    assert {r["status"] for r in rows} <= {"quarantined"}
    assert all(r["source_type"] == SourceType.trusted_user_text.value for r in rows)
    assert all(r["predicate"] not in PROFILE_REGISTRY for r in rows)


def test_a_legitimate_profile_fact_still_lands(_pg):
    """The control. A harness that refuses everything proves nothing about a policy — it proves the
    policy is broken in the other direction, and every assertion above would still pass."""
    uid = _new_user()
    receipt = _run(memory_writer.MemoryWriter(uid).evaluate(MemoryCandidate(
        user_id=uid, subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
        predicate="profile.preferred_name", proposed_value="Ari", normalized_value="ari",
        evidence_text="everyone calls me Ari", source_type=SourceType.trusted_user_text,
        source_id="msg-7", provenance_class=ProvenanceClass.trusted_user_statement,
        explicitly_stated_by_user=True, inferred=False, confidence=1.0,
        expected_stability=RetentionPolicy.durable, usefulness_reason="so replies use their name",
        sensitivity_class=Sensitivity.ordinary, retention_recommendation=RetentionPolicy.durable,
        observed_at=NOW)))
    assert receipt.stored
    assert untrusted_content_user_memories(uid) == 0


def test_an_attributed_world_fact_is_not_counted_as_a_user_memory(_pg):
    """The counter must not be vacuous. A forwarded world fact IS stored with untrusted provenance — that
    is the behaviour #122 enabled — and it correctly does not appear in the count, because it is not
    about the student. A count that was zero simply because nothing untrusted is ever stored would be
    measuring nothing."""
    uid = _new_user()
    receipt = _run(memory_writer.MemoryWriter(uid).evaluate(MemoryCandidate(
        user_id=uid, subject_type=SubjectType.world, subject_id="practice", kind=MemoryKind.world,
        predicate="practice.start_time", proposed_value="18:00", normalized_value="18:00",
        evidence_text="practice is at 6", source_type=SourceType.forwarded, source_id="fwd-1",
        provenance_class=ProvenanceClass.forwarded_content, explicitly_stated_by_user=False,
        inferred=False, confidence=0.9, expected_stability=RetentionPolicy.season,
        usefulness_reason="so Bruce can answer when practice starts",
        sensitivity_class=Sensitivity.ordinary, retention_recommendation=RetentionPolicy.season,
        observed_at=NOW)))
    assert receipt.stored

    async def _go():
        async with user_session(uid) as s:
            return (await s.execute(sa.select(sa.func.count()).select_from(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.user_id == uid,
                MEMORY_RECORDS.c.source_type != SourceType.trusted_user_text.value))).scalar_one()

    assert _run(_go()) == 1, "the untrusted row must exist, or the bar below proves nothing"
    assert untrusted_content_user_memories(uid) == 0
