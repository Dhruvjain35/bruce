"""Provider-neutral calendar CRUD (R6). update/delete on a canonical entity: bind the exact account ->
provider write -> FETCH BACK -> verify -> sync the entity -> ToolResult. Nothing reports success on the
strength of a write; a delete confirms ABSENCE by reading back. create + read-back verify already live in
calendar_schedule.schedule_event — this module adds the mutations + records the entity on a verified
create (record_created).
"""

from __future__ import annotations

import logging
from uuid import UUID

import httpx

from . import calendar_adapter, entity_store, oauth_google
from .models import CalendarEvent
from .runtime_contracts import ToolOutcome, ToolResult

log = logging.getLogger("bruce.calendar")

_CAP_UPDATE = "calendar.update_event"
_CAP_DELETE = "calendar.delete_event"
_PROVIDER = "google_calendar"


async def _bound(user_id: UUID):
    integ = await oauth_google.get_integration(user_id)
    if (integ is None or integ.status != "connected" or integ.revoked_at is not None
            or not integ.refresh_token_encrypted):
        return None
    return integ


def _adapter(user_id: UUID, calendar_id: str, http_client, injected):
    return injected or calendar_adapter.GoogleCalendarAdapter(
        http_client=http_client, user_id=user_id, calendar_id=calendar_id)


async def record_created(user_id: UUID, *, event: CalendarEvent, provider_event_id: str,
                         provider_account_id: str | None, calendar_id: str, source_message_id: str,
                         agent_run_id: UUID | None = None, receipt_id: UUID | None = None) -> UUID:
    """Persist the canonical entity for a just-VERIFIED create, so it can later be moved/deleted."""
    return await entity_store.record_event(
        user_id, title=event.title, start=event.start, end=event.end, timezone=event.timezone,
        location=event.location, provider=_PROVIDER, provider_account_id=provider_account_id,
        provider_event_id=provider_event_id, calendar_id=calendar_id,
        source_message_ids=[source_message_id], agent_run_id=agent_run_id, receipt_id=receipt_id)


async def update_event(
    user_id: UUID, entity: dict, *, new_start: str, new_end: str | None, new_timezone: str | None,
    http_client: httpx.AsyncClient | None = None, adapter=None,
) -> ToolResult:
    """Move/reschedule an existing event to a new time, then PROVE it by reading it back."""
    if await _bound(user_id) is None:
        return ToolResult(ToolOutcome.unauthorized, _CAP_UPDATE, _PROVIDER, "update_event",
                          reason="google_calendar_not_connected")
    cal = entity.get("calendar_id") or "primary"
    a = _adapter(user_id, cal, http_client, adapter)
    eid = entity["provider_event_id"]
    ev = CalendarEvent(title=entity["title"], start=new_start, end=new_end,
                       location=entity.get("location"), timezone=new_timezone)
    src = (entity.get('source_message_ids') or [None])[0]
    try:
        await a.update(ev, eid, source_message_id=src)
    except calendar_adapter.CalendarError as exc:
        return ToolResult(ToolOutcome.provider_error, _CAP_UPDATE, _PROVIDER, "update_event",
                          reason=str(exc)[:200])
    read_back = await a.get(eid)
    if read_back is None:
        return ToolResult(ToolOutcome.verification_inconclusive, _CAP_UPDATE, _PROVIDER, "update_event",
                          provider_entity_id=eid, reason="event not found on read-back after update")
    ok, reason = calendar_adapter._matches(ev, read_back, expected_account=entity.get("provider_account_id"))
    if not ok:
        return ToolResult(ToolOutcome.verification_failed, _CAP_UPDATE, _PROVIDER, "update_event",
                          provider_entity_id=eid, read_back=read_back, reason=reason)
    await entity_store.mark_updated(user_id, UUID(entity["id"]), start=new_start, end=new_end,
                                    timezone=new_timezone)
    log.info("calendar_update_verified user=%s", user_id)
    return ToolResult(ToolOutcome.ok, _CAP_UPDATE, _PROVIDER, "update_event", verified=True,
                      provider_entity_id=eid, read_back=read_back, reason=reason)


async def delete_event(
    user_id: UUID, entity: dict, *, http_client: httpx.AsyncClient | None = None, adapter=None,
) -> ToolResult:
    """Delete an existing event, then PROVE it is gone by reading back (absent/cancelled)."""
    if await _bound(user_id) is None:
        return ToolResult(ToolOutcome.unauthorized, _CAP_DELETE, _PROVIDER, "delete_event",
                          reason="google_calendar_not_connected")
    cal = entity.get("calendar_id") or "primary"
    a = _adapter(user_id, cal, http_client, adapter)
    eid = entity["provider_event_id"]
    try:
        await a.delete(eid)
    except calendar_adapter.CalendarError as exc:
        return ToolResult(ToolOutcome.provider_error, _CAP_DELETE, _PROVIDER, "delete_event",
                          reason=str(exc)[:200])
    read_back = await a.get(eid)                      # None or cancelled -> gone
    if read_back is not None:
        return ToolResult(ToolOutcome.verification_inconclusive, _CAP_DELETE, _PROVIDER, "delete_event",
                          provider_entity_id=eid, read_back=read_back,
                          reason="event still present after delete — NOT confirmed")
    await entity_store.mark_deleted(user_id, UUID(entity["id"]))
    log.info("calendar_delete_verified user=%s", user_id)
    return ToolResult(ToolOutcome.ok, _CAP_DELETE, _PROVIDER, "delete_event", verified=True,
                      provider_entity_id=eid, reason="read-back confirmed the event is gone")
