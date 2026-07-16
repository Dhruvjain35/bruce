"""Calendar execution + read-back verification (#4, execute half).

calendar_build.py BUILDS events (pure functions, .ics, conflict detection). This module EXECUTES
them against a real calendar and then PROVES the result by reading it back. Nothing here trusts a
2xx response as evidence that the event exists.

THE VERIFICATION CONTRACT — the reason this module exists:

    insert -> read back from the provider -> compare the fields we asked for -> only then verified

A write that returns 200 is a claim. A subsequent independent read that returns the same title and
start is evidence. Bruce's promise is "proves the result", so `verified` here means the read-back
happened and matched. If the read-back fails, is missing, or disagrees, the outcome is NOT verified
and the mission must not claim success. There is deliberately no code path that marks something
verified without a successful read-back.

EXECUTION HAPPENS EXACTLY ONCE, enforced remotely:

Google Calendar lets the CALLER supply the event id. Bruce derives that id deterministically from
(user_id, mission_id, event identity), so a retry — a double-tap, a redelivered webhook, a crashed
worker resuming — inserts the SAME id. The second insert is rejected by Google with 409, which we
treat as "already executed" and fall through to read-back. The guarantee is therefore enforced by
the remote system, not merely by local state that a crash could lose. This is the same reasoning as
the UNIQUE(user_id, idempotency_key) constraint on sources: the DB/provider is the arbiter, never a
check-then-act in our process.

PROVIDER-NEUTRAL by design, exactly like llm.py: CalendarAdapter is a Protocol, so the calendar is
swappable (Google today, Apple/CalDAV later) and tests run against a fake that models the real
provider's semantics — including the 409.

NO NEW COLUMNS: the external event id and the read-back proof live in receipts.evidence (JSONB),
which already exists. This branch adds no schema.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import httpx

from .models import CalendarEvent

GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class CalendarError(Exception):
    """Provider call failed. Never swallowed — a silent failure would fake a completion."""


class AlreadyExists(CalendarError):
    """The event id is already present (Google 409). Expected on retry; means execute-once held."""


@dataclass(frozen=True)
class CalendarEventRef:
    """What the provider says exists, after we asked it."""

    event_id: str
    provider: str
    html_link: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of execute -> read-back -> compare. `verified` is only ever set by evidence."""

    verified: bool
    event_id: str
    provider: str
    reason: str
    read_back: dict | None = None
    html_link: str | None = None

    def as_evidence(self) -> dict:
        """Content-free-ish receipt payload: ids, links and the fields we compared. No secrets."""
        return {
            "provider": self.provider,
            "event_id": self.event_id,
            "verified": self.verified,
            "reason": self.reason,
            "html_link": self.html_link,
            "read_back": self.read_back,
        }


def deterministic_event_id(user_id: UUID, mission_id: UUID, event: CalendarEvent) -> str:
    """A stable, provider-legal id for this exact (user, mission, event).

    Google requires base32hex: characters a-v and 0-9 only, length 5-1024. We base32hex-encode a
    sha256 of the identity tuple and lowercase it, which lands in that alphabet by construction.

    Identity includes title+start (not the whole object) so that re-running the SAME proposal is a
    retry, while a genuinely edited proposal (different time) is a genuinely different event rather
    than a silent no-op that would leave the student with the old time.
    """
    raw = f"{user_id}|{mission_id}|{event.title}|{event.start}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return base64.b32hexencode(digest).decode().rstrip("=").lower()


class CalendarAdapter(Protocol):
    async def insert(self, event: CalendarEvent, event_id: str) -> CalendarEventRef: ...
    async def get(self, event_id: str) -> dict | None: ...


# --------------------------------------------------------------------------- Google


def _google_env() -> dict[str, str]:
    missing = [
        k
        for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
        if not os.environ.get(k)
    ]
    if missing:
        raise CalendarError(f"Google Calendar not configured — missing {', '.join(missing)}")
    return {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
        "calendar_id": os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
    }


def _to_google_body(event: CalendarEvent, event_id: str) -> dict:
    """Map a Bruce CalendarEvent onto Google's event resource.

    Date-only values must use `date`, timed values `dateTime` — Google 400s if they are mixed up.
    """
    def when(value: str) -> dict:
        return {"date": value} if len(value) == 10 else {"dateTime": value}

    body: dict = {
        "id": event_id,  # caller-supplied id == the execute-once guarantee
        "summary": event.title,
        "start": when(event.start),
        "end": when(event.end or event.start),
    }
    if event.location:
        body["location"] = event.location
    if event.source:
        body["description"] = f"Added by Bruce from: {event.source}"
    return body


class GoogleCalendarAdapter:
    """Real Google Calendar v3. Reads OAuth config from the environment; never logs tokens."""

    provider = "google"

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client

    async def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=30)

    async def _access_token(self) -> str:
        """Exchange the long-lived refresh token for a short-lived access token."""
        cfg = _google_env()
        client = await self._http()
        r = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "refresh_token": cfg["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if r.status_code != 200:
            # status only — the body can echo client_secret back at us
            raise CalendarError(f"Google token refresh failed: HTTP {r.status_code}")
        return r.json()["access_token"]

    async def insert(self, event: CalendarEvent, event_id: str) -> CalendarEventRef:
        cfg = _google_env()
        token = await self._access_token()
        client = await self._http()
        r = await client.post(
            f"{GOOGLE_CALENDAR_API}/calendars/{cfg['calendar_id']}/events",
            headers={"Authorization": f"Bearer {token}"},
            json=_to_google_body(event, event_id),
        )
        if r.status_code == 409:
            raise AlreadyExists(event_id)  # retry of an already-executed insert; not an error
        if r.status_code not in (200, 201):
            raise CalendarError(f"Google events.insert failed: HTTP {r.status_code}")
        body = r.json()
        return CalendarEventRef(
            event_id=body.get("id", event_id), provider=self.provider, html_link=body.get("htmlLink")
        )

    async def get(self, event_id: str) -> dict | None:
        """Independent read-back. Returns None if absent; a cancelled event counts as absent."""
        cfg = _google_env()
        token = await self._access_token()
        client = await self._http()
        r = await client.get(
            f"{GOOGLE_CALENDAR_API}/calendars/{cfg['calendar_id']}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise CalendarError(f"Google events.get failed: HTTP {r.status_code}")
        body = r.json()
        if body.get("status") == "cancelled":
            return None  # present in the API but deleted — must never verify
        return body


# --------------------------------------------------------------------------- fake (tests/demo)


class FakeCalendarAdapter:
    """In-memory calendar that models Google's semantics — including 409 on a duplicate id.

    Not a mock of our own code: it enforces the same rules the real provider does, so the
    execute-once and read-back logic is genuinely exercised without network or OAuth.
    """

    provider = "fake"

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.insert_calls = 0

    async def insert(self, event: CalendarEvent, event_id: str) -> CalendarEventRef:
        self.insert_calls += 1
        if event_id in self.events:
            raise AlreadyExists(event_id)
        self.events[event_id] = _to_google_body(event, event_id)
        return CalendarEventRef(
            event_id=event_id, provider=self.provider, html_link=f"https://example.test/{event_id}"
        )

    async def get(self, event_id: str) -> dict | None:
        return self.events.get(event_id)


# --------------------------------------------------------------------------- execute + verify


def _matches(event: CalendarEvent, read_back: dict) -> tuple[bool, str]:
    """Does what the provider now holds actually match what the student approved?"""
    if (read_back.get("summary") or "") != event.title:
        return False, "read-back title does not match the approved proposal"
    start = read_back.get("start") or {}
    got = start.get("dateTime") or start.get("date")
    if got != event.start:
        return False, f"read-back start {got!r} does not match the approved {event.start!r}"
    return True, "read-back matched the approved proposal"


async def execute_and_verify(
    adapter: CalendarAdapter,
    event: CalendarEvent,
    *,
    user_id: UUID,
    mission_id: UUID,
) -> VerificationResult:
    """Create the event exactly once, then PROVE it exists by reading it back.

    Returns verified=True only when an independent read-back returned an event whose title and
    start match what was approved. Every other path — absent, mismatched, unreadable — returns
    verified=False with a reason. Nothing here reports success on the strength of the write alone.
    """
    event_id = deterministic_event_id(user_id, mission_id, event)
    html_link: str | None = None

    try:
        ref = await adapter.insert(event, event_id)
        html_link = ref.html_link
    except AlreadyExists:
        # A retry. Execution already happened; fall through and verify the existing event rather
        # than inserting a duplicate. This is the execute-once guarantee holding.
        pass

    read_back = await adapter.get(event_id)
    if read_back is None:
        return VerificationResult(
            verified=False,
            event_id=event_id,
            provider=getattr(adapter, "provider", "unknown"),
            reason="event not found on read-back — the write is unproven, so this is NOT verified",
            html_link=html_link,
        )

    ok, reason = _matches(event, read_back)
    return VerificationResult(
        verified=ok,
        event_id=event_id,
        provider=getattr(adapter, "provider", "unknown"),
        reason=reason,
        read_back={"summary": read_back.get("summary"), "start": read_back.get("start")},
        html_link=html_link or read_back.get("htmlLink"),
    )
