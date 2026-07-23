"""Google Calendar v3 HTTP contract — the REAL GoogleCalendarAdapter driven against an httpx
MockTransport. This is not a fake product: the adapter's own request construction and response parsing
run end to end; only the wire is mocked. Proves the body Bruce sends Google (exclusive all-day end,
extended-properties metadata) and the failures it maps (409 -> AlreadyExists, 403 insufficient ->
InsufficientScope), plus that the account is read back from organizer/creator email.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bruce_engine import calendar_adapter
from bruce_engine.calendar_adapter import (
    AlreadyExists, GoogleCalendarAdapter, InsufficientScope, execute_and_verify,
)
from bruce_engine.models import CalendarEvent
from uuid import uuid4

UID = uuid4()
MID = uuid4()
ACCOUNT = "founder@example.com"


def _event():
    return CalendarEvent(title="Startup School 2026", start="2026-07-25", end="2026-07-27",
                         location="Chase Center, San Francisco", tentative=False)


class _Google:
    """A minimal in-memory Google that speaks the v3 wire — models id-supplied insert, 409 on a
    duplicate id, and stamping organizer/creator = the connected account on the primary calendar."""

    def __init__(self, *, insert_status=None):
        self.store: dict[str, dict] = {}
        self.captured_insert: dict | None = None
        self.insert_status = insert_status              # force a status (409/403) to test failure mapping

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "at-test", "expires_in": 3600})
        if request.method == "POST" and path.endswith("/events"):
            body = json.loads(request.content)
            self.captured_insert = body
            if self.insert_status == 403:
                return httpx.Response(403, json={"error": {"message": "insufficientPermissions"}})
            eid = body["id"]
            if self.insert_status == 409 or eid in self.store:
                return httpx.Response(409, json={"error": {"message": "duplicate"}})
            stored = {**body, "organizer": {"email": ACCOUNT}, "creator": {"email": ACCOUNT},
                      "status": "confirmed", "htmlLink": f"https://cal/{eid}"}
            self.store[eid] = stored
            return httpx.Response(200, json=stored)
        if request.method == "GET" and "/events/" in path:
            eid = path.rsplit("/", 1)[-1]
            if eid not in self.store:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=self.store[eid])
        return httpx.Response(500, json={})


def _adapter(google: _Google) -> GoogleCalendarAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    # user_id=None -> the env refresh-token path, so no DB/OAuth is needed for the contract test
    return GoogleCalendarAdapter(http_client=client, calendar_id="primary")


@pytest.fixture(autouse=True)
def _google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "primary")


def _run(c):
    return asyncio.run(c)


def test_insert_body_uses_exclusive_all_day_dates_and_extended_properties():
    g = _Google()
    _run(_adapter(g).insert(_event(), "evid123", mission_id=MID,
                            source_message_id="pmid-1", attachment_digest="dig123"))
    body = g.captured_insert
    assert body["start"] == {"date": "2026-07-25"}
    assert body["end"] == {"date": "2026-07-27"}                 # Google exclusive end == 2-day event
    assert body["summary"] == "Startup School 2026"
    assert body["location"] == "Chase Center, San Francisco"
    priv = body["extendedProperties"]["private"]
    assert priv["bruce_mission"] == str(MID) and priv["bruce_attachment"] == "dig123"
    assert "bruce_source" in priv and priv["bruce_source"] != "pmid-1"   # hashed, never cleartext


def test_execute_and_verify_reads_account_back_and_verifies():
    g = _Google()
    res = _run(execute_and_verify(_adapter(g), _event(), user_id=UID, mission_id=MID,
                                  source_message_id="pmid-1", attachment_digest="dig",
                                  expected_account=ACCOUNT))
    assert res.verified is True
    assert res.read_back["account"] == ACCOUNT
    assert res.read_back["start"] == "2026-07-25" and res.read_back["end"] == "2026-07-27"


def test_duplicate_insert_409_falls_through_to_verify_never_errors():
    g = _Google(insert_status=409)
    # even when the very first insert 409s, the flow must read back the (pre-existing) event, not crash.
    # seed the store so the read-back finds it.
    ev = _event()
    from bruce_engine.calendar_adapter import deterministic_event_id, _to_google_body
    eid = deterministic_event_id(UID, MID, ev, source_message_id="pmid-1", attachment_digest="dig")
    g.store[eid] = {**_to_google_body(ev, eid, mission_id=MID), "organizer": {"email": ACCOUNT},
                    "status": "confirmed", "htmlLink": f"https://cal/{eid}"}
    res = _run(execute_and_verify(_adapter(g), ev, user_id=UID, mission_id=MID,
                                  source_message_id="pmid-1", attachment_digest="dig",
                                  expected_account=ACCOUNT))
    assert res.verified is True                             # 409 == already executed -> verify, not error


def test_insufficient_scope_403_maps_to_insufficient_scope():
    g = _Google(insert_status=403)
    async def run():
        with pytest.raises(InsufficientScope):
            await _adapter(g).insert(_event(), "evid403", mission_id=MID)
    _run(run())
