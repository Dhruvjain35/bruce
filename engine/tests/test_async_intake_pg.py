"""Async intake against REAL Postgres (durable job + lease + two-phase persist), under RLS.

The offline suite (test_async_intake_worker) proves the state machine; this proves the behaviours
that genuinely need Postgres: the 202 returns a durable mission BEFORE any model runs, the worker
persists results and advances the mission, retries are idempotent, no false completion, and one user
can never see another's mission. Skips cleanly when Postgres isn't configured (via pg_test_db).
"""

from __future__ import annotations

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
from bruce_engine import intake_store, schema, worker
from bruce_engine.db import user_session
from bruce_engine.intake_jobs import PostgresJobStore
from bruce_engine.models import ExtractedDeadline, ExtractedIntake, IntakeSourceKind

client = TestClient(api.app)
RAW = "Northgate Science Fair. Registration closes Feb 28, 2026. Projects due Mar 14, 2026."


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    monkeypatch.setenv("BRUCE_JWT_SECRET", "test-secret-that-is-at-least-32-bytes-long!!")
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    yield
    db._engine = None
    db._sessionmaker = None


def _auth(uid: UUID):
    tok = jwt.encode({"sub": str(uid), "exp": int(time.time()) + 3600},
                     os.environ["BRUCE_JWT_SECRET"], algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _grounded_intake(kind=IntakeSourceKind.text):
    return ExtractedIntake(
        source_kind=kind,
        deadlines=[
            ExtractedDeadline(label="Registration closes", date="2026-02-28", source_span="Registration closes Feb 28, 2026", confidence=0.9),
            ExtractedDeadline(label="Projects due", date="2026-03-14", source_span="Projects due Mar 14, 2026", confidence=0.9),
        ],
        raw_source_excerpt=RAW,
    )


def _fake_worker_extract(intake=None, exc=None, monkeypatch=None):
    async def _f(job):
        if exc is not None:
            raise exc
        return (intake or _grounded_intake(IntakeSourceKind(job.source_kind))), object()
    monkeypatch.setattr(worker, "_extract_for_job", _f)


async def _run_worker_until_idle(uid_seed: str = "w"):
    store = PostgresJobStore()
    for _ in range(10):
        if not await worker.process_one(store, worker_id=f"{uid_seed}-1"):
            break


# --------------------------------------------------------------------------- 202 + durability


def test_post_returns_202_with_durable_mission_before_any_model_runs(clean_db):
    uid = uuid4()
    r = client.post("/v1/intake", json={"text": RAW, "source_kind": "text"}, headers=_auth(uid))
    assert r.status_code == 202
    body = r.json()
    assert body["state"] == "understanding" and body["mission_id"] and body["source_id"]
    assert "Understanding" in body["display_status"]
    # Immediately fetchable, and it is NOT a fake completion: no extraction has run yet.
    m = client.get(f"/v1/missions/{body['mission_id']}", headers=_auth(uid)).json()
    assert m["phase"] == "understanding" and m["extracted"] is None


def test_worker_persists_extraction_and_advances_to_awaiting_approval(clean_db, monkeypatch):
    uid = uuid4()
    _fake_worker_extract(monkeypatch=monkeypatch)
    body = client.post("/v1/intake", json={"text": RAW}, headers=_auth(uid)).json()
    import asyncio
    asyncio.run(_run_worker_until_idle())
    m = client.get(f"/v1/missions/{body['mission_id']}", headers=_auth(uid)).json()
    assert m["phase"] == "awaiting_approval"
    assert m["extracted"] is not None and len(m["extracted"]["deadlines"]) == 2
    assert "approve" in m["available_actions"]


def test_state_transition_order_is_understanding_extracting_awaiting_approval(clean_db, monkeypatch):
    uid = uuid4()
    _fake_worker_extract(monkeypatch=monkeypatch)
    body = client.post("/v1/intake", json={"text": RAW}, headers=_auth(uid)).json()
    import asyncio
    asyncio.run(_run_worker_until_idle())
    events = client.get(f"/v1/missions/{body['mission_id']}/events", headers=_auth(uid)).json()
    assert [e["phase"] for e in events] == ["understanding", "extracting", "awaiting_approval"]


def test_duplicate_post_is_idempotent_one_mission_one_source_one_job(clean_db):
    uid = uuid4()
    a = client.post("/v1/intake", json={"text": RAW}, headers=_auth(uid)).json()
    b = client.post("/v1/intake", json={"text": RAW}, headers=_auth(uid)).json()
    assert a["mission_id"] == b["mission_id"] and a["source_id"] == b["source_id"]

    async def _counts():
        async with user_session(uid) as s:
            src = (await s.execute(select(func.count()).select_from(schema.Source).where(schema.Source.user_id == uid))).scalar_one()
            mis = (await s.execute(select(func.count()).select_from(schema.Mission).where(schema.Mission.user_id == uid))).scalar_one()
            job = (await s.execute(select(func.count()).select_from(schema.IntakeJob).where(schema.IntakeJob.user_id == uid))).scalar_one()
        return src, mis, job

    import asyncio
    assert asyncio.run(_counts()) == (1, 1, 1)


def test_read_failure_fails_the_mission_never_a_false_completion(clean_db, monkeypatch):
    from bruce_engine.extraction import UnsupportedSourceType
    uid = uuid4()
    _fake_worker_extract(exc=UnsupportedSourceType("bad type"), monkeypatch=monkeypatch)
    body = client.post("/v1/intake", json={"text": RAW}, headers=_auth(uid)).json()
    import asyncio
    asyncio.run(_run_worker_until_idle())
    m = client.get(f"/v1/missions/{body['mission_id']}", headers=_auth(uid)).json()
    assert m["phase"] == "failed" and m["extracted"] is None  # NOT awaiting_approval with empty tasks
    assert m["blocking_reason"]

    async def _task_count():
        async with user_session(uid) as s:
            return (await s.execute(select(func.count()).select_from(schema.TaskRow).where(schema.TaskRow.user_id == uid))).scalar_one()
    assert asyncio.run(_task_count()) == 0  # no tasks from a read that failed


def test_provider_outage_blocks_then_recovers_on_reclaim(clean_db, monkeypatch):
    from bruce_engine.provider_status import ProviderUnavailable
    uid = uuid4()
    # First attempt: outage -> mission blocked, job retryable.
    _fake_worker_extract(exc=ProviderUnavailable(provider="openai", model="gpt-5.4-mini", reason="down"), monkeypatch=monkeypatch)
    body = client.post("/v1/intake", json={"text": RAW}, headers=_auth(uid)).json()
    import asyncio
    asyncio.run(_run_worker_until_idle())
    m = client.get(f"/v1/missions/{body['mission_id']}", headers=_auth(uid)).json()
    assert m["phase"] == "blocked"

    # Expire the backoff lease, then a healthy worker reclaims and completes.
    async def _expire_and_recover():
        async with user_session(uid) as s:
            from sqlalchemy import text as sa_text
            await s.execute(sa_text("UPDATE intake_jobs SET lease_expires_at = now() - interval '1 hour' WHERE user_id=:u"), {"u": str(uid)})
        _fake_worker_extract(monkeypatch=monkeypatch)  # provider healthy now
        await _run_worker_until_idle()
    asyncio.run(_expire_and_recover())
    m2 = client.get(f"/v1/missions/{body['mission_id']}", headers=_auth(uid)).json()
    assert m2["phase"] == "awaiting_approval" and m2["extracted"] is not None


def test_one_user_cannot_see_another_users_mission(clean_db):
    a, b = uuid4(), uuid4()
    body = client.post("/v1/intake", json={"text": RAW}, headers=_auth(a)).json()
    # B tries to read A's mission and its events -> 404 (never a revealing 403).
    assert client.get(f"/v1/missions/{body['mission_id']}", headers=_auth(b)).status_code == 404
    assert client.get(f"/v1/missions/{body['mission_id']}/events", headers=_auth(b)).status_code == 404
