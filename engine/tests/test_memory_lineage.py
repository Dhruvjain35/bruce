"""Claim lineage, race-safe uniqueness, and migration parity — the three things #122 shipped without.

Each section here corresponds to a defect that was real rather than theoretical:

  * a corrected fact is two rows, and #122's fact-scoped forget reached only the newest one, leaving the
    previous value in the table and readable through provenance;
  * duplicate detection was read-then-compare, so two concurrent writes of the same fact both read "no
    duplicate" and both inserted;
  * `0001_initial_schema` calls `create_all()`, so a fresh database gets tables with no CHECK
    constraints, indexes, triggers or RLS policy, and a migration that guards its work behind "if absent"
    silently skips — leaving the invariants missing in exactly the environment nobody inspects.

The concurrency tests use SEPARATE connections and real transactions. A test that ran both writes on one
session would prove nothing about the case that actually happens.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
from sqlalchemy import select, text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import memory_correction, memory_forget, memory_record as mr
from bruce_engine import memory_retrieval as ret, schema
from bruce_engine.db import user_session
from bruce_engine.memory_retrieval import TurnCue
from bruce_engine.repositories import PostgresUserRepository
from tests import conftest as ct

users = PostgresUserRepository()
ENGINE = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _user():
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    return uid


def _write(uid, *, kind="relationships", subject="ap bio teacher",
           predicate="relationships.name", value="mr smith", status="active", claim_root=None,
           source_message_id="m-1"):
    # A RELATIONSHIP, not a profile fact. Corrections now run through the write policy, and
    # `profile.teacher_name` is not an allowlisted user-profile predicate — so a fixture using it would
    # be testing the registry rather than the lineage. That the default-deny registry caught this fixture
    # is the registry working.
    mid = uuid4()
    key = mr.claim_key(kind=kind, subject=subject, predicate=predicate)

    async def _go():
        async with user_session(uid) as s:
            s.add(schema.MemoryRecordRow(
                memory_id=mid, user_id=uid, kind=kind, subject=subject, predicate=predicate,
                value_json={"value": value}, normalized_value=mr.normalize(value)[:300],
                source_message_id=source_message_id, source_type="trusted_user_text", confidence=1.0,
                observed_at=NOW, retention_policy="durable", sensitivity="ordinary",
                user_editable=True, status=status, entity_key=mr.entity_key(subject),
                domain=(predicate or "").split(".", 1)[0],
                claim_root_id=claim_root or mid, claim_key=key))
    _run(_go())
    return mid


async def _rows(uid):
    async with user_session(uid) as s:
        return list((await s.execute(select(schema.MemoryRecordRow).where(
            schema.MemoryRecordRow.user_id == uid))).scalars().all())


# --- 6. CLAIM LINEAGE ------------------------------------------------------------------------------------

def test_a_correction_keeps_both_versions_in_one_lineage():
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _run(memory_correction.apply(uid, target_id=v1, new_value="ms smith",
                                       source_message_id="m-2"))
    assert res.applied
    rows = {str(r.memory_id): r for r in _run(_rows(uid))}
    assert len(rows) == 2
    roots = {str(r.claim_root_id) for r in rows.values()}
    assert roots == {str(v1)}, "the replacement started a new lineage instead of continuing one"
    keys = {r.claim_key for r in rows.values()}
    assert len(keys) == 1, "the claim key changed across a correction"


def test_forgetting_a_claim_takes_the_superseded_value_with_it():
    """THE #122 DEFECT. Forgetting the active version left "mr smith" in the table — the exact value the
    student asked Bruce to forget, one row below the one that was forgotten."""
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _run(memory_correction.apply(uid, target_id=v1, new_value="ms smith",
                                       source_message_id="m-2"))
    v2 = res.replacement_memory_id

    _run(memory_forget.forget(uid, scope=memory_forget.CLAIM, target=v2))

    for row in _run(_rows(uid)):
        assert row.status == "forgotten", f"{row.memory_id} survived a claim-scoped forget"
        assert row.normalized_value is None and row.value_json is None
        assert row.subject is None and row.predicate is None
    assert _run(ret.all_active(uid)) == []


def test_a_forgotten_historical_value_does_not_return_through_any_path():
    """Provenance, correction history, cache, ranking and model context are five different ways the same
    string can come back. The redaction is what closes all of them at once — there is no value left to
    return."""
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _run(memory_correction.apply(uid, target_id=v1, new_value="ms smith",
                                       source_message_id="m-2"))
    cue = TurnCue(text="who is my teacher", subjects=(mr.SELF,), domains=("profile",))
    _run(ret.retrieve(uid, cue))                                    # warm the cache first
    _run(memory_forget.forget(uid, scope=memory_forget.CLAIM, target=res.replacement_memory_id))

    ctx = _run(ret.retrieve(uid, cue))
    assert ctx.items == () and ctx.cache_hit is False
    everything = " ".join(str(r.normalized_value) + str(r.value_json) for r in _run(_rows(uid)))
    assert "smith" not in everything.lower()

    # ...including through the correction audit row, which links the ids but never carried the values.
    async def _audit():
        async with user_session(uid) as s:
            return (await s.execute(select(schema.MemoryCorrectionRow).where(
                schema.MemoryCorrectionRow.user_id == uid))).scalars().all()
    for row in _run(_audit()):
        assert "smith" not in (row.reason or "")


def test_forgetting_one_version_leaves_the_rest_of_the_lineage():
    """`version` still exists and still means one row. Both scopes are needed: an ordinary "forget that"
    means the claim, and a targeted correction rollback means the version."""
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _run(memory_correction.apply(uid, target_id=v1, new_value="ms smith",
                                       source_message_id="m-2"))
    _run(memory_forget.forget(uid, scope=memory_forget.VERSION, target=v1))
    rows = {str(r.memory_id): r for r in _run(_rows(uid))}
    assert rows[str(v1)].status == "forgotten"
    assert rows[res.replacement_memory_id].status == "active"


def test_fact_scope_now_means_the_lineage():
    """`FACT` used to address one row. That alias was the bug, so it now points at the claim — a caller
    written against #122 gets the behaviour a person expects rather than the one that shipped."""
    assert memory_forget.FACT == memory_forget.CLAIM


def test_forget_all_reaches_everything_and_asks_first():
    uid = _user()
    _write(uid, predicate="profile.timezone", value="America/Chicago")
    _write(uid, kind="relationships", subject="coach", predicate="relationships.role", value="track")
    pv = _run(memory_forget.preview(uid, scope=memory_forget.ALL, target=None))
    assert pv.count == 2 and pv.needs_confirmation is True
    assert _run(memory_forget.forget(uid, scope=memory_forget.ALL, target=None)).forgotten == 2
    assert _run(ret.all_active(uid)) == []


def test_forgetting_everything_for_one_student_does_not_touch_another():
    a, b = _user(), _user()
    _write(a, predicate="profile.timezone", value="America/Chicago")
    _write(b, predicate="profile.timezone", value="Europe/London")
    _run(memory_forget.forget(a, scope=memory_forget.ALL, target=None))
    assert len(_run(ret.all_active(b))) == 1


# --- 7. RACE-SAFE DUPLICATE PREVENTION -------------------------------------------------------------------

async def _raw_insert(url: str, uid: UUID, *, value: str, delay: float = 0.0):
    """One INSERT on its OWN connection, in its own transaction. Separate connections are the point: a
    test that used one session would serialise the writes itself and prove nothing."""
    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.user_id', $1, true)", str(uid))
            if delay:
                await asyncio.sleep(delay)
            await conn.execute(
                """INSERT INTO memory_records (memory_id, user_id, kind, subject, predicate,
                   value_json, normalized_value, source_type, confidence, observed_at,
                   retention_policy, sensitivity, user_editable, status, entity_key, domain,
                   claim_root_id, claim_key)
                   VALUES ($1,$2,'profile','self','profile.timezone',$3::jsonb,$4,'trusted_user_text',
                           1.0, now(), 'durable','ordinary', true, 'active','self','profile',$1,$5)""",
                uuid4(), uid, f'{{"value": "{value}"}}', value,
                mr.claim_key(kind="profile", subject="self", predicate="profile.timezone"))
    finally:
        await conn.close()


def _asyncpg_url() -> str:
    from sqlalchemy.engine import make_url
    u = make_url(os.environ["BRUCE_DATABASE_URL"])
    return f"postgresql://{u.username}:{u.password}@{u.host}:{u.port or 5432}/{u.database}"


def test_concurrent_identical_writes_leave_exactly_one_active_row(pg_test_db):
    """Read-then-compare let both of these land. The partial unique index makes the second one an
    integrity error, which is a thing the writer can handle, rather than a duplicate nobody notices."""
    uid = _user()
    url = _asyncpg_url()

    async def _both():
        return await asyncio.gather(
            _raw_insert(url, uid, value="America/Chicago"),
            _raw_insert(url, uid, value="America/Chicago"),
            return_exceptions=True)

    results = _run(_both())
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 1, f"both writes landed: {results}"
    assert "uq_memory_active_claim" in str(failures[0])
    assert len([r for r in _run(_rows(uid)) if r.status == "active"]) == 1


def test_concurrent_conflicting_writes_can_never_produce_two_active_facts(pg_test_db):
    """The more important half. Two DIFFERENT values for the same claim, written at once, would leave
    Bruce holding two contradictory beliefs and picking one at retrieval time."""
    uid = _user()
    url = _asyncpg_url()

    async def _both():
        return await asyncio.gather(
            _raw_insert(url, uid, value="America/Chicago"),
            _raw_insert(url, uid, value="Europe/London", delay=0.01),
            return_exceptions=True)

    results = _run(_both())
    assert len([r for r in results if isinstance(r, Exception)]) == 1
    active = [r for r in _run(_rows(uid)) if r.status == "active"]
    assert len(active) == 1
    assert _run(memory_correction.active_conflicts(uid)) == []


def test_superseded_versions_of_a_claim_coexist_freely():
    """Only `active` is exclusive. If the index were a plain unique constraint, keeping history would be
    impossible — which is why it is partial."""
    uid = _user()
    v1 = _write(uid, predicate="profile.timezone", value="America/New_York")
    _run(memory_correction.apply(uid, target_id=v1, new_value="America/Chicago", source_message_id="m-2"))
    v3 = _run(memory_correction.apply(
        uid, target_id=UUID(_run(_active_id(uid))), new_value="Europe/London", source_message_id="m-3"))
    assert v3.applied
    rows = _run(_rows(uid))
    assert len(rows) == 3 and len([r for r in rows if r.status == "active"]) == 1


async def _active_id(uid):
    async with user_session(uid) as s:
        row = (await s.execute(select(schema.MemoryRecordRow.memory_id).where(
            schema.MemoryRecordRow.user_id == uid,
            schema.MemoryRecordRow.status == "active"))).scalar_one()
    return str(row)


def test_the_uniqueness_guarantee_is_in_the_database_not_the_application(pg_test_db):
    async def _indexes():
        conn = await ct._connect_test_db()
        try:
            return {r["indexname"]: r["indexdef"] for r in await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'memory_records'")}
        finally:
            await conn.close()
    idx = _run(_indexes())
    assert "uq_memory_active_claim" in idx
    # Postgres rewrites the predicate when it stores it, so match on its normalized form rather than
    # on the text the migration wrote.
    definition = idx["uq_memory_active_claim"]
    assert "UNIQUE" in definition
    assert "'active'" in definition and "WHERE" in definition, definition
    assert "claim_key" in definition and "user_id" in definition


# --- 8. MIGRATION PARITY ---------------------------------------------------------------------------------

async def _shape(dbname: str) -> dict:
    """Everything a migration is supposed to have created, read back from the catalog.

    Deliberately NOT read from `Base.metadata`: the whole failure mode is that Base implies constraints
    the database does not have, so asking Base what should exist would agree with itself.
    """
    from sqlalchemy.engine import make_url
    u = make_url(os.environ["BRUCE_DATABASE_URL"])
    conn = await asyncpg.connect(host=u.host, port=u.port or 5432, user=u.username,
                                 password=u.password, database=dbname)
    try:
        cols = await conn.fetch(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' ORDER BY 1,2")
        idx = await conn.fetch(
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY 1,2")
        checks = await conn.fetch(
            "SELECT conrelid::regclass::text AS t, conname FROM pg_constraint WHERE contype='c' ORDER BY 1,2")
        trig = await conn.fetch(
            "SELECT c.relname AS t, tg.tgname FROM pg_trigger tg JOIN pg_class c ON c.oid=tg.tgrelid "
            "WHERE NOT tg.tgisinternal ORDER BY 1,2")
        pol = await conn.fetch(
            "SELECT tablename, policyname FROM pg_policies WHERE schemaname='public' ORDER BY 1,2")
        rls = await conn.fetch(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' "
            "ORDER BY 1")
        return {
            "columns": [tuple(r) for r in cols],
            "indexes": [(r["tablename"], r["indexname"], r["indexdef"]) for r in idx],
            "checks": [(r["t"], r["conname"]) for r in checks],
            "triggers": [(r["t"], r["tgname"]) for r in trig],
            "policies": [(r["tablename"], r["policyname"]) for r in pol],
            "rls": [tuple(r) for r in rls],
        }
    finally:
        await conn.close()


def _alembic(dbname: str, target: str) -> None:
    from sqlalchemy.engine import make_url
    base = os.environ["BRUCE_DATABASE_URL"].rsplit("/", 1)[0]
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ENGINE / "alembic.ini"), "upgrade", target],
        cwd=str(ENGINE), env={**os.environ, "BRUCE_DATABASE_URL": f"{base}/{dbname}"},
        capture_output=True, text=True)
    assert r.returncode == 0, f"alembic upgrade {target} on {dbname} failed:\n{r.stderr[-3000:]}"



def test_a_fresh_database_and_an_upgraded_one_have_the_same_shape(pg_test_db):
    """THE `create_all` TRAP, turned into a test.

    `0001_initial_schema` calls `Base.metadata.create_all()`, so every table reachable from Base exists
    on a fresh database with no CHECK constraints, no indexes, no triggers and no RLS policy — because
    `create_all` emits none of those. A migration that guards its work behind "if the table is absent"
    then skips, and the invariants are missing in the environment nobody inspects.

    Two databases, both driven by real Alembic: one straight to head, one stopping at the previous
    production head and then upgrading. If they differ in any column, index, check, trigger or policy,
    the difference is the bug — regardless of which one is "right".
    """
    fresh, upgraded = f"{ct.TEST_DB}_fresh", f"{ct.TEST_DB}_upg"
    for name in (fresh, upgraded):
        _run(ct._admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        _run(ct._admin(f'CREATE DATABASE "{name}"'))
    try:
        _alembic(fresh, "head")
        _alembic(upgraded, "0029_memory_canonical")     # the previous production head
        _alembic(upgraded, "head")

        a, b = _run(_shape(fresh)), _run(_shape(upgraded))
        for facet in ("columns", "indexes", "checks", "triggers", "policies", "rls"):
            assert a[facet] == b[facet], (
                f"{facet} differ between a fresh and an upgraded database\n"
                f"  only fresh:    {sorted(set(map(str, a[facet])) - set(map(str, b[facet])))[:8]}\n"
                f"  only upgraded: {sorted(set(map(str, b[facet])) - set(map(str, a[facet])))[:8]}")

        # ...and the memory invariants are actually there, in BOTH, rather than merely matching.
        names = {i[1] for i in a["indexes"]}
        assert {"uq_memory_active_claim", "ix_memory_shortlist", "ix_memory_entity"} <= names
        assert ("memory_records", "memory_records_no_edit") in a["triggers"]
        assert ("memory_records", "tenant_isolation") in a["policies"]
        checks = {c[1] for c in a["checks"]}
        assert {"ck_memory_forgotten_redacted", "ck_memory_forgotten_status"} <= checks
        assert ("memory_records", True, True) in a["rls"], "RLS is not forced on a fresh database"
    finally:
        for name in (fresh, upgraded):
            _run(ct._admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_no_memory_module_declares_its_own_table_metadata():
    """One table definition. #122 found the writer still querying a private Core copy whose columns the
    migration had renamed — silently writing against a shape the database no longer had."""
    import ast

    engine = ENGINE / "bruce_engine"
    for path in sorted(engine.glob("memory_*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                assert name != "MetaData", f"{path.name} declares its own MetaData"
                if name == "Table":
                    raise AssertionError(f"{path.name} declares a private Table")


# --- #124A: THE CORRECTION PATH IS THE WRITE PATH --------------------------------------------------------
# Before this, `memory_correction` inserted its own rows, so a correction met none of the write policy.
# Each case below is a way an untrusted "correction" used to reach the table.

def _correct(uid, target, value, *, source_type="trusted_user_text", message="m-2"):
    return _run(memory_correction.apply(uid, target_id=target, new_value=value,
                                        source_message_id=message, source_type=source_type))


def test_a_trusted_correction_succeeds_and_is_retrievable_immediately():
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _correct(uid, v1, "ms smith")
    assert res.applied and res.replacement_memory_id
    ctx = _run(ret.retrieve(uid, TurnCue(text="who teaches ap bio",
                                         entities=("ap bio teacher",))))
    facts = " ".join(i.fact for i in ctx.items)
    assert "ms smith" in facts and "mr smith" not in facts


@pytest.mark.parametrize("source", ["quoted", "forwarded", "attachment", "provider", "model"])
def test_an_untrusted_correction_cannot_retire_a_belief(source):
    """A forwarded email that contradicts something the student said is EVIDENCE that one of the two is
    wrong, never an instruction about which."""
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _correct(uid, v1, "ms smith", source_type=source)
    assert not res.applied
    rows = _run(_rows(uid))
    assert len(rows) == 1 and rows[0].status == "active"
    assert rows[0].normalized_value == "mr smith"


def test_a_correction_of_an_unregistered_user_profile_predicate_is_refused():
    """The default-deny registry applies to corrections too. `profile.mood` is not a predicate the
    product has a reason to hold, and a correction is not a way in through the side."""
    uid = _user()
    v1 = _write(uid, kind="profile", subject=mr.SELF, predicate="profile.mood", value="fine")
    res = _correct(uid, v1, "tired")
    assert not res.applied and res.reason == "unregistered_user_predicate"
    assert _run(_rows(uid))[0].normalized_value == "fine"


def test_a_correction_naming_a_version_that_is_not_active_is_refused():
    uid = _user()
    v1 = _write(uid, value="mr smith")
    _correct(uid, v1, "ms smith")
    assert not _correct(uid, v1, "mrs smith", message="m-3").applied     # v1 is superseded now


def test_a_correction_cannot_reach_another_students_belief():
    a, b = _user(), _user()
    v1 = _write(a, value="mr smith")
    assert not _correct(b, v1, "ms smith").applied
    assert _run(_rows(a))[0].normalized_value == "mr smith"


def test_after_a_correction_exactly_one_version_is_active_and_lineage_holds():
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _correct(uid, v1, "ms smith")
    rows = _run(_rows(uid))
    assert len([r for r in rows if r.status == "active"]) == 1
    assert {str(r.claim_root_id) for r in rows} == {str(v1)}
    assert len({r.claim_key for r in rows}) == 1
    assert _run(memory_correction.active_conflicts(uid)) == []


def test_forget_after_a_correction_removes_the_whole_lineage():
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _correct(uid, v1, "ms smith")
    _run(memory_forget.forget(uid, scope=memory_forget.CLAIM, target=res.replacement_memory_id))
    for row in _run(_rows(uid)):
        assert row.status == "forgotten" and row.normalized_value is None
    assert "smith" not in " ".join(str(r.value_json) for r in _run(_rows(uid))).lower()


def test_the_correction_audit_row_still_links_both_sides():
    uid = _user()
    v1 = _write(uid, value="mr smith")
    res = _correct(uid, v1, "ms smith")

    async def _audit():
        async with user_session(uid) as s:
            return (await s.execute(select(schema.MemoryCorrectionRow).where(
                schema.MemoryCorrectionRow.user_id == uid))).scalars().all()
    rows = _run(_audit())
    assert len(rows) == 1
    assert str(rows[0].memory_id) == res.superseded_memory_id
    assert str(rows[0].replacement_memory_id) == res.replacement_memory_id
    assert "smith" not in (rows[0].reason or "")
