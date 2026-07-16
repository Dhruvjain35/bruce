"""Calendar execution + read-back verification.

The point of these tests is adversarial: prove that `verified` CANNOT be produced without a real
read-back that matches. A calendar adapter that reports success on a 2xx write would pass a naive
test suite and lie to the student — so most of what follows is negative cases.

The Google wire format is asserted through an httpx MockTransport against the real client stack
(same approach as the Qwen provider tests), so CI covers the integration without OAuth. The live
test needs real Google credentials and skips with a precise reason when they are absent.
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

from bruce_engine import calendar_adapter as ca
from bruce_engine.calendar_adapter import (
    AlreadyExists,
    CalendarError,
    FakeCalendarAdapter,
    GoogleCalendarAdapter,
    deterministic_event_id,
    execute_and_verify,
)
from bruce_engine.models import CalendarEvent

UID, MID = uuid4(), uuid4()


def _event(**kw) -> CalendarEvent:
    base = dict(title="Science Fair registration closes", start="2026-02-28", source="flyer.png")
    return CalendarEvent(**{**base, **kw})


# --------------------------------------------------------------------------- event id


def test_event_id_is_deterministic_and_provider_legal():
    """Google requires base32hex (a-v, 0-9), length 5-1024. Same inputs -> same id, always."""
    a = deterministic_event_id(UID, MID, _event())
    b = deterministic_event_id(UID, MID, _event())
    assert a == b
    assert 5 <= len(a) <= 1024
    assert set(a) <= set("abcdefghijklmnopqrstuv0123456789"), f"illegal chars for Google: {a}"


def test_event_id_separates_users_missions_and_edited_times():
    """A retry is the same event; a different student, mission, or TIME is a different event.

    The time case matters: if an edited proposal reused the id, the insert would 409 and Bruce
    would 'verify' the OLD time — silently leaving the student with the wrong event.
    """
    base = deterministic_event_id(UID, MID, _event())
    assert deterministic_event_id(uuid4(), MID, _event()) != base
    assert deterministic_event_id(UID, uuid4(), _event()) != base
    assert deterministic_event_id(UID, MID, _event(start="2026-03-14")) != base


# --------------------------------------------------------------------------- execute once


def test_happy_path_executes_and_verifies_via_read_back():
    async def run():
        cal = FakeCalendarAdapter()
        r = await execute_and_verify(cal, _event(), user_id=UID, mission_id=MID)
        assert r.verified is True
        assert r.read_back is not None and r.read_back["summary"] == "Science Fair registration closes"
        assert cal.insert_calls == 1
        # the receipt carries the proof, not just a claim
        ev = r.as_evidence()
        assert ev["verified"] is True and ev["event_id"] == r.event_id and ev["read_back"]

    asyncio.run(run())


def test_retry_executes_exactly_once_and_still_verifies():
    """The execute-once guarantee: the provider rejects the duplicate id, we verify the original.

    Three attempts (double-tap, redelivered webhook, resumed worker) must leave ONE event.
    """
    async def run():
        cal = FakeCalendarAdapter()
        results = [
            await execute_and_verify(cal, _event(), user_id=UID, mission_id=MID) for _ in range(3)
        ]
        assert all(r.verified for r in results)
        assert len({r.event_id for r in results}) == 1  # same logical event every time
        assert len(cal.events) == 1, "a duplicate event reached the calendar"
        assert cal.insert_calls == 3  # we did attempt; the PROVIDER enforced once

    asyncio.run(run())


def test_concurrent_executions_create_exactly_one_event():
    """Two workers racing the same approval must not double-book the student."""
    async def run():
        cal = FakeCalendarAdapter()
        rs = await asyncio.gather(
            execute_and_verify(cal, _event(), user_id=UID, mission_id=MID),
            execute_and_verify(cal, _event(), user_id=UID, mission_id=MID),
        )
        assert all(r.verified for r in rs)
        assert len(cal.events) == 1

    asyncio.run(run())


# --------------------------------------------------------------------------- verification cannot be faked


def test_missing_event_on_read_back_is_not_verified():
    """The write 'succeeded' but the event isn't there -> NOT verified. No false completion."""
    class Vanishing(FakeCalendarAdapter):
        async def get(self, event_id):
            return None  # accepted the write, then has nothing

    async def run():
        r = await execute_and_verify(Vanishing(), _event(), user_id=UID, mission_id=MID)
        assert r.verified is False
        assert "not found" in r.reason and "NOT verified" in r.reason

    asyncio.run(run())


def test_read_back_with_wrong_time_is_not_verified():
    """Provider stored a different start than the student approved -> NOT verified."""
    class Drifting(FakeCalendarAdapter):
        async def get(self, event_id):
            ev = dict(self.events[event_id])
            ev["start"] = {"date": "2026-12-25"}  # not what was approved
            return ev

    async def run():
        r = await execute_and_verify(Drifting(), _event(), user_id=UID, mission_id=MID)
        assert r.verified is False and "start" in r.reason

    asyncio.run(run())


def test_read_back_with_wrong_title_is_not_verified():
    class Renamed(FakeCalendarAdapter):
        async def get(self, event_id):
            ev = dict(self.events[event_id])
            ev["summary"] = "Something else entirely"
            return ev

    async def run():
        r = await execute_and_verify(Renamed(), _event(), user_id=UID, mission_id=MID)
        assert r.verified is False and "title" in r.reason

    asyncio.run(run())


def test_provider_failure_propagates_rather_than_reporting_success():
    """A broken calendar must surface as an error, never as a quiet unverified 'done'."""
    class Broken(FakeCalendarAdapter):
        async def insert(self, event, event_id):
            raise CalendarError("Google events.insert failed: HTTP 503")

    async def run():
        with pytest.raises(CalendarError):
            await execute_and_verify(Broken(), _event(), user_id=UID, mission_id=MID)

    asyncio.run(run())


# --------------------------------------------------------------------------- Google wire format


def _google_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _with_google_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("GOOGLE_REFRESH_TOKEN", "rtoken")
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "primary")


def test_google_insert_sends_caller_supplied_id_and_correct_date_shape(monkeypatch):
    """Caller-supplied id == the execute-once guarantee; date-only must use `date`, not `dateTime`."""
    _with_google_env(monkeypatch)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at-123", "expires_in": 3599})
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": seen["body"]["id"], "htmlLink": "https://cal/x"})

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        eid = deterministic_event_id(UID, MID, _event())
        ref = await cal.insert(_event(), eid)
        assert ref.event_id == eid and ref.html_link == "https://cal/x"

    asyncio.run(run())
    assert seen["url"].endswith("/calendars/primary/events")
    assert seen["auth"] == "Bearer at-123"
    assert seen["body"]["id"] == deterministic_event_id(UID, MID, _event())
    assert seen["body"]["summary"] == "Science Fair registration closes"
    assert seen["body"]["start"] == {"date": "2026-02-28"}, "date-only must not be sent as dateTime"


def test_google_timed_event_uses_datetime_shape(monkeypatch):
    _with_google_env(monkeypatch)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": seen["body"]["id"]})

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        ev = _event(start="2026-02-28T15:00:00Z", end="2026-02-28T16:00:00Z")
        await cal.insert(ev, deterministic_event_id(UID, MID, ev))

    asyncio.run(run())
    assert seen["body"]["start"] == {"dateTime": "2026-02-28T15:00:00Z"}


def test_google_409_becomes_AlreadyExists_not_a_failure(monkeypatch):
    """409 is the execute-once guarantee firing — it must not read as an error."""
    _with_google_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        return httpx.Response(409, json={"error": {"message": "The requested identifier already exists"}})

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        with pytest.raises(AlreadyExists):
            await cal.insert(_event(), deterministic_event_id(UID, MID, _event()))

    asyncio.run(run())


def test_google_cancelled_event_reads_back_as_absent(monkeypatch):
    """A deleted event still returns 200 with status=cancelled. It must NEVER verify."""
    _with_google_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        return httpx.Response(200, json={"id": "x", "status": "cancelled", "summary": "gone"})

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        assert await cal.get("x") is None

    asyncio.run(run())


def test_google_token_failure_does_not_leak_the_client_secret(monkeypatch):
    """The token endpoint echoes request params on error — the message must stay status-only."""
    _with_google_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client", "client_secret": "csecret"})

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        with pytest.raises(CalendarError) as e:
            await cal._access_token()
        assert "csecret" not in str(e.value) and "400" in str(e.value)

    asyncio.run(run())


def test_google_unconfigured_fails_loudly(monkeypatch):
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    async def run():
        with pytest.raises(CalendarError, match="not configured"):
            await GoogleCalendarAdapter().insert(_event(), "abc12")

    asyncio.run(run())


# --------------------------------------------------------------------------- live


def _live_skip() -> str | None:
    missing = [
        k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
        if not os.environ.get(k)
    ]
    return f"Google Calendar not configured — missing {', '.join(missing)}" if missing else None


@pytest.mark.skipif(_live_skip() is not None, reason=_live_skip() or "")
def test_live_google_executes_once_and_reads_back():
    """THE calendar gate: a real event in a real calendar, proven by a real read-back."""
    async def run():
        cal = GoogleCalendarAdapter()
        mid = uuid4()
        ev = _event(title=f"Bruce live test {mid}", start="2026-02-28")
        first = await execute_and_verify(cal, ev, user_id=UID, mission_id=mid)
        assert first.verified is True, first.reason
        again = await execute_and_verify(cal, ev, user_id=UID, mission_id=mid)
        assert again.event_id == first.event_id and again.verified is True

    asyncio.run(run())
