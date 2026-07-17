"""Offline tests for the async intake worker + durable job state machine (no Postgres).

Covers the parts that don't need a real DB: the claim/lease state machine (InMemoryJobStore mirrors
the Postgres semantics) and the worker's failure taxonomy — the place the "no false completion"
guarantee lives on the async path. The concurrency/RLS/idempotency behaviours that genuinely need
Postgres are in test_async_intake_pg.py (CI-gated).
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest

from bruce_engine import intake_store, worker
from bruce_engine.intake_jobs import (
    COMPLETED,
    PROCESSING,
    RETRYABLE_FAILED,
    TERMINAL_FAILED,
    InMemoryJobStore,
    _MemJob,
)
from bruce_engine.models import ExtractedIntake, IntakeSourceKind, MissionPhase


def _job(**kw):
    base = dict(
        id=uuid4(), user_id=uuid4(), source_id=uuid4(), mission_id=uuid4(),
        source_kind="text", mime=None, input_text="Applications due May 1, 2026.",
        input_bytes=None, idempotency_key="intake:k", max_attempts=3,
    )
    base.update(kw)
    return _MemJob(**base)


def _now():
    return datetime.datetime(2026, 7, 17, 12, 0, tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------- claim / lease


def test_claim_marks_processing_and_increments_attempts():
    store = InMemoryJobStore()
    store.add(_job())
    claimed = asyncio.run(store.claim("w1", 60, now=_now()))
    assert claimed is not None and claimed.attempts == 1
    assert store.jobs[claimed.id].status == PROCESSING


def test_second_worker_finds_nothing_claimable_no_double_execution():
    """SKIP LOCKED analogue: once claimed (lease live), a second worker gets None, never the same job."""
    store = InMemoryJobStore()
    store.add(_job())
    assert asyncio.run(store.claim("w1", 60, now=_now())) is not None
    assert asyncio.run(store.claim("w2", 60, now=_now())) is None  # lease still live


def test_crashed_worker_lease_expires_and_job_is_reclaimed():
    store = InMemoryJobStore()
    store.add(_job())
    first = asyncio.run(store.claim("w1", 60, now=_now()))
    later = _now() + datetime.timedelta(seconds=61)  # w1 "crashed"; its lease has expired
    second = asyncio.run(store.claim("w2", 60, now=later))
    assert second is not None and second.id == first.id and second.attempts == 2


def test_retryable_is_reclaimed_only_after_backoff():
    store = InMemoryJobStore()
    store.add(_job())
    job = asyncio.run(store.claim("w1", 60, now=_now()))
    asyncio.run(store.mark_retryable(job, "ProviderUnavailable", backoff_seconds=5, now=_now()))
    assert store.jobs[job.id].status == RETRYABLE_FAILED
    assert asyncio.run(store.claim("w1", 60, now=_now())) is None  # within backoff
    after = _now() + datetime.timedelta(seconds=6)
    assert asyncio.run(store.claim("w1", 60, now=after)) is not None  # backoff elapsed


def test_terminal_is_never_reclaimed():
    store = InMemoryJobStore()
    store.add(_job())
    job = asyncio.run(store.claim("w1", 60, now=_now()))
    asyncio.run(store.mark_terminal(job, "UnsupportedSourceType"))
    assert store.jobs[job.id].status == TERMINAL_FAILED
    later = _now() + datetime.timedelta(hours=1)
    assert asyncio.run(store.claim("w1", 60, now=later)) is None


# --------------------------------------------------------------------------- worker taxonomy


@pytest.fixture
def _capture(monkeypatch):
    """Stub the DB-backed intake_store calls the worker makes; record what it did."""
    calls = {"completed": False, "phases": []}

    async def _complete(**kw):
        calls["completed"] = True

    async def _advance(**kw):
        calls["phases"].append(("advance", kw["phase"]))

    async def _fail(**kw):
        calls["phases"].append(("fail", kw["phase"]))

    monkeypatch.setattr(intake_store, "complete_intake_extraction", _complete)
    monkeypatch.setattr(intake_store, "advance_intake_phase", _advance)
    monkeypatch.setattr(intake_store, "fail_intake_mission", _fail)
    return calls


def _fake_extract(result=None, exc=None):
    async def _f(job):
        if exc is not None:
            raise exc
        intake = result or ExtractedIntake(source_kind=IntakeSourceKind(job.source_kind))
        return intake, object()
    return _f


def test_process_one_success_completes_job_and_reaches_awaiting_approval(monkeypatch, _capture):
    store = InMemoryJobStore()
    store.add(_job())
    monkeypatch.setattr(worker, "_extract_for_job", _fake_extract())
    handled = asyncio.run(worker.process_one(store, worker_id="w1"))
    assert handled is True
    assert _capture["completed"] is True
    assert ("advance", MissionPhase.extracting) in _capture["phases"]


def test_read_failure_is_terminal_and_fails_mission_no_false_completion(monkeypatch, _capture):
    from bruce_engine.extraction import UnsupportedSourceType
    store = InMemoryJobStore()
    store.add(_job())
    monkeypatch.setattr(worker, "_extract_for_job", _fake_extract(exc=UnsupportedSourceType("bad type")))
    asyncio.run(worker.process_one(store, worker_id="w1"))
    job_id = next(iter(store.jobs))
    assert store.jobs[job_id].status == TERMINAL_FAILED
    assert ("fail", MissionPhase.failed) in _capture["phases"]  # NOT awaiting_approval / not completed
    assert _capture["completed"] is False


def test_provider_outage_is_retryable_and_blocks_mission_while_attempts_remain(monkeypatch, _capture):
    from bruce_engine.provider_status import ProviderUnavailable
    store = InMemoryJobStore()
    store.add(_job(max_attempts=3))
    monkeypatch.setattr(worker, "_extract_for_job",
                        _fake_extract(exc=ProviderUnavailable(provider="openai", model="gpt-5.4-mini", reason="down")))
    asyncio.run(worker.process_one(store, worker_id="w1"))  # attempt 1 of 3
    job_id = next(iter(store.jobs))
    assert store.jobs[job_id].status == RETRYABLE_FAILED
    assert ("fail", MissionPhase.blocked) in _capture["phases"]


def test_provider_outage_on_last_attempt_is_terminal(monkeypatch, _capture):
    from bruce_engine.provider_status import ProviderUnavailable
    store = InMemoryJobStore()
    j = _job(max_attempts=1)
    store.add(j)
    monkeypatch.setattr(worker, "_extract_for_job",
                        _fake_extract(exc=ProviderUnavailable(provider="openai", model="gpt-5.4-mini", reason="down")))
    asyncio.run(worker.process_one(store, worker_id="w1"))  # attempt 1 of 1 -> no retry left
    assert store.jobs[j.id].status == TERMINAL_FAILED
    assert ("fail", MissionPhase.failed) in _capture["phases"]


def test_idle_queue_returns_false():
    assert asyncio.run(worker.process_one(InMemoryJobStore(), worker_id="w1")) is False


def test_dispatch_routes_by_source_kind(monkeypatch, _capture):
    seen = {}

    async def _img(data, mime="image/png", **kw):
        seen["called"] = "image"
        return ExtractedIntake(source_kind=IntakeSourceKind.image), object()

    monkeypatch.setattr(worker, "extract_from_image_traced", _img)
    store = InMemoryJobStore()
    store.add(_job(source_kind="image", mime="image/png", input_text=None, input_bytes=b"\x89PNG"))
    asyncio.run(worker.process_one(store, worker_id="w1"))
    assert seen["called"] == "image"
