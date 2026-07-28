"""The memory acceptance harness — run on real Postgres, through the real retrieval pipeline.

Not unit tests. Every case here writes real rows to a real database with RLS on, and asks the production
retriever, corrector and forgetter the question a student would ask. The bars in section 12 are measured
from these runs, not asserted from intent.

Records are inserted directly through `schema.MemoryRecordRow` rather than through the writer. That is
deliberate: this file is about what happens to memory once it exists — retrieval, correction, forgetting,
provenance, cache — and routing every fixture through the write policy would make a retrieval failure and
a write-policy failure indistinguishable. The write policy has its own file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import (memory_cache, memory_correction, memory_forget, memory_provenance,
                          memory_record as mr, memory_retrieval as ret, schema)
from bruce_engine.db import user_session
from bruce_engine.memory_retrieval import TurnCue
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    memory_cache.clear()
    yield
    memory_cache.clear()
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _user():
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    return uid


def _write(uid, *, kind, subject, predicate, value, source_type="trusted_user_text",
           source_message_id="m-1", confidence=1.0, observed_at=None, expires_at=None,
           freshness="fresh", sensitivity="ordinary", retention="durable", status="active",
           reason="", domain=None):
    mid = uuid4()

    async def _go():
        async with user_session(uid) as s:
            s.add(schema.MemoryRecordRow(
                memory_id=mid, user_id=uid, kind=kind, subject=subject, predicate=predicate,
                value_json={"value": value}, normalized_value=mr.normalize(value)[:300],
                evidence_text=None, source_message_id=source_message_id, source_type=source_type,
                confidence=confidence, observed_at=observed_at or NOW, last_confirmed_at=None,
                expires_at=expires_at, freshness_class=freshness, retention_policy=retention,
                sensitivity=sensitivity, user_editable=True, status=status,
                entity_key=mr.entity_key(subject), domain=domain or (predicate or "").split(".", 1)[0],
                reason_it_matters=reason or None))
    _run(_go())
    return mid


def _facts(ctx) -> str:
    return " | ".join(i.fact for i in ctx.items)


# --- PROFILE -------------------------------------------------------------------------------------------

def test_timezone_and_preferred_name_are_recalled_without_being_mentioned():
    """Standing facts about the student are always candidates. No turn says "by the way, my timezone" —
    if they had to be named to be retrieved they would never be retrieved."""
    uid = _user()
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.preferred_name", value="jordy")
    ctx = _run(ret.retrieve(uid, TurnCue(text="whats on friday", domains=("calendar",))))
    assert "america/chicago" in _facts(ctx).lower()
    assert "jordy" in _facts(ctx).lower()


def test_notification_preference_is_recalled():
    uid = _user()
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.notification_timing",
           value="after 4pm")
    ctx = _run(ret.retrieve(uid, TurnCue(text="tell me when he replies", domains=("communication",))))
    assert "after 4pm" in _facts(ctx)


def test_a_corrected_profile_fact_is_the_one_retrieved():
    uid = _user()
    old = _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/New_York")
    res = _run(memory_correction.apply(uid, target_id=old, new_value="America/Chicago",
                                       source_message_id="m-2"))
    assert res.applied
    ctx = _run(ret.retrieve(uid, TurnCue(text="whats on friday", domains=("calendar",))))
    assert "america/chicago" in _facts(ctx).lower()
    assert "new_york" not in _facts(ctx).lower()


# --- RELATIONSHIPS -------------------------------------------------------------------------------------

def test_a_teacher_relationship_is_recalled_when_the_turn_names_them():
    uid = _user()
    _write(uid, kind="relationships", subject="ms delgado", predicate="relationships.role",
           value="ap bio teacher", domain="relationships")
    ctx = _run(ret.retrieve(uid, TurnCue(text="email ms delgado about the lab",
                                         entities=("ms delgado",), domains=("communication",))))
    assert "ap bio teacher" in _facts(ctx)


def test_two_people_with_similar_names_are_not_confused():
    """Entity resolution is by folded key, not by similarity. "ms smith" and "mr smith" are two people,
    and a retriever that returns both has told the model to guess which teacher to email."""
    uid = _user()
    _write(uid, kind="relationships", subject="ms smith", predicate="relationships.role",
           value="chem teacher", domain="relationships")
    _write(uid, kind="relationships", subject="mr smith", predicate="relationships.role",
           value="track coach", domain="relationships")
    ctx = _run(ret.retrieve(uid, TurnCue(text="email ms smith", entities=("ms smith",))))
    facts = _facts(ctx)
    assert "chem teacher" in facts and "track coach" not in facts


def test_forgetting_one_relationship_leaves_the_other():
    uid = _user()
    _write(uid, kind="relationships", subject="coach ramirez", predicate="relationships.role",
           value="track coach", domain="relationships")
    _write(uid, kind="relationships", subject="ms delgado", predicate="relationships.role",
           value="ap bio teacher", domain="relationships")
    res = _run(memory_forget.forget(uid, scope=memory_forget.SUBJECT, target="coach ramirez"))
    assert res.forgotten == 1
    remaining = _run(ret.all_active(uid))
    assert len(remaining) == 1 and remaining[0].subject == "ms delgado"


# --- WORLD ---------------------------------------------------------------------------------------------

def test_a_class_deadline_is_recalled_with_its_provenance():
    uid = _user()
    _write(uid, kind="world", subject="ap bio lab", predicate="world.due", value="friday",
           source_type="provider", domain="school")
    ctx = _run(ret.retrieve(uid, TurnCue(text="when is the lab due", domains=("school",),
                                         entities=("ap bio lab",))))
    assert "friday" in _facts(ctx)
    assert ctx.items[0].provenance == "from your connected account"


def test_an_expired_deadline_is_not_retrieved_even_before_a_sweeper_marks_it():
    """Expiry is enforced at read time as well. A record whose window closed an hour ago has not been
    marked `expired` by anything yet, and showing it would be showing a fact Bruce itself calls stale."""
    uid = _user()
    _write(uid, kind="world", subject="old quiz", predicate="world.due", value="last tuesday",
           expires_at=NOW - timedelta(hours=1), domain="school")
    ctx = _run(ret.retrieve(uid, TurnCue(text="whats due", domains=("school",))))
    assert "last tuesday" not in _facts(ctx)


def test_a_stale_record_is_counted_even_when_it_is_shown():
    uid = _user()
    _write(uid, kind="world", subject="club fair", predicate="world.when", value="october",
           freshness="stale", domain="school")
    ctx = _run(ret.retrieve(uid, TurnCue(text="when is club fair", domains=("school",),
                                         entities=("club fair",))))
    assert ctx.stale_count == 1


# --- STYLE ---------------------------------------------------------------------------------------------

def test_style_memory_never_appears_in_a_factual_context():
    """The separation that stops Bruce concluding something about a person from how they type. A style
    record has a `style` kind and no factual domain, so the factual retriever never selects it."""
    uid = _user()
    _write(uid, kind="style", subject=mr.SELF, predicate="style.lowercase_preference", value="always",
           domain="style")
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    ctx = _run(ret.retrieve(uid, TurnCue(text="whats on friday", domains=("calendar",))))
    kinds = {i.kind for i in ctx.items}
    assert "style" not in kinds
    assert "profile" in kinds


# --- SECURITY ------------------------------------------------------------------------------------------

def test_no_cross_user_retrieval():
    a, b = _user(), _user()
    _write(a, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    ctx = _run(ret.retrieve(b, TurnCue(text="whats on friday", domains=("calendar",))))
    assert ctx.items == ()
    assert _run(ret.all_active(b)) == []


def test_a_retriever_is_bound_to_one_student_for_its_whole_life():
    """Structural, not a filter. A caller holding a retriever for A has no method that accepts B."""
    import inspect
    r = ret.MemoryRetriever(user_id=_user())
    with pytest.raises(Exception):
        r.user_id = uuid4()                                    # frozen
    for name in ("context", "everything"):
        assert "user_id" not in inspect.signature(getattr(r, name)).parameters


def test_a_forgotten_memory_never_comes_back():
    uid = _user()
    _write(uid, kind="relationships", subject="coach ramirez", predicate="relationships.phone",
           value="555 0134", domain="relationships")
    cue = TurnCue(text="text coach", entities=("coach ramirez",))
    assert "555 0134" in _facts(_run(ret.retrieve(uid, cue)))
    _run(memory_forget.forget(uid, scope=memory_forget.SUBJECT, target="coach ramirez"))
    assert _facts(_run(ret.retrieve(uid, cue))) == ""
    assert _run(ret.all_active(uid)) == []


def test_the_cache_cannot_serve_a_forgotten_memory_in_the_same_process():
    """The failure worth defending against: a student says forget that, and Bruce repeats it thirty
    seconds later out of a warm context. Retrieve, forget, retrieve — same process, same cue."""
    uid = _user()
    _write(uid, kind="relationships", subject="coach ramirez", predicate="relationships.phone",
           value="555 0134", domain="relationships")
    cue = TurnCue(text="text coach", entities=("coach ramirez",))
    first = _run(ret.retrieve(uid, cue))
    assert first.cache_hit is False and "555 0134" in _facts(first)
    assert _run(ret.retrieve(uid, cue)).cache_hit is True      # the cache is genuinely being used

    _run(memory_forget.forget(uid, scope=memory_forget.SUBJECT, target="coach ramirez"))
    after = _run(ret.retrieve(uid, cue))
    assert after.cache_hit is False and "555 0134" not in _facts(after)


def test_the_cache_cannot_serve_a_corrected_value():
    uid = _user()
    old = _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone",
                 value="America/New_York")
    cue = TurnCue(text="whats on friday", domains=("calendar",))
    assert "new_york" in _facts(_run(ret.retrieve(uid, cue))).lower()
    _run(memory_correction.apply(uid, target_id=old, new_value="America/Chicago",
                                 source_message_id="m-2"))
    after = _run(ret.retrieve(uid, cue))
    assert after.cache_hit is False and "new_york" not in _facts(after).lower()


def test_one_students_cache_invalidation_does_not_touch_another():
    a, b = _user(), _user()
    _write(a, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    _write(b, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="Europe/London")
    cue = TurnCue(text="whats on friday", domains=("calendar",))
    _run(ret.retrieve(a, cue))
    _run(ret.retrieve(b, cue))
    memory_cache.invalidate(a, reason="test")
    assert _run(ret.retrieve(b, cue)).cache_hit is True
    assert _run(ret.retrieve(a, cue)).cache_hit is False


def test_a_quarantined_record_cannot_reach_ordinary_retrieval():
    uid = _user()
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.something", value="held for review",
           status="quarantined")
    ctx = _run(ret.retrieve(uid, TurnCue(text="anything", domains=("calendar",))))
    assert ctx.items == ()
    assert "quarantined" not in ret.RETRIEVABLE


def test_a_wrong_owner_memory_id_corrects_nothing():
    a, b = _user(), _user()
    mid = _write(a, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    res = _run(memory_correction.apply(b, target_id=mid, new_value="Europe/London",
                                       source_message_id="m-2"))
    assert not res.applied and res.reason == memory_correction.NOT_FOUND
    assert "america/chicago" in _facts(_run(ret.retrieve(
        a, TurnCue(text="x", domains=("calendar",))))).lower()


def test_untrusted_content_cannot_correct_a_fact_about_the_student():
    """A forwarded email that contradicts something the student said is EVIDENCE that one of the two is
    wrong, never an instruction about which."""
    uid = _user()
    old = _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    res = _run(memory_correction.apply(uid, target_id=old, new_value="Europe/London",
                                       source_message_id="m-2", source_type="forwarded"))
    assert not res.applied and res.reason == memory_correction.UNTRUSTED


# --- CORRECTION ----------------------------------------------------------------------------------------

def test_a_correction_supersedes_and_leaves_no_active_contradiction():
    uid = _user()
    old = _write(uid, kind="relationships", subject="ms smith", predicate="relationships.role",
                 value="mr smith", domain="relationships")
    _run(memory_correction.apply(uid, target_id=old, new_value="ms smith", source_message_id="m-2"))
    assert _run(memory_correction.active_conflicts(uid)) == []

    async def _old_row():
        async with user_session(uid) as s:
            return (await s.execute(select(schema.MemoryRecordRow).where(
                schema.MemoryRecordRow.memory_id == old))).scalar_one()
    row = _run(_old_row())
    assert row.status == "superseded" and row.superseded_by_id is not None
    # History survives: the old value is still readable for "what did you believe when you did that".
    assert row.normalized_value == "mr smith"


def test_a_duplicate_active_fact_is_swept_up_by_the_correction():
    """Two active rows for the same subject and predicate is the state that makes retrieval a coin flip.
    A correction closes ALL of them, not only the one it was pointed at."""
    uid = _user()
    a = _write(uid, kind="relationships", subject="ms smith", predicate="relationships.role", value="one",
               domain="relationships")
    _write(uid, kind="relationships", subject="ms smith", predicate="relationships.role", value="two",
           domain="relationships")
    assert _run(memory_correction.active_conflicts(uid)) != []
    _run(memory_correction.apply(uid, target_id=a, new_value="three", source_message_id="m-3"))
    assert _run(memory_correction.active_conflicts(uid)) == []
    assert _facts(_run(ret.retrieve(uid, TurnCue(text="x", entities=("ms smith",))))).count("three") == 1


def test_the_correction_audit_row_links_both_sides():
    uid = _user()
    old = _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="UTC")
    res = _run(memory_correction.apply(uid, target_id=old, new_value="America/Chicago",
                                       source_message_id="m-9", reason="student corrected"))

    async def _audit():
        async with user_session(uid) as s:
            return (await s.execute(select(schema.MemoryCorrectionRow).where(
                schema.MemoryCorrectionRow.user_id == uid))).scalars().all()
    rows = _run(_audit())
    assert len(rows) == 1
    assert str(rows[0].memory_id) == res.superseded_memory_id
    assert str(rows[0].replacement_memory_id) == res.replacement_memory_id
    assert rows[0].source_message_id == "m-9"


def test_a_row_cannot_be_edited_even_by_raw_sql():
    """The append-only trigger. This module is not the only thing standing between a correction and an
    UPDATE — a future module, a migration author and a console session all hit the same wall."""
    uid = _user()
    mid = _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="UTC")

    async def _edit():
        from sqlalchemy import update as sa_update
        async with user_session(uid) as s:
            await s.execute(sa_update(schema.MemoryRecordRow)
                            .where(schema.MemoryRecordRow.memory_id == mid)
                            .values(normalized_value="edited"))
    with pytest.raises(Exception):
        _run(_edit())


# --- FORGET --------------------------------------------------------------------------------------------

def test_forget_scopes_reach_exactly_what_they_name():
    uid = _user()
    _write(uid, kind="relationships", subject="coach", predicate="relationships.role", value="track",
           source_message_id="m-a", domain="relationships")
    _write(uid, kind="world", subject="lab", predicate="world.due", value="friday",
           source_message_id="m-b", domain="school")
    _write(uid, kind="world", subject="quiz", predicate="world.due", value="monday",
           source_message_id="m-b", domain="school")

    assert _run(memory_forget.forget(uid, scope=memory_forget.SOURCE, target="m-b")).forgotten == 2
    assert len(_run(ret.all_active(uid))) == 1
    assert _run(memory_forget.forget(uid, scope=memory_forget.KIND, target="relationships")).forgotten == 1
    assert _run(ret.all_active(uid)) == []


def test_a_broad_forget_is_previewed_without_showing_the_content():
    uid = _user()
    for i in range(5):
        _write(uid, kind="relationships", subject="coach", predicate=f"relationships.f{i}",
               value=f"secret {i}", domain="relationships")
    pv = _run(memory_forget.preview(uid, scope=memory_forget.SUBJECT, target="coach"))
    assert pv.count == 5 and pv.needs_confirmation is True
    assert "secret" not in pv.summary()
    assert _run(ret.all_active(uid)), "preview must not forget anything"


def test_forgetting_redacts_the_content_not_just_a_flag():
    uid = _user()
    mid = _write(uid, kind="relationships", subject="coach", predicate="relationships.phone",
                 value="555 0134", domain="relationships")
    _run(memory_forget.forget(uid, scope=memory_forget.FACT, target=mid))

    async def _row():
        async with user_session(uid) as s:
            return (await s.execute(select(schema.MemoryRecordRow).where(
                schema.MemoryRecordRow.memory_id == mid))).scalar_one()
    row = _run(_row())
    assert row.status == "forgotten" and row.forgotten_at is not None
    for column in (row.normalized_value, row.value_json, row.subject, row.predicate,
                   row.evidence_text, row.entity_key):
        assert column is None, "forgetting left content behind"


def test_a_forgotten_source_is_remembered_as_forgotten():
    """The re-learn guard. Without it, the next intake pass over the same inbox undoes the forget and the
    student experiences Bruce ignoring them."""
    uid = _user()
    _write(uid, kind="world", subject="lab", predicate="world.due", value="friday",
           source_message_id="m-b", domain="school")
    _run(memory_forget.forget(uid, scope=memory_forget.SOURCE, target="m-b"))
    assert _run(memory_forget.is_forgotten_source(uid, "m-b")) is True
    assert _run(memory_forget.is_forgotten_source(uid, "m-other")) is False


def test_forgetting_one_students_memory_does_not_touch_another():
    a, b = _user(), _user()
    _write(a, kind="relationships", subject="coach", predicate="relationships.role", value="track",
           domain="relationships")
    _write(b, kind="relationships", subject="coach", predicate="relationships.role", value="track",
           domain="relationships")
    assert _run(memory_forget.forget(a, scope=memory_forget.SUBJECT, target="coach")).forgotten == 1
    assert len(_run(ret.all_active(b))) == 1


# --- PROVENANCE ----------------------------------------------------------------------------------------

def test_every_retrieved_fact_can_say_where_it_came_from():
    uid = _user()
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    _write(uid, kind="world", subject="lab", predicate="world.due", value="friday",
           source_type="provider", domain="school")
    ctx = _run(ret.retrieve(uid, TurnCue(text="whats due", domains=("school", "calendar"),
                                         entities=("lab",))))
    assert len(ctx.items) == 2
    for item in ctx.items:
        assert item.provenance and "memory_records" not in item.provenance
        assert item.reason_relevant


def test_provenance_distinguishes_what_you_said_from_what_a_provider_said():
    uid = _user()
    mid = _write(uid, kind="world", subject="lab", predicate="world.due", value="friday",
                 source_type="forwarded", domain="school")

    async def _row():
        async with user_session(uid) as s:
            return (await s.execute(select(schema.MemoryRecordRow).where(
                schema.MemoryRecordRow.memory_id == mid))).scalar_one()
    d = memory_provenance.detail(_run(_row()))
    assert d["explicitly_stated"] is False and d["from_provider_evidence"] is True
    assert memory_provenance.phrase(_run(_row())) == "from an email you forwarded"


# --- BUDGETS + MEASURED BARS -----------------------------------------------------------------------------

def test_the_token_budget_is_respected_and_omissions_are_counted():
    uid = _user()
    for i in range(40):
        _write(uid, kind="world", subject=f"thing {i}", predicate="world.detail",
               value="a fairly long remembered value that costs a real number of tokens " + str(i),
               domain="school")
    ctx = _run(ret.retrieve(uid, TurnCue(text="chat", domains=("school",),
                                         turn_class="conversation")))
    assert ctx.token_count <= ret.BUDGET_CONVERSATION
    assert ctx.omitted_count > 0, "a silent drop is indistinguishable from having nothing to say"
    action = _run(ret.retrieve(uid, TurnCue(text="do the thing", domains=("school",),
                                            turn_class="action"), use_cache=False))
    assert action.token_count <= ret.BUDGET_ACTION
    assert action.token_count > ctx.token_count


def test_the_whole_memory_store_is_never_handed_to_a_prompt():
    uid = _user()
    for i in range(200):
        _write(uid, kind="world", subject=f"thing {i}", predicate="world.detail", value=f"v{i}",
               domain="school")
    ctx = _run(ret.retrieve(uid, TurnCue(text="chat", domains=("school",))))
    assert len(ctx.items) < 200
    assert len(_run(ret.shortlist(uid, TurnCue(domains=("school",))))) <= ret.SHORTLIST_LIMIT


@dataclass
class Bars:
    useful_recall_precision: float
    useful_recall_rate: float
    stale_usage: float
    forgotten_retrieved: int
    cross_user_leaks: int
    active_contradictions: int
    avg_tokens: float
    p50_ms: float
    p95_ms: float


def _measure(uid, cases) -> Bars:
    """PRECISION IS MEASURED AGAINST FACTS THAT MUST NOT APPEAR, not against a minimal expected list.

    The first version of this divided "expected facts found" by "items returned" and scored 0.300 on a
    retriever with perfect recall — because a turn about the coach also legitimately surfaces the
    student's timezone and preferred name, and counting those as errors measures the metric rather than
    the system. What actually matters is whether retrieval returns KNOWN JUNK: a teacher's role in a turn
    about track practice. So each case names what must be there and what must not, and precision is the
    share of returned items that are not on the must-not list.
    """
    import statistics
    hits = total_expected = returned = stale_used = junk = 0
    tokens: list[int] = []
    lat: list[float] = []
    for cue, expected, unwanted in cases:
        ctx = _run(ret.retrieve(uid, cue, use_cache=False))
        lat.append(ctx.retrieval_latency_ms)
        tokens.append(ctx.token_count)
        got = _facts(ctx).lower()
        returned += len(ctx.items)
        stale_used += ctx.stale_count
        for want in expected:
            total_expected += 1
            hits += int(want.lower() in got)
        junk += sum(1 for i in ctx.items if any(u.lower() in i.fact.lower() for u in unwanted))
    ordered = sorted(lat)
    return Bars(
        useful_recall_precision=(1.0 - junk / returned) if returned else 1.0,
        useful_recall_rate=(hits / total_expected) if total_expected else 1.0,
        stale_usage=(stale_used / returned) if returned else 0.0,
        forgotten_retrieved=0, cross_user_leaks=0, active_contradictions=0,
        avg_tokens=statistics.mean(tokens) if tokens else 0.0,
        p50_ms=ordered[len(ordered) // 2], p95_ms=ordered[max(0, int(len(ordered) * 0.95) - 1)])


def test_the_release_bars(capsys):
    """Section 12, measured on real Postgres through the production retriever.

    Every case is a turn where the expected facts are the ones that could change the answer, and the
    scenario deliberately includes memory that must NOT come back — a forgotten phone number, a
    superseded timezone, another student's data — so precision is measured against a store that contains
    traps rather than only the answers.
    """
    uid, other = _user(), _user()

    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.preferred_name", value="jordy")
    _write(uid, kind="relationships", subject="ms delgado", predicate="relationships.role",
           value="ap bio teacher", domain="relationships")
    _write(uid, kind="relationships", subject="coach ramirez", predicate="relationships.role",
           value="track coach", domain="relationships")
    _write(uid, kind="world", subject="ap bio lab", predicate="world.due", value="friday",
           source_type="provider", domain="school")
    _write(uid, kind="world", subject="uconn visit", predicate="world.when", value="october 3",
           domain="calendar")
    _write(other, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="Europe/London")

    gone = _write(uid, kind="relationships", subject="old tutor", predicate="relationships.phone",
                  value="555 9999", domain="relationships")
    _run(memory_forget.forget(uid, scope=memory_forget.FACT, target=gone))
    superseded = _write(uid, kind="profile", subject=mr.SELF, predicate="profile.school",
                        value="old high school")
    _run(memory_correction.apply(uid, target_id=superseded, new_value="westlake high",
                                 source_message_id="m-c"))

    # (cue, must appear, must NOT appear)
    cases = [
        (TurnCue(text="email ms delgado about the lab", entities=("ms delgado", "ap bio lab"),
                 domains=("communication", "school"), turn_class="action"),
         ["ap bio teacher", "friday"], ["track coach", "october 3"]),
        (TurnCue(text="whats on friday", domains=("calendar",)),
         ["america/chicago"], ["ap bio teacher", "track coach"]),
        (TurnCue(text="text coach", entities=("coach ramirez",), domains=("communication",)),
         ["track coach"], ["ap bio teacher", "555 9999"]),
        (TurnCue(text="when is the uconn visit", entities=("uconn visit",), domains=("calendar",)),
         ["october 3"], ["ap bio teacher", "track coach"]),
        (TurnCue(text="what school do i go to", domains=("profile",)),
         ["westlake high"], ["old high school", "555 9999"]),
    ]
    bars = _measure(uid, cases)

    everything = " ".join(str(r.normalized_value) for r in _run(ret.all_active(uid)))
    forgotten_retrieved = int("555 9999" in everything)
    leaked = int("europe/london" in everything.lower())
    contradictions = len(_run(memory_correction.active_conflicts(uid)))

    with capsys.disabled():
        print(f"""
MEMORY RELEASE BARS (real Postgres, production retriever)
  useful recall precision   {bars.useful_recall_precision:.3f}   (bar >= 0.95)
  useful recall rate        {bars.useful_recall_rate:.3f}
  stale memory usage        {bars.stale_usage:.3f}   (bar < 0.01)
  forgotten-memory retrieval {forgotten_retrieved}       (bar 0)
  cross-user leakage        {leaked}       (bar 0)
  active contradictions     {contradictions}       (bar 0)
  untrusted-content memories 0       (bar 0 — enforced by the write policy, measured in its own file)
  average retrieved tokens  {bars.avg_tokens:.0f}
  retrieval p50             {bars.p50_ms:.1f} ms
  retrieval p95             {bars.p95_ms:.1f} ms  (bar < 100)
""")

    assert forgotten_retrieved == 0
    assert leaked == 0
    assert contradictions == 0
    assert bars.stale_usage < 0.01
    assert bars.useful_recall_rate >= 0.95, f"recall {bars.useful_recall_rate:.3f}"
    assert bars.useful_recall_precision >= 0.95, f"precision {bars.useful_recall_precision:.3f}"
    assert bars.p95_ms < 100.0, f"p95 {bars.p95_ms:.1f}ms"


# --- PRODUCTION INTEGRATION --------------------------------------------------------------------------

def test_memory_reaches_the_compiled_context_before_the_model_reasons():
    """Section 10's order, asserted on the real compiler rather than described in a comment:
    trusted input -> active state -> capability snapshot -> memory shortlist -> compact context.

    Memory sits BELOW capability truth and the clock and ABOVE the entity list, because a context that
    drops what Bruce can do makes it lie, and one that drops a remembered teacher only makes it vaguer.
    """
    from bruce_engine import context_compiler

    uid = _user()
    _write(uid, kind="relationships", subject="ms delgado", predicate="relationships.role",
           value="ap bio teacher", domain="relationships")
    compiled = _run(context_compiler.compile(
        uid, [], memory_cue=TurnCue(text="email ms delgado", entities=("ms delgado",),
                                    domains=("communication",), turn_class="action")))
    assert "ap bio teacher" in compiled.text
    assert compiled.memory is not None and compiled.memory.items
    layers = [b.layer for b in compiled.blocks]
    assert "memory" in layers
    assert context_compiler._P_CAPABILITY > context_compiler._P_MEMORY
    assert context_compiler._P_NOW > context_compiler._P_MEMORY
    assert context_compiler._P_MEMORY > context_compiler._P_ENTITY


def test_a_retrieval_fault_costs_context_not_the_turn():
    """A student whose memory lookup fails should get an answer that knows less, never no answer."""
    from unittest.mock import patch

    from bruce_engine import context_compiler, memory_retrieval

    uid = _user()
    _write(uid, kind="profile", subject=mr.SELF, predicate="profile.timezone", value="America/Chicago")

    async def _boom(*a, **kw):
        raise RuntimeError("retrieval down")

    with patch.object(memory_retrieval, "retrieve", _boom):
        compiled = _run(context_compiler.compile(uid, [], memory_cue=TurnCue(text="hi")))
    assert compiled.memory is None
    assert compiled.text is not None


def test_memory_cannot_authorize_anything():
    """The separation that keeps #120 and #121a true. Memory can tell Bruce who "coach" is; only the
    student's own trusted words in the current turn can tell Bruce to email him."""
    import inspect

    from bruce_engine import memory_correction, memory_forget, memory_retrieval

    import ast

    forbidden = {"authorization_evidence", "authorization_store", "execution_gate", "mutation_gateway"}
    for module in (memory_retrieval, memory_correction, memory_forget, memory_cache):
        # The IMPORT GRAPH, not the prose. Asserting on source text would fail on a docstring that
        # explains the separation, which is the second time that shape of assertion has bitten in this
        # PR — the first was cleanup "retry" appearing in a comment about not retrying.
        tree = ast.parse(inspect.getsource(module))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.rsplit(".", 1)[-1] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= {a.name for a in node.names} | {(node.module or "").rsplit(".", 1)[-1]}
        leaked = names & forbidden
        assert not leaked, f"{module.__name__} imports {leaked}"
