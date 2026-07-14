"""REAL-Postgres tests for source retention enforcement (bruce_engine.retention).

Every test depends on ``clean_db`` (truncates all user tables around each test) and exercises the
actual RLS-enforced app role. Rows are seeded with a privileged owner connection (superuser,
bypasses RLS) so we can set fields the app repos don't expose (expires_at, raw_text directly),
then the retention code is asserted through its real per-user context.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy.engine import make_url

from bruce_engine import retention
from bruce_engine.repositories import PostgresUserRepository

SECRET_RAW = "SECRET essay draft + parent phone 555-0100 — must never survive retention"


def _run(main) -> None:
    """Run one async test body, then dispose the app engine INSIDE this same event loop.

    Each test uses its own asyncio.run() (== its own loop). The app engine caches a connection
    pool; if pooled connections outlive their loop, teardown raises 'Event loop is closed'. So we
    dispose + reset the cached engine at the end of every test, in-loop.
    """
    async def wrapper():
        try:
            await main()
        finally:
            import bruce_engine.db as db

            if db._engine is not None:
                await db._engine.dispose()
                db._engine = None
                db._sessionmaker = None

    asyncio.run(wrapper())


async def _oc() -> asyncpg.Connection:
    url = make_url(os.environ["BRUCE_DATABASE_URL"])
    return await asyncpg.connect(
        host=url.host, port=url.port or 5432, user=url.username,
        password=url.password, database=url.database,
    )


async def _mk_user(conn: asyncpg.Connection, user_id: UUID) -> None:
    await conn.execute("INSERT INTO users (id, auth_provider) VALUES ($1, 'supabase')", user_id)


async def _mk_source(
    conn: asyncpg.Connection, user_id: UUID, *, raw_text: str | None,
    expires_at: datetime.datetime | None, kind: str = "text",
) -> UUID:
    return await conn.fetchval(
        "INSERT INTO sources (user_id, kind, raw_text, expires_at) VALUES ($1,$2,$3,$4) RETURNING id",
        user_id, kind, raw_text, expires_at,
    )


async def _mk_span(conn: asyncpg.Connection, user_id: UUID, source_id: UUID) -> UUID:
    return await conn.fetchval(
        "INSERT INTO source_spans (user_id, source_id, span_text) VALUES ($1,$2,$3) RETURNING id",
        user_id, source_id, "Due May 1 (grounded span)",
    )


async def _mk_task(conn: asyncpg.Connection, user_id: UUID, source_id: UUID | None) -> UUID:
    return await conn.fetchval(
        "INSERT INTO tasks (user_id, kind, title, source_id) VALUES ($1,$2,$3,$4) RETURNING id",
        user_id, "deadline", "Submit application", source_id,
    )


def _past() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)


def _future() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def test_not_yet_expired_content_remains(clean_db):
    async def run():
        conn = await _oc()
        try:
            u = uuid4()
            await _mk_user(conn, u)
            sid = await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_future())
            result = await retention.sweep_expired(_now())
            assert result["erased"] == 0
            raw = await conn.fetchval("SELECT raw_text FROM sources WHERE id = $1", sid)
            assert raw == SECRET_RAW  # future window: untouched
        finally:
            await conn.close()

    _run(run)


def test_expired_content_erased_source_and_task_remain(clean_db):
    async def run():
        conn = await _oc()
        try:
            u = uuid4()
            await _mk_user(conn, u)
            sid = await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_past())
            tid = await _mk_task(conn, u, sid)

            result = await retention.sweep_expired(_now())
            assert result["erased"] == 1 and result["scanned"] == 1

            row = await conn.fetchrow("SELECT id, raw_text FROM sources WHERE id = $1", sid)
            assert row is not None  # source row PRESERVED
            assert row["raw_text"] is None  # raw content ERASED

            task_src = await conn.fetchval("SELECT source_id FROM tasks WHERE id = $1", tid)
            assert task_src == sid  # linked task remains, still points at the (now content-free) source
        finally:
            await conn.close()

    _run(run)


def test_repeated_sweeps_are_idempotent(clean_db):
    async def run():
        conn = await _oc()
        try:
            u = uuid4()
            await _mk_user(conn, u)
            await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_past())

            first = await retention.sweep_expired(_now())
            assert first["erased"] == 1

            second = await retention.sweep_expired(_now())  # harmless re-run
            assert second["erased"] == 0 and second["scanned"] == 0

            n_audit = await conn.fetchval(
                "SELECT count(*) FROM audit_events WHERE event_type = 'source_retention'"
            )
            assert n_audit == 1  # no duplicate audit event from the second sweep
        finally:
            await conn.close()

    _run(run)


def test_retention_audit_event_has_no_private_content(clean_db):
    async def run():
        conn = await _oc()
        try:
            u = uuid4()
            await _mk_user(conn, u)
            sid = await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_past())

            await retention.sweep_expired(_now())

            ev = await conn.fetchrow(
                "SELECT event_type, detail FROM audit_events WHERE user_id = $1", u
            )
            assert ev is not None
            assert ev["event_type"] == "source_retention"
            detail_text = ev["detail"] if isinstance(ev["detail"], str) else json.dumps(ev["detail"])
            detail = json.loads(detail_text)
            assert detail == {"source_id": str(sid), "action": "raw_erased"}
            # the private content must appear NOWHERE in the audit event
            assert SECRET_RAW not in detail_text
            assert "raw_text" not in detail_text
            assert "555-0100" not in detail_text
        finally:
            await conn.close()

    _run(run)


def test_account_deletion_supersedes_retention(clean_db):
    async def run():
        conn = await _oc()
        try:
            u = uuid4()
            await _mk_user(conn, u)
            # NOT expired (future window) + raw_text present: retention alone would keep it.
            await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_future())

            await PostgresUserRepository().delete(u)  # account deletion path (cascade)

            n = await conn.fetchval("SELECT count(*) FROM sources WHERE user_id = $1", u)
            assert n == 0  # source gone regardless of (future) expiry — deletion wins
        finally:
            await conn.close()

    _run(run)


def test_immediate_delete_source_removes_spans_keeps_tasks(clean_db):
    async def run():
        conn = await _oc()
        try:
            u = uuid4()
            await _mk_user(conn, u)
            sid = await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_future())
            span_id = await _mk_span(conn, u, sid)
            tid = await _mk_task(conn, u, sid)

            deleted = await retention.delete_source(sid, u)
            assert deleted is True

            assert await conn.fetchval("SELECT count(*) FROM sources WHERE id = $1", sid) == 0
            assert await conn.fetchval(
                "SELECT count(*) FROM source_spans WHERE id = $1", span_id
            ) == 0  # spans cascade-deleted

            task_row = await conn.fetchrow("SELECT id, source_id FROM tasks WHERE id = $1", tid)
            assert task_row is not None  # task PRESERVED
            assert task_row["source_id"] is None  # FK ON DELETE SET NULL
        finally:
            await conn.close()

    _run(run)


def test_delete_source_is_scoped_to_owner(clean_db):
    async def run():
        conn = await _oc()
        try:
            owner, other = uuid4(), uuid4()
            await _mk_user(conn, owner)
            await _mk_user(conn, other)
            sid = await _mk_source(conn, owner, raw_text=SECRET_RAW, expires_at=_future())

            # another user cannot delete it (RLS + explicit owner filter) -> no-op
            assert await retention.delete_source(sid, other) is False
            assert await conn.fetchval("SELECT count(*) FROM sources WHERE id = $1", sid) == 1
        finally:
            await conn.close()

    _run(run)


def test_retention_status_reports_pending_and_erased(clean_db):
    async def run():
        conn = await _oc()
        try:
            u = uuid4()
            await _mk_user(conn, u)
            await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_past())
            await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_past())
            await _mk_source(conn, u, raw_text=SECRET_RAW, expires_at=_future())  # not pending

            before = await retention.retention_status(_now())
            assert before["pending_expired"] == 2
            assert before["erased"] == 0

            await retention.sweep_expired(_now())

            after = await retention.retention_status(_now())
            assert after["pending_expired"] == 0
            assert after["erased"] == 2  # two erasures recorded; future source untouched
        finally:
            await conn.close()

    _run(run)
