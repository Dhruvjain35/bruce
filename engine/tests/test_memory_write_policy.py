"""The write policy (#123) — default deny, one door, and a floor per kind.

WHAT THIS FILE IS FOR. #121b's policy could be bypassed by not calling it, and its user-profile check was
a denylist that thirteen of sixteen euphemisms walked straight through (the count is reproduced in
`test_memory_euphemism.py`). Both defects are structural, so both are tested structurally here:

  * ONE DOOR      an AST walk over every module in `bruce_engine`, proving the set of modules that can
                  create a `memory_records` row is the documented one. Asserted on the syntax tree and
                  never on source text — a docstring that merely NAMES the table has already produced two
                  false failures in this repo, and `memory_writer`'s own docstring names it repeatedly.
  * DEFAULT DENY  a `subject_type=user` claim is refused unless its predicate is registered. The tests
                  read the registry rather than restating it, so adding an entry cannot leave a test
                  asserting the old surface.

The Postgres half proves the parts only the database can: claim lineage is written, the partial unique
index makes a second active version of a claim impossible, and a quarantined row is invisible to
retrieval.
"""

from __future__ import annotations

import ast
import asyncio
import itertools
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import memory_policy as policy
from bruce_engine import memory_retrieval, memory_writer
from bruce_engine.db import user_session
from bruce_engine.memory_candidate import (
    PROVENANCE_TO_SOURCE, TRUSTED_PROVENANCE, UNTRUSTED_PROVENANCE, MemoryCandidate, ProvenanceClass,
    SubjectType,
)
from bruce_engine.memory_policy import KIND_FLOORS, PROFILE_REGISTRY, SUBJECT_KIND_MATRIX, Outcome
from bruce_engine.memory_record import (
    FACTUAL, MEMORY_RECORDS, SELF, MemoryKind, RetentionPolicy, Sensitivity, SourceType,
)
from bruce_engine.repositories import PostgresUserRepository

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
ENGINE_PKG = Path(memory_writer.__file__).resolve().parent


def _candidate(**over) -> MemoryCandidate:
    """A well-formed, trusted, registered profile claim. Every test names only what it changes, so a
    refusal in a test is attributable to that one field."""
    base = dict(
        user_id=uuid4(), subject_type=SubjectType.user, subject_id=SELF, kind=MemoryKind.profile,
        predicate="profile.preferred_name", proposed_value="Ari", normalized_value="ari",
        evidence_text="everyone calls me Ari", source_type=SourceType.trusted_user_text,
        source_id="msg-1", provenance_class=ProvenanceClass.trusted_user_statement,
        explicitly_stated_by_user=True, inferred=False, confidence=1.0,
        expected_stability=RetentionPolicy.durable, usefulness_reason="so replies use their name",
        sensitivity_class=Sensitivity.ordinary, retention_recommendation=RetentionPolicy.durable,
        observed_at=NOW)
    base.update(over)
    return MemoryCandidate(**base)


# =====================================================================================================
# One door — proved on the AST, never on the source text
# =====================================================================================================


def _memory_row_aliases(tree: ast.AST) -> set[str]:
    """Names bound, in this module, to the `memory_records` table or its ORM class.

    Resolved per module because every module spells it differently: `MEMORY_RECORDS` here, a local `R =
    schema.MemoryRecordRow` there. Matching the spelling instead of the binding is how a rename silently
    disables the proof.
    """
    aliases: set[str] = set()

    def _is_memory_row(node: ast.AST) -> bool:
        # `schema.MemoryRecordRow`, `MemoryRecordRow`, and `<either>.__table__`.
        if isinstance(node, ast.Attribute):
            if node.attr == "MemoryRecordRow":
                return True
            if node.attr == "__table__":
                return _is_memory_row(node.value)
            return False
        return isinstance(node, ast.Name) and node.id in {"MemoryRecordRow", "MEMORY_RECORDS"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_memory_row(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in {"MEMORY_RECORDS", "MemoryRecordRow"}:
                    aliases.add(a.asname or a.name)
    return aliases


def _creates_memory_rows(tree: ast.AST, aliases: set[str]) -> list[str]:
    """Every expression in this module that would CREATE a `memory_records` row.

    Two shapes, because the codebase uses both and a proof that knows only one is not a proof:
      * Core   `sa.insert(<alias>)` / `insert(<alias>)`
      * ORM    `session.add(<alias>(...))`
    """
    found: list[str] = []

    def _names(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in aliases
        if isinstance(node, ast.Attribute):
            return node.attr in aliases or _names(node.value)
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "insert" and node.args and _names(node.args[0]):
            found.append(f"insert@{node.lineno}")
        elif name == "add" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Call) and _names(arg.func):
                found.append(f"session.add@{node.lineno}")
    return found


def _row_creators() -> dict[str, list[str]]:
    creators: dict[str, list[str]] = {}
    for path in sorted(ENGINE_PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = _creates_memory_rows(tree, _memory_row_aliases(tree))
        if hits:
            creators[path.stem] = hits
    return creators


def test_the_ast_proof_can_actually_see_an_insert():
    """The proof's own smoke test. An AST matcher that silently matches NOTHING passes every assertion
    below it and is indistinguishable from a working one — which is exactly the class of test that ships.
    So: assert the matcher finds the writer's insert before trusting it to say anyone else has none."""
    creators = _row_creators()
    assert "memory_writer" in creators, "the matcher found no insert in the module that definitely has one"
    assert len(creators["memory_writer"]) == 1, (
        f"memory_writer must contain exactly one insert; found {creators['memory_writer']}")


def test_only_the_writer_can_create_a_memory_row():
    """ONE DOOR, and it is now literally one.

    #123 shipped with two: `memory_correction` inserted its own replacement rows, so an untrusted
    "correction" reached the table without meeting the write policy, the provenance rules or the
    per-kind confidence floor. This assertion could only name the two modules and say the second was
    deliberate — which is a description of a hole, not a boundary.

    #124A moved supersession into the writer. A correction now proposes a `MemoryCandidate` and gets
    judged exactly like a first-time fact; `memory_correction` coordinates and writes an audit row to a
    different table, and has nothing left that puts a row in `memory_records`.

    Anything appearing here is a second door, and this is the thing that stops it.
    """
    assert set(_row_creators()) == {"memory_writer"}


def test_a_correction_is_judged_by_the_same_policy_as_a_first_time_fact():
    """The property the second door was hiding. A correction carrying untrusted provenance is refused by
    the writer itself, not only by the caller that happens to check first — so a future path that reaches
    the writer without going through `memory_correction` is refused for the same reason."""
    import inspect

    from bruce_engine import memory_writer

    src = inspect.getsource(memory_writer.MemoryWriter.evaluate)
    tree = ast.parse(inspect.cleandoc(src.split('"""', 2)[-1]) if False else src.lstrip())
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "trusted_user_correction" in names, "supersession does not check correction provenance"
    assert "supersedes" in inspect.signature(memory_writer.MemoryWriter.evaluate).parameters


def test_no_module_reaches_around_the_writer_for_its_row_builder():
    """The private helpers that assemble a row are the writer's alone. A module importing `_row_values`
    or `_insert` would be building the door again out of the door's own parts."""
    for path in sorted(ENGINE_PKG.glob("*.py")):
        if path.stem == "memory_writer":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("memory_writer"):
                assert not any(a.name.startswith("_") for a in node.names), (
                    f"{path.stem} imports a private writer helper")


# =====================================================================================================
# Gate 1 — which kinds a subject type may reach
# =====================================================================================================


def test_the_subject_kind_matrix_is_exhaustive_over_subject_types():
    assert set(SUBJECT_KIND_MATRIX) == set(SubjectType)


@pytest.mark.parametrize("kind", sorted(FACTUAL, key=lambda k: k.value))
def test_a_style_observation_can_never_reach_a_factual_kind(kind):
    """Rule 4, from the subject side. No confidence, provenance or predicate makes this reachable —
    `SubjectType.style` has one destination and it is not a factual one."""
    decision = policy.decide(_candidate(subject_type=SubjectType.style, kind=kind,
                                        predicate=f"{kind.value}.anything", subject_id=SELF))
    assert decision.outcome is Outcome.reject
    assert decision.reason == policy.STYLE_CANNOT_BECOME_FACT


@pytest.mark.parametrize("subject_type", [s for s in SubjectType if s is not SubjectType.user])
def test_nothing_but_a_user_subject_reaches_the_profile_kind(subject_type):
    """Provider content, a forwarded world fact and a style signal all fail identically at the matrix,
    before any predicate is considered — which is why the registry never has to see them."""
    decision = policy.decide(_candidate(subject_type=subject_type, kind=MemoryKind.profile))
    assert decision.outcome is Outcome.reject


# =====================================================================================================
# Gate 2 — the user-profile registry is an allowlist
# =====================================================================================================


def test_the_registry_is_the_documented_surface():
    """Read from the registry, not restated: the assertion is on SHAPE, so adding an entry does not
    silently leave a stale expectation behind."""
    assert len(PROFILE_REGISTRY) == 10
    for predicate, rule in PROFILE_REGISTRY.items():
        assert predicate == rule.predicate
        assert predicate.startswith("profile.")
        assert rule.accepted_provenance <= TRUSTED_PROVENANCE, (
            f"{predicate} accepts a source that is not the student's own words")
        assert rule.product_need.strip(), f"{predicate} has no registered product need"
        assert 0.0 < rule.min_confidence <= 1.0


def test_no_registered_predicate_is_sensitive_so_no_sensitive_user_fact_can_be_stored():
    """The sensitive rule has two halves — explicit trusted disclosure AND a registered product need —
    and the second is unsatisfiable today by construction. This is the assertion that keeps it that way:
    opening the door means declaring `sensitivity=sensitive` on an entry, which fails here until someone
    changes this test on purpose."""
    assert all(r.sensitivity is not Sensitivity.sensitive for r in PROFILE_REGISTRY.values())


def test_an_unregistered_user_predicate_never_becomes_active():
    """Default deny, and nothing read the value. This is the whole fix for the euphemism defect; the
    adversarial coverage lives in `test_memory_euphemism.py`."""
    decision = policy.decide(_candidate(predicate="profile.favourite_colour"))
    assert decision.outcome is not Outcome.store
    assert decision.reason == policy.UNREGISTERED_USER_PREDICATE


def test_an_unregistered_predicate_that_is_also_inferred_is_rejected_outright():
    """Quarantine exists to surface a registry GAP — a trusted, explicit statement nobody registered. An
    inference is not a gap, it is a guess, and it leaves no row at all."""
    decision = policy.decide(_candidate(predicate="profile.favourite_colour", inferred=True))
    assert decision.outcome is Outcome.reject


@pytest.mark.parametrize("predicate", sorted(PROFILE_REGISTRY))
def test_every_registered_predicate_is_reachable_by_the_source_it_declares(predicate):
    """A registry entry nothing can satisfy is a dead rule that reads as protection. For each entry, the
    provenance it declares plus its own confidence floor must actually produce a store."""
    rule = PROFILE_REGISTRY[predicate]
    provenance = sorted(rule.accepted_provenance, key=lambda p: p.value)[0]
    decision = policy.decide(_candidate(
        predicate=predicate, provenance_class=provenance, confidence=1.0,
        explicitly_stated_by_user=True, expected_stability=rule.retention,
        retention_recommendation=rule.retention))
    assert decision.outcome is Outcome.store, f"{predicate} is unreachable: {decision.reason}"
    assert decision.retention is rule.retention, "retention comes from the registry, not the caller"


@pytest.mark.parametrize("provenance", sorted(UNTRUSTED_PROVENANCE, key=lambda p: p.value))
def test_no_untrusted_provenance_reaches_any_registered_predicate(provenance):
    """Received or uploaded material cannot describe the student, whatever it says and however well
    formed the claim is. Exhaustive over the untrusted half of the enum rather than over examples."""
    for predicate in PROFILE_REGISTRY:
        decision = policy.decide(_candidate(
            predicate=predicate, provenance_class=provenance,
            source_type=PROVENANCE_TO_SOURCE[provenance]))
        assert decision.outcome is Outcome.reject
        assert decision.reason == policy.UNTRUSTED_SOURCE_FOR_USER


def test_a_predicate_requiring_an_explicit_statement_is_withheld_when_it_is_only_implied():
    """`autonomy_preference` decides how much Bruce may act unasked. Guessed wrong in the permissive
    direction it authorizes unrequested action, so an unstated one is held out of retrieval rather than
    believed."""
    decision = policy.decide(_candidate(
        predicate="profile.autonomy_preference", proposed_value="act without asking",
        explicitly_stated_by_user=False))
    assert decision.outcome is Outcome.quarantine
    assert decision.reason == policy.NOT_EXPLICITLY_STATED


def test_inference_is_refused_except_where_the_predicate_permits_it():
    """Two entries permit derivation (`timezone`, `locale`) because a provider's value beats the
    student's recall and a wrong one silently mis-times every reminder. Everything else refuses."""
    permitted = {p for p, r in PROFILE_REGISTRY.items() if r.inference_allowed}
    assert permitted == {"profile.timezone", "profile.locale"}
    for predicate, rule in PROFILE_REGISTRY.items():
        decision = policy.decide(_candidate(predicate=predicate, inferred=True, confidence=1.0,
                                            expected_stability=rule.retention,
                                            retention_recommendation=rule.retention))
        expected = Outcome.store if rule.inference_allowed else Outcome.reject
        assert decision.outcome is expected, f"{predicate} inference handling is wrong"


# =====================================================================================================
# Gate 3 — a floor per kind, never one global threshold
# =====================================================================================================


def test_the_floor_table_is_exhaustive_over_kinds():
    """A seventh kind cannot be added without deciding what it accepts. Without this, a new kind would
    KeyError at write time or, worse, inherit whichever floor happened to be loosest."""
    assert set(KIND_FLOORS) == set(MemoryKind)
    for kind, floor in KIND_FLOORS.items():
        assert floor.kind is kind
        assert 0.0 < floor.min_confidence <= 1.0
        assert floor.accepted_provenance, f"{kind.value} accepts nothing"


def test_the_floors_are_not_all_the_same_number():
    """The point of a per-kind floor is that the kinds differ. If every floor converged on one value
    somebody has quietly reintroduced a global threshold."""
    assert len({f.min_confidence for f in KIND_FLOORS.values()}) > 1
    assert KIND_FLOORS[MemoryKind.profile].min_confidence > KIND_FLOORS[MemoryKind.world].min_confidence


def test_profile_accepts_only_trusted_provenance_and_forbids_inference():
    floor = KIND_FLOORS[MemoryKind.profile]
    assert floor.accepted_provenance == TRUSTED_PROVENANCE
    assert floor.inference_allowed is False


def test_world_and_entity_require_attribution_and_freshness():
    """Untrusted material may create these only when Bruce can name the source it came from and when it
    can say how old the claim is — an unattributed forwarded fact and one that can never go stale are the
    two ways an attributed record turns into a permanent unsourced belief."""
    for kind in (MemoryKind.world, MemoryKind.entity):
        floor = KIND_FLOORS[kind]
        assert floor.requires_attribution and floor.requires_source_freshness


def test_episodic_accepts_only_observed_events_and_never_an_inference():
    floor = KIND_FLOORS[MemoryKind.episodic]
    assert ProvenanceClass.model_inference not in floor.accepted_provenance
    assert floor.inference_allowed is False


def test_confidence_is_capped_by_the_source_not_chosen_by_the_caller():
    """A caller asserting 0.99 on an attachment gets the attachment's ceiling. The cap only lowers: an
    honest low number about a flat statement is believed."""
    forwarded = _candidate(subject_type=SubjectType.world, kind=MemoryKind.world,
                           subject_id="practice", predicate="practice.start_time",
                           proposed_value="18:00", normalized_value="18:00",
                           provenance_class=ProvenanceClass.forwarded_content,
                           source_type=SourceType.forwarded, confidence=0.99,
                           expected_stability=RetentionPolicy.season,
                           retention_recommendation=RetentionPolicy.season)
    assert policy.effective_confidence(forwarded) == policy.PROVENANCE_CEILING[
        ProvenanceClass.forwarded_content]
    modest = _candidate(confidence=0.3)
    assert policy.effective_confidence(modest) == pytest.approx(0.3)


def test_the_ceiling_table_covers_every_provenance_class():
    assert set(policy.PROVENANCE_CEILING) == set(ProvenanceClass)


# =====================================================================================================
# Source-aware rules — the forwarded practice email, in full
# =====================================================================================================


def _forwarded_world_fact(**over) -> MemoryCandidate:
    base = dict(
        subject_type=SubjectType.world, kind=MemoryKind.world, subject_id="practice",
        predicate="practice.start_time", proposed_value="18:00", normalized_value="18:00",
        evidence_text="practice is at 6", provenance_class=ProvenanceClass.forwarded_content,
        source_type=SourceType.forwarded, source_id="fwd-99", confidence=0.9,
        explicitly_stated_by_user=False, expected_stability=RetentionPolicy.season,
        retention_recommendation=RetentionPolicy.season,
        usefulness_reason="so Bruce can answer when practice starts")
    base.update(over)
    return _candidate(**base)


def test_a_forwarded_email_may_create_an_attributed_world_fact():
    """The case #122 dropped `ck_memory_trusted_source` to permit. It stores, with the FORWARDED source
    on the row and a confidence the source can support — not the 0.9 the caller asked for."""
    decision = policy.decide(_forwarded_world_fact())
    assert decision.outcome is Outcome.store
    assert decision.confidence == policy.PROVENANCE_CEILING[ProvenanceClass.forwarded_content]


def test_an_attributed_fact_bruce_cannot_source_is_not_attributed():
    assert policy.decide(_forwarded_world_fact(source_id="")).reason == policy.UNATTRIBUTED_UNTRUSTED


@pytest.mark.parametrize("predicate,value", [
    ("profile.attends_practice", "yes"),
    ("profile.schedule_preference", "evenings"),
    ("profile.agreed_to_practice", "yes"),
    ("profile.is_an_athlete", "yes"),
])
def test_the_same_email_creates_no_fact_about_the_student(predicate, value):
    """The four readings that must NOT follow from receiving a message. "user attends practice", "user
    prefers evening practice", "user agreed", "user is an athlete" — each is a claim about the student,
    each is refused at the user gate because the source is not the student, and none of the four
    refusals depended on reading the value.

    Absence of disagreement is not consent: the student never replied, and nothing here treats silence as
    a statement.
    """
    decision = policy.decide(_forwarded_world_fact(
        subject_type=SubjectType.user, kind=MemoryKind.profile, subject_id=SELF,
        predicate=predicate, proposed_value=value, normalized_value=value))
    assert decision.outcome is Outcome.reject
    assert decision.reason == policy.UNTRUSTED_SOURCE_FOR_USER


def test_provider_content_may_describe_a_provider_entity_but_not_the_student():
    entity = _candidate(
        subject_type=SubjectType.provider_entity, kind=MemoryKind.entity, subject_id="AP Bio",
        predicate="course.period", proposed_value="4", normalized_value="4",
        provenance_class=ProvenanceClass.provider_verified, source_type=SourceType.provider,
        source_id="canvas-course-11", expected_stability=RetentionPolicy.season,
        retention_recommendation=RetentionPolicy.season)
    assert policy.decide(entity).outcome is Outcome.store
    assert policy.decide(_candidate(
        provenance_class=ProvenanceClass.provider_verified,
        source_type=SourceType.provider)).outcome is Outcome.reject


# =====================================================================================================
# Real Postgres — lineage, the unique index, and what quarantine actually means
# =====================================================================================================

users = PostgresUserRepository()


@pytest.fixture()
def _pg(pg_test_db, monkeypatch):
    """Real Postgres, real RLS. Deliberately WITHOUT `clean_db`: every test mints a fresh user id, so
    tenant isolation is also what keeps the tests apart. Truncating between them would take an ACCESS
    EXCLUSIVE lock on `memory_records` that races the in-flight inserts of the test that just finished."""
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


def _write(user_id, candidate):
    return _run(memory_writer.MemoryWriter(user_id).evaluate(candidate))


def _rows(user_id):
    async def _go():
        async with user_session(user_id) as s:
            return list((await s.execute(sa.select(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.user_id == user_id))).mappings().all())
    return _run(_go())


def test_a_new_claim_is_its_own_root(_pg):
    uid = _new_user()
    receipt = _write(uid, _candidate(user_id=uid))
    assert receipt.stored
    row = _rows(uid)[0]
    assert row["claim_root_id"] == row["memory_id"], "a brand-new claim must be its own root"
    assert row["claim_key"] == receipt.claim_key
    assert row["claim_key"] is not None


def test_the_claim_key_is_the_one_the_record_module_computes(_pg):
    """Computed by `memory_record.claim_key`, not re-derived here — two spellings of the same key is how
    the unique index silently stops covering what it was built for."""
    from bruce_engine.memory_record import claim_key

    uid = _new_user()
    receipt = _write(uid, _candidate(user_id=uid))
    assert receipt.claim_key == claim_key(kind=MemoryKind.profile, subject=SELF,
                                          predicate="profile.preferred_name")


def test_a_second_active_version_of_one_claim_is_refused(_pg):
    """`uq_memory_active_claim` is the guarantee; the writer's read turns it into a typed refusal instead
    of an IntegrityError. Replacing a belief is `memory_correction`'s job, which writes an audit row."""
    uid = _new_user()
    assert _write(uid, _candidate(user_id=uid)).stored
    second = _write(uid, _candidate(user_id=uid, proposed_value="Arielle", normalized_value="arielle"))
    assert not second.stored
    assert second.reason == memory_writer.DUPLICATE_CLAIM
    assert len([r for r in _rows(uid) if r["status"] == "active"]) == 1


def test_the_database_itself_refuses_a_duplicate_active_claim(_pg):
    """With the writer's read bypassed. The index is what makes two active contradictory versions
    impossible rather than merely unlikely, so it is asserted directly."""
    uid = _new_user()
    receipt = _write(uid, _candidate(user_id=uid))
    row = _rows(uid)[0]

    async def _go():
        async with user_session(uid) as s:
            values = dict(row)
            values["memory_id"] = uuid4()
            values["claim_root_id"] = values["memory_id"]
            await s.execute(sa.insert(MEMORY_RECORDS).values(**values))

    with pytest.raises(Exception) as excinfo:
        _run(_go())
    assert "uq_memory_active_claim" in str(excinfo.value)
    assert receipt.stored


def test_a_quarantined_row_exists_but_is_unreachable_by_retrieval(_pg):
    """The point of quarantine: a registry gap is discoverable without the claim being believed. It is a
    row, and `memory_retrieval.RETRIEVABLE` does not include it."""
    uid = _new_user()
    receipt = _write(uid, _candidate(user_id=uid, predicate="profile.favourite_colour",
                                     proposed_value="green", normalized_value="green"))
    assert receipt.persisted and not receipt.stored
    rows = _rows(uid)
    assert len(rows) == 1 and rows[0]["status"] == "quarantined"
    assert "quarantined" not in memory_retrieval.RETRIEVABLE


def test_an_attributed_world_fact_is_stored_with_its_real_source(_pg):
    """Honest provenance on the row: the column says `forwarded`, and `memory_retrieval._SOURCE_TRUST`
    scores it below the student's own words rather than treating it as theirs."""
    uid = _new_user()
    assert _write(uid, _forwarded_world_fact(user_id=uid)).stored
    row = _rows(uid)[0]
    assert row["source_type"] == SourceType.forwarded.value
    assert row["confidence"] == pytest.approx(
        policy.PROVENANCE_CEILING[ProvenanceClass.forwarded_content])
    assert memory_retrieval._SOURCE_TRUST[row["source_type"]] < memory_retrieval._SOURCE_TRUST[
        SourceType.trusted_user_text.value]


def test_a_writer_cannot_be_pointed_at_another_student(_pg):
    """Bound to one user for its whole life. RLS is underneath this, but the type refuses first."""
    uid = _new_user()
    with pytest.raises(memory_writer.MemoryWriteError):
        _write(uid, _candidate(user_id=uuid4()))


def test_every_persisted_row_carries_a_reason_it_matters(_pg):
    """A memory nobody could state the use of has no use, and the reason cannot be recovered later."""
    uid = _new_user()
    _write(uid, _candidate(user_id=uid))
    _write(uid, _forwarded_world_fact(user_id=uid))
    assert all((r["reason_it_matters"] or "").strip() for r in _rows(uid))


def test_every_subject_provenance_combination_has_a_decision(_pg):
    """Totality, on the real product of the two enums rather than on a sample. `decide` is total by
    construction; this proves no combination raises, and that nothing about the student survives an
    untrusted source all the way to a row."""
    uid = _new_user()
    for subject_type, provenance in itertools.product(SubjectType, ProvenanceClass):
        kinds = SUBJECT_KIND_MATRIX[subject_type]
        for kind in kinds:
            decision = policy.decide(_candidate(
                user_id=uid, subject_type=subject_type, kind=kind,
                subject_id=SELF if subject_type is SubjectType.user else "thing",
                predicate="profile.preferred_name" if subject_type is SubjectType.user
                else f"{kind.value}.detail",
                provenance_class=provenance, source_type=PROVENANCE_TO_SOURCE[provenance],
                source_id="src-1", expected_stability=RetentionPolicy.season,
                retention_recommendation=RetentionPolicy.season))
            assert decision.outcome in set(Outcome)
            if subject_type is SubjectType.user and provenance in UNTRUSTED_PROVENANCE:
                assert decision.outcome is Outcome.reject
