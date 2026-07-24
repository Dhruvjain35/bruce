"""calendar_tools verified-I/O hardening (G0.4, Finding B regression) — a provider write that VERIFIED via
read-back must never be downgraded to a false failure by a best-effort entity-bookkeeping hiccup. Uses the
real FakeCalendarAdapter (models Google semantics) so the actual write + read-back + _matches path runs;
only the post-verification entity_store write is forced to fail."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import uuid4

from bruce_engine import calendar_adapter, calendar_tools, entity_store
from bruce_engine.models import CalendarEvent
from bruce_engine.runtime_contracts import ToolOutcome

ACCOUNT = "me@example.com"


def _run(c):
    return asyncio.run(c)


def _entity(uid, provider_event_id):
    return {"id": str(uid), "title": "chess club", "provider_event_id": provider_event_id,
            "provider_account_id": ACCOUNT, "calendar_id": "primary",
            "start": "2026-07-25T15:00:00", "end": "2026-07-25T16:00:00", "location": None,
            "timezone": "America/Chicago", "source_message_ids": ["m1"]}


async def _bound_ok(_uid):
    return object()                                          # a connected integration (non-None)


async def _boom(*_a, **_k):
    raise RuntimeError("entity db down")


def test_update_stays_verified_when_bookkeeping_fails():
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)
    peid = "evt_upd"
    _run(adapter.insert(CalendarEvent(title="chess club", start="2026-07-25T15:00:00",
                                      end="2026-07-25T16:00:00", timezone="America/Chicago"), peid))
    uid = uuid4()
    with patch.object(calendar_tools, "_bound", _bound_ok), \
         patch.object(entity_store, "mark_updated", _boom):
        tr = _run(calendar_tools.update_event(uid, _entity(uid, peid), new_start="2026-07-25T21:00:00",
                                              new_end="2026-07-25T22:00:00", new_timezone="America/Chicago",
                                              adapter=adapter))
    assert tr.verified is True and tr.outcome is ToolOutcome.ok   # bookkeeping raise did NOT mask the verified write


def test_delete_stays_verified_when_bookkeeping_fails():
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)
    peid = "evt_del"
    _run(adapter.insert(CalendarEvent(title="chess club", start="2026-07-25T15:00:00",
                                      end="2026-07-25T16:00:00", timezone="America/Chicago"), peid))
    uid = uuid4()
    with patch.object(calendar_tools, "_bound", _bound_ok), \
         patch.object(entity_store, "mark_deleted", _boom):
        tr = _run(calendar_tools.delete_event(uid, _entity(uid, peid), adapter=adapter))
    assert tr.verified is True and tr.outcome is ToolOutcome.ok
