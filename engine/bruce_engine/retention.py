"""Source retention enforcement — erase temporary raw content on schedule, per-user.

Raw uploaded/pasted content (``sources.raw_text``) is TEMPORARY: it exists only long enough to
extract grounded spans/tasks, then it must go. This module enforces that lifecycle. Two paths:

  * ``sweep_expired`` — the scheduled sweep. It runs across ALL users, so it enumerates expired
    sources with a PRIVILEGED owner connection (the owner is a superuser and bypasses RLS) to get
    ``(source_id, user_id)`` pairs in bounded batches. It then does the actual mutation for EACH
    source INSIDE that user's RLS context (``bruce_engine.db.user_session(user_id)``), never with
    the privileged connection — so the erase + audit obey the same tenant isolation as everything
    else and can never touch another user's row.
  * ``delete_source`` — immediate, user-requested deletion, always in the acting user's context.

What "erase" means here: set ``raw_text = NULL`` while PRESERVING the source row itself, its
``source_spans`` (the grounding anchors), and any ``tasks`` derived from it. We keep the durable,
content-free lineage (source id, kind, content_sha256, spans, tasks) and drop only the raw blob.
Each erasure writes a CONTENT-FREE ``audit_events`` row (``event_type='source_retention'``,
``detail={source_id, action:'raw_erased'}``) — it NEVER contains ``raw_text`` or any private
content, only the id and the action taken.

Idempotent + retry-safe: the erase targets ``raw_text IS NOT NULL``, so an already-erased source
is skipped (no second audit event, no error), and a crashed/re-run sweep converges.

DOCUMENTED GAPS (intentional, must be revisited before they exist):
  (i)  NO object storage yet. Raw inputs live only in ``sources.raw_text`` (a DB column); there is
       no S3/GCS bucket or blob store holding uploaded originals. So "delete the raw uploaded
       object" is N/A today — the ONLY raw artifact is ``raw_text``, and nulling it is the complete
       erasure. When object storage is introduced, this sweep MUST be extended to also delete the
       stored object (and that deletion must be verified, not fire-and-forget).
  (ii) NO legal / administrative hold mechanism. There is deliberately no way to exempt a source
       from retention (e.g. for litigation hold or an abuse investigation). Its absence is
       INTENTIONAL: a hold that silently preserves user content is a privacy hazard and must be
       explicitly designed, access-controlled, and documented BEFORE one is added. Until then,
       retention always wins and account deletion always supersedes retention.

Never log deleted content: nothing here prints, returns, or persists ``raw_text``.
"""

from __future__ import annotations

import datetime
import os
from uuid import UUID

import asyncpg
from sqlalchemy import delete, update
from sqlalchemy.engine import make_url

from . import schema
from .db import user_session

_RETENTION_EVENT = "source_retention"

# THE raw-content retention policy. This module is its only home: writers MUST stamp expires_at via
# expires_at_for() rather than inventing a window, so the sweep below and the writers can never
# disagree. 30 days is a deliberate default, not a derived one — long enough that a student can
# still open the original email/PDF Bruce acted on, short enough that raw content isn't kept
# indefinitely for a product that only needs it long enough to extract grounded spans/tasks.
# Override per-deployment with BRUCE_RAW_RETENTION_DAYS. Shortening it is safe and retroactive:
# the sweep selects on expires_at < now, so already-written rows expire earlier automatically.
DEFAULT_RAW_RETENTION_DAYS = 30


def raw_retention_days() -> int:
    """Active retention window in days (env-overridable). Read per call so tests can vary it."""
    raw = os.environ.get("BRUCE_RAW_RETENTION_DAYS")
    if not raw:
        return DEFAULT_RAW_RETENTION_DAYS
    try:
        days = int(raw)
    except ValueError:
        return DEFAULT_RAW_RETENTION_DAYS
    return days if days >= 0 else DEFAULT_RAW_RETENTION_DAYS


def expires_at_for(now: datetime.datetime) -> datetime.datetime:
    """When raw_text written at ``now`` must be erased. The only way writers should set expires_at."""
    return now + datetime.timedelta(days=raw_retention_days())


async def _owner_conn() -> asyncpg.Connection:
    """Privileged (superuser/owner) asyncpg connection — bypasses RLS for cross-user enumeration.

    Built from BRUCE_DATABASE_URL via make_url so it always tracks the active (test or prod) DB.
    """
    import os

    raw = os.environ.get("BRUCE_DATABASE_URL")
    if not raw:
        raise RuntimeError("BRUCE_DATABASE_URL not set — owner connection required for retention sweeps.")
    url = make_url(raw)
    return await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database,
    )


async def _erase_one(source_id: UUID, user_id: UUID) -> bool:
    """Erase raw_text for one source INSIDE its owner's RLS context. Returns True iff it erased now.

    Idempotent: the WHERE clause requires raw_text IS NOT NULL, so an already-erased source (or one
    a concurrent sweep just handled) matches nothing, returns False, and writes no audit event.
    """
    async with user_session(user_id) as s:
        res = await s.execute(
            update(schema.Source)
            .where(
                schema.Source.id == source_id,
                schema.Source.user_id == user_id,  # redundant with RLS, explicit for defence-in-depth
                schema.Source.raw_text.isnot(None),
            )
            .values(raw_text=None)
        )
        if res.rowcount != 1:
            return False
        # content-free audit: id + action only, NEVER raw_text or any private content.
        s.add(
            schema.AuditEvent(
                user_id=user_id,
                event_type=_RETENTION_EVENT,
                detail={"source_id": str(source_id), "action": "raw_erased"},
            )
        )
        return True


async def sweep_expired(now: datetime.datetime, batch_size: int = 100) -> dict[str, int]:
    """Erase raw_text for every source whose retention window has closed, across ALL users.

    Enumeration uses a privileged owner connection (bypasses RLS) to fetch (source_id, user_id)
    pairs in bounded batches of ``batch_size``; the erase itself runs per-source inside that user's
    RLS context. Erased rows drop out of the eligibility query (raw_text becomes NULL), so the loop
    converges without offsets. Returns {'scanned': n, 'erased': n} — counts only, no content.
    """
    scanned = 0
    erased = 0
    conn = await _owner_conn()
    try:
        while True:
            rows = await conn.fetch(
                "SELECT id, user_id FROM sources "
                "WHERE expires_at < $1 AND raw_text IS NOT NULL "
                "ORDER BY expires_at LIMIT $2",
                now,
                batch_size,
            )
            if not rows:
                break
            batch_erased = 0
            for row in rows:
                scanned += 1
                if await _erase_one(row["id"], row["user_id"]):
                    batch_erased += 1
            erased += batch_erased
            # Defensive: if a non-empty batch made zero progress, stop rather than spin forever.
            if batch_erased == 0:
                break
    finally:
        await conn.close()
    return {"scanned": scanned, "erased": erased}


async def delete_source(source_id: UUID, user_id: UUID) -> bool:
    """Immediate user-requested deletion of one source, in the acting user's RLS context.

    Deleting the source cascades to its source_spans (FK ON DELETE CASCADE); derived tasks are kept
    with tasks.source_id set to NULL (FK ON DELETE SET NULL). Returns True iff a row was deleted.
    RLS independently guarantees a user can only delete their own source.
    """
    async with user_session(user_id) as s:
        res = await s.execute(
            delete(schema.Source).where(
                schema.Source.id == source_id,
                schema.Source.user_id == user_id,
            )
        )
        return res.rowcount == 1


async def retention_status(now: datetime.datetime) -> dict[str, int]:
    """Privileged read for internal verification: how much raw content is pending vs already erased.

    pending_expired: sources past their window that still hold raw_text (a sweep would erase these).
    erased:          count of retention erasures performed (source_retention audit events).
    """
    conn = await _owner_conn()
    try:
        pending = await conn.fetchval(
            "SELECT count(*) FROM sources WHERE expires_at < $1 AND raw_text IS NOT NULL",
            now,
        )
        erased = await conn.fetchval(
            "SELECT count(*) FROM audit_events WHERE event_type = $1",
            _RETENTION_EVENT,
        )
    finally:
        await conn.close()
    return {"pending_expired": int(pending or 0), "erased": int(erased or 0)}
