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
import json
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


class CalendarAuthError(CalendarError):
    """Not connected / revoked / unauthorized. The student must reconnect — retrying won't help."""


class InsufficientScope(CalendarError):
    """Connected, but without calendar.events. Retrying is pointless; re-consent is required."""


class CalendarNotFound(CalendarError):
    """The calendar or event does not exist (e.g. the student deleted the calendar)."""


class RateLimited(CalendarError):
    """Google 429. Transient — a retry may succeed."""


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


def deterministic_event_id(
    user_id: UUID,
    mission_id: UUID,
    event: CalendarEvent,
    *,
    provider_account: str | None = None,
    source_message_id: str | None = None,
    attachment_digest: str | None = None,
) -> str:
    """A stable, provider-legal id for this exact (user, mission, event).

    Google requires base32hex: characters a-v and 0-9 only, length 5-1024. We base32hex-encode a
    sha256 of the identity tuple and lowercase it, which lands in that alphabet by construction.

    Identity includes title+start (not the whole object) so that re-running the SAME proposal is a
    retry, while a genuinely edited proposal (different time) is a genuinely different event rather
    than a silent no-op that would leave the student with the old time. The idempotency key the
    schedule flow needs is owner + source message + attachment digest + provider account + the
    normalized details, so those are folded in WHEN supplied — a redelivery of the same flyer to the
    same connected account derives the SAME id (Google 409 -> verify, never a duplicate), while the
    same flyer sent to a DIFFERENT connected account is a different event, never silently merged.

    The extra components are appended only when present, so the id is unchanged for legacy callers.
    """
    base = f"{user_id}|{mission_id}|{event.title}|{event.start}"
    extra = [x for x in (provider_account, source_message_id, attachment_digest) if x]
    raw = (base if not extra else base + "|" + "|".join(extra)).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return base64.b32hexencode(digest).decode().rstrip("=").lower()


class CalendarAdapter(Protocol):
    """The canonical calendar interface. NO provider-specific types cross this boundary.

    `get` returns a normalized dict (see _normalize), never a raw Google resource — the domain must
    not learn Google's field names, or swapping to CalDAV/Apple later means touching the verifier.
    """

    async def insert(self, event: CalendarEvent, event_id: str) -> CalendarEventRef: ...
    async def get(self, event_id: str) -> dict | None: ...
    async def update(self, event: CalendarEvent, event_id: str) -> CalendarEventRef: ...
    async def delete(self, event_id: str) -> bool: ...


# The marker Bruce writes into every event it creates. Unobtrusive and non-secret: it links the
# event back to a mission for undo/audit without exposing anything about the student.
BRUCE_MARKER = "bruce:mission:"


def mission_marker(mission_id: UUID) -> str:
    return f"{BRUCE_MARKER}{mission_id}"


def _normalize(raw: dict) -> dict:
    """Google event resource -> Bruce's normalized shape. The ONLY place Google field names live.

    Date-only and timed events are both flattened to a single `start`/`end` string so the verifier
    compares like with like — Google returns `date` for all-day and `dateTime` for timed, and a
    comparison that forgot that would silently pass on a mismatch.
    """
    def when(side: dict | None) -> str | None:
        side = side or {}
        return side.get("dateTime") or side.get("date")

    # The AUTHORITATIVE account this event lives on: the organizer/creator email Google returns. For a
    # student's primary calendar this is their own Google account address, so it is how the schedule
    # flow both learns WHICH account it wrote to (identity) and PROVES it wrote to the intended one.
    account = (raw.get("organizer") or {}).get("email") or (raw.get("creator") or {}).get("email")
    return {
        "id": raw.get("id"),
        "title": raw.get("summary"),
        "start": when(raw.get("start")),
        "end": when(raw.get("end")),
        "timezone": (raw.get("start") or {}).get("timeZone"),
        "location": raw.get("location"),
        "description": raw.get("description"),
        "account": account,
        "calendar_id": account or raw.get("_calendar_id"),
        "status": raw.get("status"),
        "html_link": raw.get("htmlLink"),
    }


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


def _to_google_body(
    event: CalendarEvent,
    event_id: str,
    *,
    mission_id: UUID | None = None,
    source_message_id: str | None = None,
    attachment_digest: str | None = None,
) -> dict:
    """Map a Bruce CalendarEvent onto Google's event resource.

    Date-only values must use `date`, timed values `dateTime` — Google 400s if they are mixed up.

    Bruce metadata (the mission id, a HASH of the source message, the attachment digest) is written to
    ``extendedProperties.private`` — machine-readable, invisible to the student, and queryable so a
    later run can FIND the event Bruce created without scraping the description. The source message id
    is hashed, never stored in cleartext on Google's side.
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
    private: dict[str, str] = {"bruce": "1"}
    if mission_id is not None:
        private["bruce_mission"] = str(mission_id)
    if source_message_id:
        private["bruce_source"] = hashlib.sha256(source_message_id.encode("utf-8")).hexdigest()[:32]
    if attachment_digest:
        private["bruce_attachment"] = attachment_digest[:64]
    body["extendedProperties"] = {"private": private}
    desc = []
    if event.source:
        desc.append(f"Added by Bruce from: {event.source}")
    if mission_id is not None:
        # Unobtrusive, non-secret. Lets undo/audit find the exact event Bruce created without
        # exposing anything about the student.
        desc.append(mission_marker(mission_id))
    if desc:
        body["description"] = "\n".join(desc)
    return body


class GoogleCalendarAdapter:
    """Real Google Calendar v3.

    Credentials come from the OAuth integration for a specific user (bruce_engine.oauth_google) —
    NOT from a process-wide env var, which could only ever serve one account. A short-lived access
    token is fetched per operation from the encrypted refresh token; nothing here logs a token.

    `user_id=None` falls back to GOOGLE_REFRESH_TOKEN from the environment. That path exists ONLY
    for single-account smoke tests before the OAuth flow is connected; it is never used for a real
    student, because it cannot distinguish accounts.
    """

    provider = "google"

    def __init__(
        self,
        http_client: httpx.AsyncClient | None = None,
        *,
        user_id: UUID | None = None,
        calendar_id: str | None = None,
    ) -> None:
        self._client = http_client
        self._user_id = user_id
        self._calendar_id = calendar_id

    async def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=30)

    async def _calendar(self) -> str:
        if self._calendar_id:
            return self._calendar_id
        if self._user_id is not None:
            from . import oauth_google

            row = await oauth_google.get_integration(self._user_id)
            if row is not None and row.selected_calendar_id:
                return row.selected_calendar_id
        return os.environ.get("GOOGLE_CALENDAR_ID", "primary")

    async def _access_token(self) -> str:
        """Short-lived access token for THIS user's connected account."""
        if self._user_id is not None:
            from . import oauth_google

            try:
                return await oauth_google.access_token_for(
                    self._user_id, http_client=self._client
                )
            except oauth_google.OAuthError as exc:
                # Surface the real cause (not connected / revoked) rather than a generic failure —
                # the UI needs to tell the student to reconnect, not "try again".
                raise CalendarError(str(exc)) from exc
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

    def _classify(self, r: httpx.Response, op: str) -> None:
        """Map Google's failures to causes the product can act on. Status only, never the body."""
        if r.status_code in (401, 403):
            body = ""
            try:
                body = json.dumps(r.json())
            except Exception:
                body = ""
            if "insufficientPermissions" in body or "insufficient" in body.lower():
                raise InsufficientScope(f"Google {op}: the connection lacks calendar.events scope")
            raise CalendarAuthError(f"Google {op}: not authorized (HTTP {r.status_code}) — reconnect")
        if r.status_code == 404:
            raise CalendarNotFound(f"Google {op}: calendar or event not found (HTTP 404)")
        if r.status_code == 429:
            raise RateLimited(f"Google {op}: rate limited (HTTP 429)")
        raise CalendarError(f"Google {op} failed: HTTP {r.status_code}")

    async def insert(
        self, event: CalendarEvent, event_id: str, *, mission_id: UUID | None = None,
        source_message_id: str | None = None, attachment_digest: str | None = None,
    ) -> CalendarEventRef:
        cal = await self._calendar()
        token = await self._access_token()
        client = await self._http()
        r = await client.post(
            f"{GOOGLE_CALENDAR_API}/calendars/{cal}/events",
            headers={"Authorization": f"Bearer {token}"},
            json=_to_google_body(event, event_id, mission_id=mission_id,
                                 source_message_id=source_message_id, attachment_digest=attachment_digest),
        )
        if r.status_code == 409:
            raise AlreadyExists(event_id)  # retry of an already-executed insert; not an error
        if r.status_code not in (200, 201):
            self._classify(r, "events.insert")
        body = r.json()
        return CalendarEventRef(
            event_id=body.get("id", event_id), provider=self.provider, html_link=body.get("htmlLink")
        )

    async def get(self, event_id: str) -> dict | None:
        """Independent read-back, NORMALIZED. None if absent; a cancelled event counts as absent."""
        cal = await self._calendar()
        token = await self._access_token()
        client = await self._http()
        r = await client.get(
            f"{GOOGLE_CALENDAR_API}/calendars/{cal}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            self._classify(r, "events.get")
        body = r.json()
        if body.get("status") == "cancelled":
            return None  # present in the API but deleted — must never verify
        body["_calendar_id"] = cal
        return _normalize(body)

    async def update(
        self, event: CalendarEvent, event_id: str, *, mission_id: UUID | None = None
    ) -> CalendarEventRef:
        cal = await self._calendar()
        token = await self._access_token()
        client = await self._http()
        r = await client.put(
            f"{GOOGLE_CALENDAR_API}/calendars/{cal}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=_to_google_body(event, event_id, mission_id=mission_id),
        )
        if r.status_code not in (200, 201):
            self._classify(r, "events.update")
        body = r.json()
        return CalendarEventRef(
            event_id=body.get("id", event_id), provider=self.provider, html_link=body.get("htmlLink")
        )

    async def delete(self, event_id: str) -> bool:
        """Delete. True if deleted, False if it was already gone (410/404) — both are 'absent',
        so undo is naturally idempotent."""
        cal = await self._calendar()
        token = await self._access_token()
        client = await self._http()
        r = await client.delete(
            f"{GOOGLE_CALENDAR_API}/calendars/{cal}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code in (200, 204):
            return True
        if r.status_code in (404, 410):
            return False  # already absent — repeated undo must not error
        self._classify(r, "events.delete")
        return False


# --------------------------------------------------------------------------- fake (tests/demo)


class FakeCalendarAdapter:
    """In-memory calendar that models Google's semantics — including 409 on a duplicate id.

    Not a mock of our own code: it enforces the same rules the real provider does, so the
    execute-once and read-back logic is genuinely exercised without network or OAuth.
    """

    provider = "fake"

    def __init__(self, *, account: str | None = None) -> None:
        self.events: dict[str, dict] = {}
        self.insert_calls = 0
        self.delete_calls = 0
        # The account the provider will stamp as organizer/creator (models Google's primary-calendar
        # behaviour) — lets the read-back's account be verified against the connected integration.
        self.account = account

    async def insert(
        self, event: CalendarEvent, event_id: str, *, mission_id: UUID | None = None,
        source_message_id: str | None = None, attachment_digest: str | None = None,
    ) -> CalendarEventRef:
        self.insert_calls += 1
        if event_id in self.events:
            raise AlreadyExists(event_id)
        body = _to_google_body(event, event_id, mission_id=mission_id,
                               source_message_id=source_message_id, attachment_digest=attachment_digest)
        # Model the provider stamping the connected account as organizer/creator on the primary calendar.
        if self.account:
            body["organizer"] = {"email": self.account}
            body["creator"] = {"email": self.account}
        self.events[event_id] = body
        return CalendarEventRef(
            event_id=event_id, provider=self.provider, html_link=f"https://example.test/{event_id}"
        )

    async def get(self, event_id: str) -> dict | None:
        raw = self.events.get(event_id)
        return _normalize(raw) if raw is not None else None

    async def update(
        self, event: CalendarEvent, event_id: str, *, mission_id: UUID | None = None
    ) -> CalendarEventRef:
        if event_id not in self.events:
            raise CalendarNotFound(event_id)
        self.events[event_id] = _to_google_body(event, event_id, mission_id=mission_id)
        return CalendarEventRef(event_id=event_id, provider=self.provider)

    async def delete(self, event_id: str) -> bool:
        self.delete_calls += 1
        return self.events.pop(event_id, None) is not None


# --------------------------------------------------------------------------- execute + verify


def _matches(
    event: CalendarEvent,
    read_back: dict,
    *,
    expected_timezone: str | None = None,
    expected_account: str | None = None,
) -> tuple[bool, str]:
    """Does what the provider now holds actually match what the student APPROVED?

    Compares the fields a student would be harmed by getting wrong. `location` is compared only
    when it was part of the approval — Google returns None for a field we never sent, and treating
    that as a mismatch would fail every event that simply has no location.

    ``expected_account`` — when known — is HARD: an event that came back on a different Google account
    than the connected integration is the wrong account and must NOT verify, even if the title/time
    match. ``expected_timezone`` is checked only when supplied (all-day events carry none).
    """
    if (read_back.get("title") or "") != event.title:
        return False, "read-back title does not match the approved proposal"
    if read_back.get("start") != event.start:
        return False, f"read-back start {read_back.get('start')!r} does not match the approved {event.start!r}"
    expected_end = event.end or event.start
    if read_back.get("end") != expected_end:
        return False, f"read-back end {read_back.get('end')!r} does not match the approved {expected_end!r}"
    if event.location and (read_back.get("location") or "") != event.location:
        return False, "read-back location does not match the approved proposal"
    if expected_timezone is not None and read_back.get("timezone") != expected_timezone:
        return False, f"read-back timezone {read_back.get('timezone')!r} does not match the approved {expected_timezone!r}"
    if expected_account and (read_back.get("account") or "") != expected_account:
        return False, "read-back is on a different Google account than the connected one — NOT verified"
    return True, "read-back matched the approved proposal"


async def undo(
    adapter: CalendarAdapter, *, event_id: str
) -> VerificationResult:
    """Reverse an executed calendar action, and PROVE it is gone by reading back.

    Symmetric with execute_and_verify: a delete that returns 204 is a claim; a subsequent read that
    finds nothing is evidence. `reversed=True` is only ever set by that read.

    Idempotent: deleting an already-absent event is success, not an error — a student tapping Undo
    twice must not see a failure for work that is genuinely done.
    """
    await adapter.delete(event_id)
    still_there = await adapter.get(event_id)
    if still_there is not None:
        return VerificationResult(
            verified=False,
            event_id=event_id,
            provider=getattr(adapter, "provider", "unknown"),
            reason="event still present after delete — the undo is NOT confirmed",
            read_back=still_there,
        )
    return VerificationResult(
        verified=True,
        event_id=event_id,
        provider=getattr(adapter, "provider", "unknown"),
        reason="read-back confirmed the event is gone",
    )


async def execute_and_verify(
    adapter: CalendarAdapter,
    event: CalendarEvent,
    *,
    user_id: UUID,
    mission_id: UUID,
    source_message_id: str | None = None,
    attachment_digest: str | None = None,
    expected_account: str | None = None,
    expected_timezone: str | None = None,
) -> VerificationResult:
    """Create the event exactly once, then PROVE it exists by reading it back.

    Returns verified=True only when an independent read-back returned an event whose title, start,
    end (and account/timezone when known) match what was approved. Every other path — absent,
    mismatched, wrong account, unreadable — returns verified=False with a reason. Nothing here
    reports success on the strength of the write alone.

    ``source_message_id`` + ``attachment_digest`` fold into the deterministic id (so a redelivery of
    the same flyer to the same account is a 409 -> verify, never a duplicate) and are stamped into the
    event's extended properties. ``expected_account`` binds verification to the connected Google
    account: a read-back on a different account is NOT verified.
    """
    # The id must be STABLE across retries. It is bound to owner + mission + source message +
    # attachment digest — NOT to expected_account, which starts unknown and is learned on the first
    # write: folding a None -> email transition into the id would change it mid-retry and duplicate
    # the event. The owner (user_id) already scopes to exactly one connected integration, so the
    # account is implied; ``expected_account`` is used only to VERIFY the read-back, never to key it.
    event_id = deterministic_event_id(
        user_id, mission_id, event,
        source_message_id=source_message_id, attachment_digest=attachment_digest)
    html_link: str | None = None

    try:
        ref = await adapter.insert(
            event, event_id, mission_id=mission_id,
            source_message_id=source_message_id, attachment_digest=attachment_digest)
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

    ok, reason = _matches(event, read_back, expected_timezone=expected_timezone,
                          expected_account=expected_account)
    return VerificationResult(
        verified=ok,
        event_id=event_id,
        provider=getattr(adapter, "provider", "unknown"),
        reason=reason,
        # The receipt carries exactly the fields that were COMPARED — so a human reading it can
        # check the verification themselves rather than taking `verified: true` on trust.
        read_back={
            k: read_back.get(k)
            for k in ("title", "start", "end", "timezone", "location", "account", "calendar_id", "html_link")
        },
        html_link=html_link or read_back.get("html_link"),
    )
