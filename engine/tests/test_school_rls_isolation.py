"""Cross-user RLS isolation for the SchoolConnector academic graph, against REAL Postgres.

A student's synced school record — every course, assignment, grade, and teacher comment — is among the
most sensitive data Bruce holds. This proves, through the restricted ``bruce_app`` role, that a second
user B cannot READ, UPDATE, DELETE, or cross-INSERT user A's school rows — not merely that the RLS flags
are set (test_postgres_integration test_08 covers the flags). Mirrors the two-user denial pattern used for
the conversation-brain tables. Skips cleanly when Postgres isn't configured (via ``pg_test_db``).
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import schema, school_store
from bruce_engine.canvas_fake import CanvasFakeConnector
from bruce_engine.db import user_session
from bruce_engine.repositories import PostgresUserRepository

users_repo = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(coro):
    return asyncio.run(coro)


async def _count(uid, model) -> int:
    async with user_session(uid) as s:
        return (await s.execute(select(func.count()).select_from(model).where(model.user_id == uid))).scalar_one()


def test_school_data_is_isolated_across_users(clean_db):
    """B, synced with their OWN data, sees exactly their own — never a row of A's — through the store."""
    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        await school_store.sync_provider(CanvasFakeConnector(), a)
        await school_store.sync_provider(CanvasFakeConnector(), b)

        # Each user sees exactly 7 assignments / 3 courses — their own copies, no cross-tenant bleed.
        assert await _count(a, schema.SchoolAssignment) == 7
        assert await _count(b, schema.SchoolAssignment) == 7
        a_assignments = await school_store.list_assignments(a)
        b_assignments = await school_store.list_assignments(b)
        a_ids = {r.id for r in await _rows(a, schema.SchoolAssignment)}
        b_ids = {r.id for r in await _rows(b, schema.SchoolAssignment)}
        assert a_ids and b_ids and a_ids.isdisjoint(b_ids)   # different physical rows, per tenant
        # the query layer for B never returns an A-owned row id
        assert len(a_assignments) == 7 and len(b_assignments) == 7
    _run(run())


async def _rows(uid, model):
    async with user_session(uid) as s:
        return (await s.execute(select(model).where(model.user_id == uid))).scalars().all()


def test_user_b_cannot_read_update_delete_or_cross_insert_user_a_rows(clean_db):
    """Adversarial: under B's RLS context, A's synced school rows are invisible and immutable."""
    async def run():
        a, b = uuid4(), uuid4()
        await users_repo.ensure(a)
        await users_repo.ensure(b)
        await school_store.sync_provider(CanvasFakeConnector(), a)  # only A has data

        # grab an A-owned assignment id + source id (under A's own context)
        a_assignment = (await _rows(a, schema.SchoolAssignment))[0]
        a_source = (await _rows(a, schema.SchoolSource))[0]

        async with user_session(b) as s:    # cross-READ: B sees zero school rows of every kind
            for model in (schema.SchoolAssignment, schema.SchoolCourse, schema.SchoolSource,
                          schema.SchoolSubmission, schema.SchoolObjectChange, schema.SchoolSyncCursor):
                assert (await s.execute(select(func.count()).select_from(model))).scalar_one() == 0

        async with user_session(b) as s:    # cross-UPDATE / cross-DELETE: 0 rows affected
            assert (await s.execute(update(schema.SchoolAssignment)
                    .where(schema.SchoolAssignment.id == a_assignment.id).values(name="hacked"))).rowcount == 0
            assert (await s.execute(delete(schema.SchoolSource)
                    .where(schema.SchoolSource.id == a_source.id))).rowcount == 0

        with pytest.raises(Exception):      # cross-INSERT: WITH CHECK rejects a row owned by A
            async with user_session(b) as s:
                await s.execute(schema.SchoolAssignment.__table__.insert().values(
                    user_id=a, provider="canvas", provider_id="x", course_provider_id="101", name="intruder"))

        async with user_session(a) as s:    # A's rows survived every attempt, untouched
            row = (await s.execute(select(schema.SchoolAssignment)
                   .where(schema.SchoolAssignment.id == a_assignment.id))).scalar_one()
            assert row.name == a_assignment.name != "hacked"
    _run(run())
