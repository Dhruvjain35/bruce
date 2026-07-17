"""Async DB engine/session + per-request RLS user context.

Runtime uses the non-superuser app role (BRUCE_APP_DATABASE_URL) so Postgres RLS actually
enforces. Each request opens a session, sets `app.user_id` (the authenticated sub) transaction-
locally, and every RLS policy compares current_setting('app.user_id') to the row's user_id.
Migrations use the owner URL (BRUCE_DATABASE_URL) via Alembic — schema is NEVER created at startup.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_engine = None
_sessionmaker: async_sessionmaker | None = None


def _app_url() -> str:
    url = os.environ.get("BRUCE_APP_DATABASE_URL")
    if not url:
        raise RuntimeError("BRUCE_APP_DATABASE_URL not set — load engine/.env at the entrypoint.")
    return url


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(_app_url(), pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


@asynccontextmanager
async def user_session(user_id: UUID) -> AsyncIterator[AsyncSession]:
    """Session bound to a user for RLS: sets app.user_id, commits on success, rolls back on error."""
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        # transaction-local: RLS policies read current_setting('app.user_id', true)
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)}
        )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """Session for the intake worker's QUEUE operations ONLY (claim/lease/status transitions).

    Sets the transaction-local ``app.worker='on'`` flag, which the intake_jobs RLS policy admits so
    the worker can claim jobs across users. This is set ONLY here, in server worker code — never in a
    request handler, never from user input — so it can never widen a user's visibility. It grants
    access to the intake_jobs table alone; all CONTENT writes (sources/spans/tasks) still go through
    user_session(job.user_id) and stay fully tenant-scoped.
    """
    get_engine()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        await session.execute(text("SELECT set_config('app.worker', 'on', true)"))
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
