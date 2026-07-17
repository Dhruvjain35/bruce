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
        assert r.read_back is not None and r.read_back["title"] == "Science Fair registration closes"
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
            ev = dict(await super().get(event_id))
            ev["start"] = "2026-12-25"  # not what was approved
            return ev

    async def run():
        r = await execute_and_verify(Drifting(), _event(), user_id=UID, mission_id=MID)
        assert r.verified is False and "start" in r.reason

    asyncio.run(run())


def test_read_back_with_wrong_title_is_not_verified():
    class Renamed(FakeCalendarAdapter):
        async def get(self, event_id):
            ev = dict(await super().get(event_id))
            ev["title"] = "Something else entirely"
            return ev

    async def run():
        r = await execute_and_verify(Renamed(), _event(), user_id=UID, mission_id=MID)
        assert r.verified is False and "title" in r.reason

    asyncio.run(run())


def test_provider_failure_propagates_rather_than_reporting_success():
    """A broken calendar must surface as an error, never as a quiet unverified 'done'."""
    class Broken(FakeCalendarAdapter):
        async def insert(self, event, event_id, *, mission_id=None):
            raise CalendarError("Google events.insert failed: HTTP 503")

    async def run():
        with pytest.raises(CalendarError):
            await execute_and_verify(Broken(), _event(), user_id=UID, mission_id=MID)

    asyncio.run(run())


# --------------------------------------------------------------------------- normalization / marker


def test_read_back_carries_no_google_specific_field_names():
    """The domain must never learn Google's vocabulary, or swapping to CalDAV means rewriting the
    verifier. The receipt should show the fields that were COMPARED, in Bruce's terms."""
    async def run():
        r = await execute_and_verify(FakeCalendarAdapter(), _event(), user_id=UID, mission_id=MID)
        assert set(r.read_back) >= {"title", "start", "end"}
        for google_only in ("summary", "dateTime", "htmlLink", "organizer"):
            assert google_only not in r.read_back

    asyncio.run(run())


def test_receipt_shows_the_fields_that_were_compared():
    """A human must be able to check the verification themselves rather than trust `verified:true`."""
    async def run():
        r = await execute_and_verify(FakeCalendarAdapter(), _event(), user_id=UID, mission_id=MID)
        ev = r.as_evidence()
        assert ev["read_back"]["title"] == "Science Fair registration closes"
        assert ev["read_back"]["start"] == "2026-02-28"

    asyncio.run(run())


def test_created_event_carries_an_unobtrusive_mission_marker():
    """Links the event back to its mission for undo/audit — and must expose nothing about the student."""
    async def run():
        cal = FakeCalendarAdapter()
        await execute_and_verify(cal, _event(), user_id=UID, mission_id=MID)
        raw = next(iter(cal.events.values()))
        assert ca.mission_marker(MID) in raw["description"]
        assert str(UID) not in raw["description"], "the marker must not leak the student's id"

    asyncio.run(run())


def test_end_mismatch_is_not_verified():
    """end is part of what the student approved — a drifting end time must fail verification."""
    class BadEnd(FakeCalendarAdapter):
        async def get(self, event_id):
            ev = dict(await super().get(event_id))
            ev["end"] = "2099-01-01"
            return ev

    async def run():
        r = await execute_and_verify(BadEnd(), _event(), user_id=UID, mission_id=MID)
        assert r.verified is False and "end" in r.reason

    asyncio.run(run())


def test_absent_location_does_not_cause_a_false_mismatch():
    """Google returns nothing for a field we never sent. Treating that as a mismatch would fail
    every event without a location — a verifier that cries wolf gets ignored."""
    async def run():
        r = await execute_and_verify(FakeCalendarAdapter(), _event(), user_id=UID, mission_id=MID)
        assert r.verified is True

    asyncio.run(run())


def test_location_mismatch_is_caught_when_it_was_approved():
    class MovedRoom(FakeCalendarAdapter):
        async def get(self, event_id):
            ev = dict(await super().get(event_id))
            ev["location"] = "Somewhere else"
            return ev

    async def run():
        r = await execute_and_verify(MovedRoom(), _event(location="Gym"), user_id=UID, mission_id=MID)
        assert r.verified is False and "location" in r.reason

    asyncio.run(run())


# --------------------------------------------------------------------------- undo


def test_undo_deletes_and_proves_absence_by_read_back():
    """Symmetric with execute: a 204 is a claim, an absent read-back is evidence."""
    async def run():
        cal = FakeCalendarAdapter()
        ev = _event()
        r = await execute_and_verify(cal, ev, user_id=UID, mission_id=MID)
        u = await ca.undo(cal, event_id=r.event_id)
        assert u.verified is True and "gone" in u.reason
        assert cal.events == {}

    asyncio.run(run())


def test_undo_is_idempotent():
    """A student tapping Undo twice must not see a failure for work that is genuinely done."""
    async def run():
        cal = FakeCalendarAdapter()
        r = await execute_and_verify(cal, _event(), user_id=UID, mission_id=MID)
        first = await ca.undo(cal, event_id=r.event_id)
        second = await ca.undo(cal, event_id=r.event_id)
        assert first.verified is True and second.verified is True

    asyncio.run(run())


def test_undo_that_did_not_actually_delete_is_not_confirmed():
    """The delete 'succeeded' but the event is still there -> NOT reversed. No fake undo receipt."""
    class Stubborn(FakeCalendarAdapter):
        async def delete(self, event_id):
            return True  # claims success, changes nothing

    async def run():
        cal = Stubborn()
        r = await execute_and_verify(cal, _event(), user_id=UID, mission_id=MID)
        u = await ca.undo(cal, event_id=r.event_id)
        assert u.verified is False and "still present" in u.reason

    asyncio.run(run())


# --------------------------------------------------------------------------- typed Google failures


@pytest.mark.parametrize(
    "status,body,exc",
    [
        (403, {"error": {"errors": [{"reason": "insufficientPermissions"}]}}, ca.InsufficientScope),
        (401, {"error": "unauthorized"}, ca.CalendarAuthError),
        (404, {"error": "not found"}, ca.CalendarNotFound),
        (429, {"error": "rate limit"}, ca.RateLimited),
    ],
)
def test_google_failures_are_typed_so_the_product_can_react(monkeypatch, status, body, exc):
    """'Reconnect', 'grant permission' and 'retry later' are different instructions to a student.
    A single generic CalendarError would make all three read as 'something broke'."""
    _with_google_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        return httpx.Response(status, json=body)

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        with pytest.raises(exc):
            await cal.insert(_event(), deterministic_event_id(UID, MID, _event()))

    asyncio.run(run())


def test_google_delete_treats_already_gone_as_absent(monkeypatch):
    """410/404 on delete means the goal state is reached — undo must be idempotent."""
    _with_google_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        return httpx.Response(410, json={})

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        assert await cal.delete("someid") is False

    asyncio.run(run())


def test_google_get_returns_normalized_not_raw(monkeypatch):
    _with_google_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})
        return httpx.Response(200, json={
            "id": "e1", "summary": "Registration closes", "status": "confirmed",
            "start": {"date": "2026-02-28"}, "end": {"date": "2026-02-28"},
            "htmlLink": "https://cal/e1",
        })

    async def run():
        cal = GoogleCalendarAdapter(http_client=_google_client(handler))
        got = await cal.get("e1")
        assert got["title"] == "Registration closes" and got["start"] == "2026-02-28"
        assert "summary" not in got, "raw Google fields must not cross the adapter boundary"

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
