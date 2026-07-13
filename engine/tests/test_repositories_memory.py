"""Offline tests for the in-memory repositories (same protocols as Postgres; no network)."""

import asyncio
from uuid import uuid4

import pytest

from bruce_engine.records import (
    ConcurrencyError,
    CrossTenantError,
    MissionRecord,
    NotFoundError,
    SourceRecord,
    TaskRecord,
)
from bruce_engine.repositories import (
    InMemoryMissionRepository,
    InMemorySourceRepository,
    InMemoryStore,
    InMemoryTaskRepository,
)


def test_mission_isolation_requires_matching_user():
    async def run():
        repo = InMemoryMissionRepository(InMemoryStore())
        a, b = uuid4(), uuid4()
        m = await repo.create(MissionRecord(user_id=a, goal={"topic": "x"}))
        assert await repo.get_for_user(m.id, a) is not None
        assert await repo.get_for_user(m.id, b) is None  # B can't read A's mission by id
        assert await repo.list_for_user(b) == []

    asyncio.run(run())


def test_optimistic_concurrency_and_404_on_wrong_owner():
    async def run():
        repo = InMemoryMissionRepository(InMemoryStore())
        a = uuid4()
        m = await repo.create(MissionRecord(user_id=a))
        m2 = await repo.update_phase(m.id, a, expected_version=1, phase="executing", short_status="x")
        assert m2.version == 2 and m2.phase == "executing"
        with pytest.raises(ConcurrencyError):
            await repo.update_phase(m.id, a, expected_version=1, phase="verifying", short_status="y")
        with pytest.raises(NotFoundError):  # wrong owner -> not-found, never a revealing error
            await repo.update_phase(m.id, uuid4(), expected_version=2, phase="z", short_status="z")

    asyncio.run(run())


def test_idempotent_mission_create():
    async def run():
        repo = InMemoryMissionRepository(InMemoryStore())
        a = uuid4()
        m1 = await repo.create(MissionRecord(user_id=a, idempotency_key="k1"))
        m2 = await repo.create(MissionRecord(user_id=a, idempotency_key="k1"))
        assert m1.id == m2.id
        assert len(await repo.list_for_user(a)) == 1

    asyncio.run(run())


def test_task_cross_tenant_source_blocked():
    async def run():
        store = InMemoryStore()
        sources = InMemorySourceRepository(store)
        tasks = InMemoryTaskRepository(store)
        a, b = uuid4(), uuid4()
        src_b = await sources.create(SourceRecord(user_id=b, kind="text"))
        with pytest.raises(CrossTenantError):  # A cannot attach B's source to A's task
            await tasks.create(TaskRecord(user_id=a, kind="deadline", title="x", source_id=src_b.id))
        src_a = await sources.create(SourceRecord(user_id=a, kind="text"))
        ok = await tasks.create(TaskRecord(user_id=a, kind="deadline", title="x", source_id=src_a.id))
        assert ok.id is not None and ok.source_id == src_a.id

    asyncio.run(run())


def test_task_idempotent_and_isolated():
    async def run():
        store = InMemoryStore()
        tasks = InMemoryTaskRepository(store)
        a, b = uuid4(), uuid4()
        t1 = await tasks.create(TaskRecord(user_id=a, kind="deadline", title="x", idempotency_key="tk"))
        t2 = await tasks.create(TaskRecord(user_id=a, kind="deadline", title="x", idempotency_key="tk"))
        assert t1.id == t2.id
        assert await tasks.get_for_user(t1.id, b) is None

    asyncio.run(run())
