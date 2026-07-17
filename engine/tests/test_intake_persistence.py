"""INCREMENT B — durable /v1/intake against REAL Postgres (source -> spans -> tasks, under RLS).

Same rules as test_postgres_integration.py: disposable ``bruce_test`` database, assertions through
the restricted ``bruce_app`` role, real FastAPI app with minted HS256 JWTs, real repositories. No
SQLite, no mocks, no in-memory repos. The ONLY thing faked is the LLM extraction itself — injected
so success and failure are deterministic; every write under test is real.

Exit gate for this increment: a real intake request creates durable, isolated source/span/task
records that survive restart and are retrievable only by the owning user.
"""

from __future__ import annotations

import asyncio
import datetime
import os
import time
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.api as api
import bruce_engine.db as db
from bruce_engine import intake_store, retention, schema
from bruce_engine.db import user_session
from bruce_engine.models import ExtractedDeadline, ExtractedIntake, IntakeSourceKind, RequiredItem
from bruce_engine.repositories import PostgresSourceRepository, PostgresTaskRepository

sources = PostgresSourceRepository()
tasks = PostgresTaskRepository()
client = TestClient(api.app)

RAW = "Northgate Science Fair 2026. Registration closes Feb 28, 2026. Projects due Mar 14, 2026. $25 fee."


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    """Rebuild the app engine per test with NullPool (real asyncpg/real PG, no cross-loop pooling)."""

    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _token(uid: UUID) -> str:
    payload = {"sub": str(uid), "exp": int(time.time()) + 3600}
    aud = os.environ.get("BRUCE_JWT_AUDIENCE")
    if aud:
        payload["aud"] = aud
    return jwt.encode(payload, os.environ["BRUCE_JWT_SECRET"], algorithm="HS256")


def _auth(uid: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(uid)}"}


def _intake() -> ExtractedIntake:
    """Two grounded deadlines + a fee — the shape the original verified sample produced."""
    return ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        title="Northgate Science Fair 2026",
        deadlines=[
            ExtractedDeadline(label="Registration closes", date="2026-02-28",
                              source_span="Registration closes Feb 28, 2026.", confidence=0.95),
            ExtractedDeadline(label="Projects due", date="2026-03-14",
                              source_span="Projects due Mar 14, 2026.", confidence=0.93),
        ],
        required_items=[RequiredItem(name="$25 fee", kind="fee")],
    )


def _fake_extract(intake: ExtractedIntake | None = None):
    async def _extract(text, source_kind=IntakeSourceKind.text):
        return intake if intake is not None else _intake()

    return _extract


def _patch_extract(monkeypatch, intake: ExtractedIntake | None = None) -> None:
    monkeypatch.setattr(api, "extract_from_text", _fake_extract(intake))


async def _count(model, user_id: UUID) -> int:
    async with user_session(user_id) as s:
        return int(
            (await s.execute(select(func.count()).select_from(model).where(model.user_id == user_id))).scalar_one()
        )


# --------------------------------------------------------------------------- 1


def test_successful_authenticated_intake_persists_source_spans_tasks(clean_db, monkeypatch):
    """The happy path: 200, and real rows land — 1 source, 2 spans, 2 tasks, all owned by the caller."""
    _patch_extract(monkeypatch)
    uid = uuid4()

    r = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid))
    assert r.status_code == 200
    body = r.json()

    # the extraction contract is unchanged — original fields at their original paths
    assert body["title"] == "Northgate Science Fair 2026"
    assert len(body["deadlines"]) == 2
    # ...plus the new durable ids
    assert body["source_id"] and len(body["span_ids"]) == 2 and len(body["task_ids"]) == 2

    async def check():
        assert await _count(schema.Source, uid) == 1
        assert await _count(schema.SourceSpan, uid) == 2
        assert await _count(schema.TaskRow, uid) == 2

    asyncio.run(check())


# --------------------------------------------------------------------------- 2


def test_unauthenticated_intake_is_401_and_writes_nothing(clean_db):
    """No token -> 401 before anything is computed or written."""
    r = client.post("/v1/intake/sync", json={"text": RAW})
    assert r.status_code == 401

    async def check():
        async with user_session(uuid4()) as s:  # any context: assert the table is globally empty
            total = (await s.execute(select(func.count()).select_from(schema.Source))).scalar_one()
        assert int(total) == 0

    asyncio.run(check())


# --------------------------------------------------------------------------- 3


def test_student_A_cannot_access_student_B_intake_records(clean_db, monkeypatch):
    """B's ids are useless to A: RLS-scoped repo reads return None, and A's own view stays empty."""
    _patch_extract(monkeypatch)
    a, b = uuid4(), uuid4()

    body = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(b)).json()
    b_source = UUID(body["source_id"])
    b_task = UUID(body["task_ids"][0])

    async def check():
        # A holds B's ids and still cannot read them (indistinguishable from missing).
        assert await sources.get_for_user(b_source, a) is None
        assert await tasks.get_for_user(b_task, a) is None
        # A's own records: none. B's rows never leak into A's list.
        assert await _count(schema.Source, a) == 0
        assert await tasks.list_for_user(a) == []
        # B still owns them.
        assert (await sources.get_for_user(b_source, b)) is not None

    asyncio.run(check())


# --------------------------------------------------------------------------- 4


def test_persisted_intake_survives_server_restart(clean_db, monkeypatch):
    """Dispose+reset the engine (simulated restart); source, spans and tasks are still there."""
    _patch_extract(monkeypatch)
    uid = uuid4()
    body = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()
    source_id = UUID(body["source_id"])
    task_ids = [UUID(t) for t in body["task_ids"]]

    async def check():
        await db._engine.dispose()  # simulate a process restart
        db._engine = None
        db._sessionmaker = None

        again = await sources.get_for_user(source_id, uid)
        assert again is not None and again.id == source_id
        assert await _count(schema.SourceSpan, uid) == 2
        for tid in task_ids:
            assert (await tasks.get_for_user(tid, uid)) is not None

    asyncio.run(check())


# --------------------------------------------------------------------------- 5


def test_duplicate_request_is_idempotent(clean_db, monkeypatch):
    """Same user + same content twice -> same ids, one source, no duplicate spans or tasks."""
    _patch_extract(monkeypatch)
    uid = uuid4()

    first = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()
    second = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()

    assert first["source_id"] == second["source_id"]
    # ORDER matters, not just membership: a replay that returns the same ids shuffled would break
    # span_ids[i] <-> deadlines[i] correspondence for the client.
    assert first["task_ids"] == second["task_ids"]
    assert first["span_ids"] == second["span_ids"]
    assert second["title"] == "Northgate Science Fair 2026"  # replayed, not re-extracted
    assert second["deadlines"] == first["deadlines"]

    async def check():
        assert await _count(schema.Source, uid) == 1
        assert await _count(schema.SourceSpan, uid) == 2
        assert await _count(schema.TaskRow, uid) == 2

    asyncio.run(check())


def test_concurrent_duplicate_intakes_create_exactly_one_source(clean_db):
    """The adversarial case for idempotency: two identical intakes racing IN PARALLEL.

    A check-then-insert implementation passes the sequential test above and still double-writes
    here. The UNIQUE(user_id, idempotency_key) constraint is the actual guarantee: both coroutines
    attempt the INSERT, exactly one wins, the loser replays the winner's row.
    """
    uid = uuid4()

    async def run():
        from bruce_engine.repositories import PostgresUserRepository

        await PostgresUserRepository().ensure(uid)

        async def slow_extract(text, source_kind=IntakeSourceKind.text):
            await asyncio.sleep(0.05)  # widen the window between the lookup and the insert
            return _intake()

        a, b = await asyncio.gather(
            intake_store.persist_intake(
                user_id=uid, text=RAW, source_kind=IntakeSourceKind.text, extract=slow_extract
            ),
            intake_store.persist_intake(
                user_id=uid, text=RAW, source_kind=IntakeSourceKind.text, extract=slow_extract
            ),
        )

        assert a.source_id == b.source_id  # one logical intake, not two
        assert a.task_ids == b.task_ids
        assert await _count(schema.Source, uid) == 1  # not 2
        assert await _count(schema.SourceSpan, uid) == 2  # not 4
        assert await _count(schema.TaskRow, uid) == 2  # not 4
        assert a.replayed != b.replayed  # exactly one wrote; the other replayed

    asyncio.run(run())


def test_same_content_from_two_users_are_separate_intakes(clean_db, monkeypatch):
    """Idempotency is PER USER — B pasting the same text must not collide with A's source."""
    _patch_extract(monkeypatch)
    a, b = uuid4(), uuid4()

    ra = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(a)).json()
    rb = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(b)).json()
    assert ra["source_id"] != rb["source_id"]

    async def check():
        assert await _count(schema.Source, a) == 1
        assert await _count(schema.Source, b) == 1

    asyncio.run(check())


# --------------------------------------------------------------------------- 6 + 10


def test_malformed_extraction_rolls_back_cleanly(clean_db, monkeypatch):
    """Extraction raises -> 502, and NOTHING is left behind (no orphan source)."""

    async def boom(text, source_kind=IntakeSourceKind.text):
        raise ValueError("model returned unparseable output")

    monkeypatch.setattr(api, "extract_from_text", boom)
    uid = uuid4()

    r = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid))
    assert r.status_code == 502
    assert r.json()["detail"] == "extraction failed: ValueError"

    async def check():
        assert await _count(schema.Source, uid) == 0  # the source was rolled back with the tx

    asyncio.run(check())


def test_no_partial_records_after_forced_failure_mid_write(clean_db, monkeypatch):
    """Fail AFTER spans/tasks are staged: the whole transaction unwinds — no source, span or task.

    This is the one that proves atomicity rather than mere ordering: the failure is injected at the
    last write, so a non-transactional implementation would leave a source and its spans behind.
    """
    _patch_extract(monkeypatch)
    uid = uuid4()
    real_to_tasks = intake_store.tasks_mod.intake_to_tasks

    def explode(intake, source=None):
        real_to_tasks(intake, source=source)  # spans already inserted at this point
        raise RuntimeError("forced failure after source+spans were staged")

    monkeypatch.setattr(intake_store.tasks_mod, "intake_to_tasks", explode)

    r = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid))
    assert r.status_code == 502

    async def check():
        assert await _count(schema.Source, uid) == 0
        assert await _count(schema.SourceSpan, uid) == 0
        assert await _count(schema.TaskRow, uid) == 0

    asyncio.run(check())


# --------------------------------------------------------------------------- 7


def test_source_spans_point_at_the_correct_source(clean_db, monkeypatch):
    """Every span belongs to THIS source, carries the verbatim grounding text, and is owned by the user."""
    _patch_extract(monkeypatch)
    uid = uuid4()
    body = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()
    source_id = UUID(body["source_id"])

    async def check():
        async with user_session(uid) as s:
            spans = (await s.execute(select(schema.SourceSpan).where(schema.SourceSpan.user_id == uid))).scalars().all()
        assert len(spans) == 2
        assert all(sp.source_id == source_id and sp.user_id == uid for sp in spans)
        # grounding is verbatim source text, not a paraphrase
        assert {sp.span_text for sp in spans} == {
            "Registration closes Feb 28, 2026.",
            "Projects due Mar 14, 2026.",
        }
        assert all(sp.span_text in RAW for sp in spans)

        # span_ids[i] must ground deadlines[i] — the response's ordering is a real claim about
        # lineage, so assert it positionally rather than as a set.
        by_id = {sp.id: sp for sp in spans}
        for i, span_id in enumerate(body["span_ids"]):
            assert by_id[UUID(span_id)].span_text == body["deadlines"][i]["source_span"]
            assert by_id[UUID(span_id)].ordinal == i

    asyncio.run(check())


# --------------------------------------------------------------------------- 8


def test_tasks_point_to_valid_source_evidence(clean_db, monkeypatch):
    """source -> span -> task lineage holds: each task's span belongs to that same source."""
    _patch_extract(monkeypatch)
    uid = uuid4()
    body = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()
    source_id = UUID(body["source_id"])

    async def check():
        async with user_session(uid) as s:
            rows = (await s.execute(select(schema.TaskRow).where(schema.TaskRow.user_id == uid))).scalars().all()
            assert len(rows) == 2
            for t in rows:
                assert t.source_id == source_id
                assert t.span_id is not None  # a deadline task is always grounded
                span = (
                    await s.execute(select(schema.SourceSpan).where(schema.SourceSpan.id == t.span_id))
                ).scalar_one()
                assert span.source_id == source_id  # the evidence chain closes on the same source
                # the task's title came from the deadline that span grounded
                assert t.title in ("Registration closes", "Projects due")

    asyncio.run(check())


def test_umbrella_task_without_deadline_is_honest_about_having_no_span(clean_db, monkeypatch):
    """Required items but no deadline -> one umbrella task with span_id NULL, never a fake anchor."""
    _patch_extract(
        monkeypatch,
        ExtractedIntake(
            source_kind=IntakeSourceKind.text,
            title="Summer REU application",
            deadlines=[],
            required_items=[RequiredItem(name="Transcript", kind="doc")],
        ),
    )
    uid = uuid4()
    body = client.post("/v1/intake/sync", json={"text": "Send a transcript."}, headers=_auth(uid)).json()
    assert body["span_ids"] == [] and len(body["task_ids"]) == 1

    async def check():
        async with user_session(uid) as s:
            row = (await s.execute(select(schema.TaskRow).where(schema.TaskRow.user_id == uid))).scalar_one()
        assert row.span_id is None and row.source_id == UUID(body["source_id"])

    asyncio.run(check())


# --------------------------------------------------------------------------- 9


def test_expired_content_metadata_is_set_from_the_retention_policy(clean_db, monkeypatch):
    """expires_at is stamped from retention policy, is in the future, and the sweep can find it."""
    _patch_extract(monkeypatch)
    uid = uuid4()
    before = datetime.datetime.now(datetime.timezone.utc)
    body = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()

    async def check():
        rec = await sources.get_for_user(UUID(body["source_id"]), uid)
        assert rec is not None and rec.expires_at is not None
        assert rec.raw_text == RAW  # raw content is present until the sweep erases it
        window = datetime.timedelta(days=retention.raw_retention_days())
        # stamped at write time from the policy (allow a minute of slack for test wall-clock)
        assert abs((rec.expires_at - (before + window)).total_seconds()) < 60
        assert rec.expires_at > before  # not already expired

        # and the retention sweep genuinely owns this row once the window closes
        status = await retention.retention_status(before + window + datetime.timedelta(days=1))
        assert status["pending_expired"] == 1

    asyncio.run(check())


def test_retention_sweep_erases_intake_raw_text_but_keeps_lineage(clean_db, monkeypatch):
    """End-to-end with the existing sweep: raw_text goes, source/spans/tasks lineage stays."""
    _patch_extract(monkeypatch)
    uid = uuid4()
    body = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()

    async def check():
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=retention.raw_retention_days() + 1
        )
        result = await retention.sweep_expired(future)
        assert result["erased"] == 1

        rec = await sources.get_for_user(UUID(body["source_id"]), uid)
        assert rec is not None and rec.raw_text is None  # the raw blob is gone
        assert await _count(schema.SourceSpan, uid) == 2  # grounding survives
        assert await _count(schema.TaskRow, uid) == 2  # the student's tasks survive

    asyncio.run(check())


# --------------------------------------------------------------------------- 11


def test_response_ids_are_fetchable_through_authenticated_repository_methods(clean_db, monkeypatch):
    """Every id the API returned resolves through the RLS-scoped repos — for the owner, and only them."""
    _patch_extract(monkeypatch)
    uid = uuid4()
    body = client.post("/v1/intake/sync", json={"text": RAW}, headers=_auth(uid)).json()

    async def check():
        src = await sources.get_for_user(UUID(body["source_id"]), uid)
        assert src is not None and src.kind == "text" and src.content_sha256 == intake_store.content_sha256(RAW)

        for tid in body["task_ids"]:
            t = await tasks.get_for_user(UUID(tid), uid)
            assert t is not None and t.source_id == src.id and t.span_id is not None

        listed = await tasks.list_for_user(uid)
        assert {str(t.id) for t in listed} == set(body["task_ids"])

        # spans resolve under the owner's context and are scoped to them
        async with user_session(uid) as s:
            for sid in body["span_ids"]:
                span = (
                    await s.execute(select(schema.SourceSpan).where(schema.SourceSpan.id == UUID(sid)))
                ).scalar_one()
                assert span.user_id == uid

    asyncio.run(check())
