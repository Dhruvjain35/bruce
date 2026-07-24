"""CalendarEventEntity store (R7) — the canonical record of every VERIFIED provider event.

One row per real Google event, written only AFTER read-back verification. This is what lets "move guitar
class", "delete that", and a correction resolve to a REAL entity + its provider_event_id instead of
re-parsing text. Owner-scoped (tenant_isolation); unique per (user, provider, provider_event_id) so a
re-verify of the same event upserts rather than duplicating.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import select

from . import schema
from .db import user_session


def normalize_title(title: str | None) -> str:
    """Lowercased alphanumeric tokens for fuzzy reference matching ("Chess Class" -> "chess class")."""
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))


def _to_dict(e: "schema.CalendarEventEntity") -> dict:
    return {
        "id": str(e.id), "title": e.title, "normalized_title": e.normalized_title,
        "start": e.start, "end": e.end, "timezone": e.timezone, "location": e.location,
        "provider": e.provider, "provider_account_id": e.provider_account_id,
        "provider_event_id": e.provider_event_id, "calendar_id": e.calendar_id,
        "agent_run_id": str(e.agent_run_id) if e.agent_run_id else None,
        "provider_version": e.provider_version, "deleted": e.deleted_at is not None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


async def record_event(
    user_id: UUID, *, title: str, start: str, end: str | None, timezone: str | None,
    location: str | None, provider: str, provider_account_id: str | None, provider_event_id: str,
    calendar_id: str = "primary", source_message_ids: list[str] | None = None,
    agent_run_id: UUID | None = None, receipt_id: UUID | None = None,
) -> UUID:
    """Upsert the canonical entity for a verified event. Returns its id."""
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.CalendarEventEntity).where(
            schema.CalendarEventEntity.user_id == user_id,
            schema.CalendarEventEntity.provider == provider,
            schema.CalendarEventEntity.provider_event_id == provider_event_id))).scalar_one_or_none()
        if row is None:
            row = schema.CalendarEventEntity(
                user_id=user_id, provider=provider, provider_event_id=provider_event_id)
            s.add(row)
        row.title = title
        row.normalized_title = normalize_title(title)
        row.start = start
        row.end = end
        row.timezone = timezone
        row.location = location
        row.provider_account_id = provider_account_id
        row.calendar_id = calendar_id
        row.source_message_ids = source_message_ids or []
        if agent_run_id is not None:
            row.agent_run_id = agent_run_id
        if receipt_id is not None:
            row.receipt_id = receipt_id
        row.deleted_at = None
        await s.flush()
        return row.id


async def get_entity(user_id: UUID, entity_id: UUID) -> dict | None:
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.CalendarEventEntity).where(
            schema.CalendarEventEntity.id == entity_id,
            schema.CalendarEventEntity.user_id == user_id))).scalar_one_or_none()
        return _to_dict(row) if row is not None else None


async def active_events(user_id: UUID, *, limit: int = 50) -> list[dict]:
    """All non-deleted events for the owner, most-recently-created first."""
    async with user_session(user_id) as s:
        rows = (await s.execute(select(schema.CalendarEventEntity).where(
            schema.CalendarEventEntity.user_id == user_id,
            schema.CalendarEventEntity.deleted_at.is_(None)).order_by(
            schema.CalendarEventEntity.created_at.desc()).limit(limit))).scalars().all()
        return [_to_dict(r) for r in rows]


async def mark_updated(user_id: UUID, entity_id: UUID, *, start: str, end: str | None,
                       timezone: str | None) -> None:
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.CalendarEventEntity).where(
            schema.CalendarEventEntity.id == entity_id,
            schema.CalendarEventEntity.user_id == user_id))).scalar_one_or_none()
        if row is not None:
            row.start = start
            row.end = end
            row.timezone = timezone
            row.provider_version = (row.provider_version or 1) + 1
            await s.flush()


async def mark_deleted(user_id: UUID, entity_id: UUID) -> None:
    from datetime import datetime, timezone as _tz
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.CalendarEventEntity).where(
            schema.CalendarEventEntity.id == entity_id,
            schema.CalendarEventEntity.user_id == user_id))).scalar_one_or_none()
        if row is not None:
            row.deleted_at = datetime.now(_tz.utc)
            await s.flush()
