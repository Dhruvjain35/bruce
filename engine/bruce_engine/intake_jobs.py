"""Durable intake job queue — claim/lease semantics, not fire-and-forget.

Accepted intake work is a ROW, not a coroutine. The request commits a job (status='pending') and
returns 202; a worker later CLAIMS it with a time-boxed lease, does the model work, and records the
outcome. If the worker process dies mid-job the row survives and its lease expires, so any worker
reclaims it — accepted work is never silently lost, which an in-process BackgroundTask cannot promise.

State machine (attempts is incremented on each claim, so it bounds retries):

    pending ─claim─▶ processing ─ok─▶ completed
                         │
                         ├─ retryable (attempts < max) ─▶ retryable_failed ─(lease expires)─▶ (reclaim)
                         └─ terminal  (attempts ≥ max, or a non-retryable read error) ─▶ terminal_failed
    (a crashed worker leaves status=processing with an expired lease → also reclaimable)

Two implementations behind one Protocol: PostgresJobStore (the real thing — FOR UPDATE SKIP LOCKED
claiming under a worker RLS session) and InMemoryJobStore (fast offline tests of the state machine).
The Postgres claim is the only cross-user step; everything after runs under user_session(job.user_id).
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import text as sa_text

from .db import user_session, worker_session

# --- statuses -----------------------------------------------------------------------------------
PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
RETRYABLE_FAILED = "retryable_failed"
TERMINAL_FAILED = "terminal_failed"

_CLAIMABLE = (PENDING, PROCESSING, RETRYABLE_FAILED)
DEFAULT_LEASE_SECONDS = 60
DEFAULT_RETRY_BACKOFF_SECONDS = 5


@dataclasses.dataclass
class ClaimedJob:
    """Everything the worker needs to process a job — no ORM object escapes the store."""

    id: UUID
    user_id: UUID
    source_id: UUID
    mission_id: UUID
    source_kind: str
    mime: str | None
    input_text: str | None
    input_bytes: bytes | None
    attempts: int
    max_attempts: int
    idempotency_key: str | None = None  # == the source_key (task derivation needs it)

    @property
    def can_retry(self) -> bool:
        """Whether another attempt is allowed after a retryable failure on THIS attempt."""
        return self.attempts < self.max_attempts


class JobStore(Protocol):
    async def claim(self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> ClaimedJob | None: ...
    async def mark_completed(self, job: ClaimedJob) -> None: ...
    async def mark_retryable(self, job: ClaimedJob, reason: str, backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS) -> None: ...
    async def mark_terminal(self, job: ClaimedJob, reason: str) -> None: ...


def _short(reason: str) -> str:
    """last_error stores a TYPE/short reason only — never student content (column is 200 chars)."""
    return (reason or "")[:200]


# --- Postgres -----------------------------------------------------------------------------------


class PostgresJobStore:
    """Real durable store. Claim is atomic (FOR UPDATE SKIP LOCKED) under a worker RLS session; the
    terminal/complete/retry transitions run under the job owner's session (the row is the user's own,
    so the tenant RLS clause admits it) alongside the content writes for atomicity."""

    async def claim(self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> ClaimedJob | None:
        # SKIP LOCKED lets N workers claim concurrently without ever grabbing the same row. Claimable
        # = pending, OR processing/retryable whose lease has expired (a crashed or backed-off job).
        sql = sa_text(
            """
            UPDATE intake_jobs SET
                status = 'processing',
                lease_owner = :worker,
                lease_expires_at = now() + make_interval(secs => :lease),
                attempts = attempts + 1,
                version = version + 1,
                updated_at = now()
            WHERE id = (
                SELECT id FROM intake_jobs
                WHERE (status = 'pending')
                   OR (status IN ('processing', 'retryable_failed')
                       AND lease_expires_at IS NOT NULL AND lease_expires_at < now())
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, user_id, source_id, mission_id, source_kind, mime,
                      input_text, input_bytes, attempts, max_attempts, idempotency_key
            """
        )
        async with worker_session() as s:
            row = (await s.execute(sql, {"worker": worker_id[:64], "lease": lease_seconds})).mappings().first()
        if row is None:
            return None
        return ClaimedJob(
            id=row["id"], user_id=row["user_id"], source_id=row["source_id"],
            mission_id=row["mission_id"], source_kind=row["source_kind"], mime=row["mime"],
            input_text=row["input_text"], input_bytes=row["input_bytes"],
            attempts=row["attempts"], max_attempts=row["max_attempts"], idempotency_key=row["idempotency_key"],
        )

    async def mark_completed(self, job: ClaimedJob) -> None:
        # Clear the transient input on success; the durable content is in sources/spans/tasks now.
        async with user_session(job.user_id) as s:
            await s.execute(
                sa_text(
                    "UPDATE intake_jobs SET status='completed', input_text=NULL, input_bytes=NULL, "
                    "lease_owner=NULL, lease_expires_at=NULL, version=version+1, updated_at=now() "
                    "WHERE id=:id AND user_id=:uid"
                ),
                {"id": str(job.id), "uid": str(job.user_id)},
            )

    async def mark_retryable(self, job: ClaimedJob, reason: str, backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS) -> None:
        async with user_session(job.user_id) as s:
            await s.execute(
                sa_text(
                    "UPDATE intake_jobs SET status='retryable_failed', last_error=:err, "
                    "lease_expires_at = now() + make_interval(secs => :backoff), "
                    "version=version+1, updated_at=now() WHERE id=:id AND user_id=:uid"
                ),
                {"id": str(job.id), "uid": str(job.user_id), "err": _short(reason), "backoff": backoff_seconds},
            )

    async def mark_terminal(self, job: ClaimedJob, reason: str) -> None:
        async with user_session(job.user_id) as s:
            await s.execute(
                sa_text(
                    "UPDATE intake_jobs SET status='terminal_failed', last_error=:err, "
                    "input_text=NULL, input_bytes=NULL, lease_owner=NULL, lease_expires_at=NULL, "
                    "version=version+1, updated_at=now() WHERE id=:id AND user_id=:uid"
                ),
                {"id": str(job.id), "uid": str(job.user_id), "err": _short(reason)},
            )


# --- In-memory (offline tests) ------------------------------------------------------------------


@dataclasses.dataclass
class _MemJob:
    id: UUID
    user_id: UUID
    source_id: UUID
    mission_id: UUID
    source_kind: str
    mime: str | None
    input_text: str | None
    input_bytes: bytes | None
    idempotency_key: str | None
    status: str = PENDING
    attempts: int = 0
    max_attempts: int = 3
    lease_owner: str | None = None
    lease_expires_at: datetime.datetime | None = None
    last_error: str | None = None


class InMemoryJobStore:
    """Mirrors the Postgres claim/lease state machine for fast offline tests. Time is injected so a
    test can simulate a crashed worker's expired lease without sleeping."""

    def __init__(self) -> None:
        self.jobs: dict[UUID, _MemJob] = {}

    def add(self, job: _MemJob) -> _MemJob:
        self.jobs[job.id] = job
        return job

    async def claim(self, worker_id: str, lease_seconds: int = DEFAULT_LEASE_SECONDS, *, now: datetime.datetime | None = None) -> ClaimedJob | None:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        candidates = [
            j for j in self.jobs.values()
            if j.status == PENDING
            or (j.status in (PROCESSING, RETRYABLE_FAILED) and j.lease_expires_at is not None and j.lease_expires_at < now)
        ]
        if not candidates:
            return None
        j = min(candidates, key=lambda x: (x.id.int))  # stable pick (created_at proxy)
        j.status = PROCESSING
        j.lease_owner = worker_id
        j.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        j.attempts += 1
        return ClaimedJob(
            id=j.id, user_id=j.user_id, source_id=j.source_id, mission_id=j.mission_id,
            source_kind=j.source_kind, mime=j.mime, input_text=j.input_text, input_bytes=j.input_bytes,
            attempts=j.attempts, max_attempts=j.max_attempts, idempotency_key=j.idempotency_key,
        )

    async def mark_completed(self, job: ClaimedJob) -> None:
        j = self.jobs[job.id]
        j.status, j.input_text, j.input_bytes, j.lease_expires_at = COMPLETED, None, None, None

    async def mark_retryable(self, job: ClaimedJob, reason: str, backoff_seconds: int = DEFAULT_RETRY_BACKOFF_SECONDS, *, now: datetime.datetime | None = None) -> None:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        j = self.jobs[job.id]
        j.status, j.last_error = RETRYABLE_FAILED, _short(reason)
        j.lease_expires_at = now + datetime.timedelta(seconds=backoff_seconds)

    async def mark_terminal(self, job: ClaimedJob, reason: str) -> None:
        j = self.jobs[job.id]
        j.status, j.last_error, j.input_text, j.input_bytes, j.lease_expires_at = (
            TERMINAL_FAILED, _short(reason), None, None, None,
        )
