"""Real Google Calendar "schedule this for me" — the operation graph + honest state machine.

This is the milestone seam: a student hands Bruce an event flyer and says "handle this", and Bruce puts
a REAL event on their REAL connected Google Calendar, then PROVES it by reading it back — replying only
after that read-back verifies. Nothing here says "done" on the strength of a write.

Design guarantees:
  * BOUND TO THE EXACT ACCOUNT. Every op resolves the ONE ``Integration`` row for the owner (never guesses
    a Google account). If the calendar isn't connected, the honest state is ``not_connected`` — no write.
  * EXECUTE ONCE. The calendar event id is deterministic in (owner, mission, source message, attachment
    digest), so a relay redelivery / retry re-derives the SAME id: Google 409s and we fall through to
    read-back — never a duplicate. The mission itself is idempotent on (owner, source message, capability).
  * FETCH-BACK MANDATORY. ``verified`` is set only by an independent read-back whose title, start, end,
    location, and — once known — the account match. A mismatch or absence is ``verification_inconclusive``
    or ``failed``, never success.
  * HONEST STATES, DURABLE. Each state (parsed -> prepared -> creation_attempted -> created -> fetched_back
    -> verified / failed / verification_inconclusive) is recorded as a ``MissionPhaseEvent``, and the
    read-back evidence lands in a ``Receipt``. The mission is marked ``succeeded`` only at ``verified``.

Provider identity, honestly: with the ``calendar.events`` scope a student may have granted, Google returns
401/403 for every pre-write identity endpoint (proven live). So for such a connection the account is
learned from the AUTHORITATIVE created-event record (organizer/creator email) during the mandatory
read-back and backfilled onto the integration; connections that also carried ``userinfo.email`` already
have it before any write.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

import httpx

from . import calendar_adapter, mission_kernel, oauth_google, schema
from .db import user_session
from .models import CalendarEvent, MissionPhase

if TYPE_CHECKING:
    from .conversation_contract import ConversationDecision

log = logging.getLogger("bruce.calendar")   # content-free: ids/states only, never user text

_CAPABILITY = "calendar.create_event"

_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ScheduleState(str, Enum):
    """The honest lifecycle. The reply and the receipt are driven by exactly this — never a state
    ahead of the evidence (a write is never reported as ``verified``)."""

    not_connected = "not_connected"
    parsed = "parsed"
    prepared = "prepared"
    creation_attempted = "creation_attempted"
    created = "created"
    fetched_back = "fetched_back"
    verified = "verified"
    failed = "failed"
    verification_inconclusive = "verification_inconclusive"


@dataclass
class ScheduleResult:
    state: ScheduleState
    mission_id: UUID
    title: str = ""
    all_day: bool = False
    event_id: str | None = None
    account: str | None = None
    html_link: str | None = None
    reason: str = ""


# --------------------------------------------------------------------------- extraction (pure)

def attachment_digest(attachment_refs: list[dict] | None) -> str:
    """A stable digest of the source attachments (metadata only, never bytes) for the idempotency key.
    Empty string when there are none, so a text-only handoff still has a stable key."""
    if not attachment_refs:
        return ""
    parts = sorted(
        f"{a.get('media_type')}|{a.get('filename')}|{a.get('sha256') or a.get('source') or ''}"
        for a in attachment_refs
    )
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:32]


def is_all_day(event: CalendarEvent) -> bool:
    return len(event.start) == 10          # ISO date-only (YYYY-MM-DD), no time component


def _extract_times(decision: "ConversationDecision") -> tuple[list[str], list[str]]:
    """Grounded ISO datetimes and dates the model pulled from the flyer, de-duplicated + sorted.
    A date that is merely the date-part of a captured datetime is NOT double-counted."""
    datetimes: list[str] = []
    dates: list[str] = []
    for e in decision.extracted_entities:
        et = (e.type or "").lower()
        if not any(k in et for k in ("date", "time", "day", "when")):
            continue
        for src in (e.normalized, e.value):
            if not src:
                continue
            for m in _ISO_DATETIME.findall(src):
                if m not in datetimes:
                    datetimes.append(m)
            for m in _ISO_DATE.findall(src):
                if any(dt.startswith(m) for dt in datetimes) or m in dates:
                    continue
                dates.append(m)
    return sorted(datetimes), sorted(dates)


def _plus_day(iso_date: str) -> str:
    return (_dt.date.fromisoformat(iso_date) + _dt.timedelta(days=1)).isoformat()


def _parse_dt(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _plus_hour(iso_dt: str) -> str:
    dt = _parse_dt(iso_dt) + _dt.timedelta(hours=1)
    out = dt.isoformat()
    return out.replace("+00:00", "Z") if iso_dt.endswith("Z") else out


def build_calendar_event(
    decision: "ConversationDecision", *, source: str | None = None
) -> CalendarEvent | None:
    """Turn a grounded event decision into a CalendarEvent, or None if no date is resolvable.

    Timed vs all-day vs multi-day is decided from the grounded entities, never invented:
      * a datetime present -> timed (end = the next datetime, else +1h)
      * only dates -> all-day; a single date is one day, several dates span min..max
      * all-day end uses Google's EXCLUSIVE end semantics: a July 25-26 event is start 2026-07-25,
        end 2026-07-27 (the day after the last inclusive day), so the calendar shows exactly two days.
    Returns None (honest: ask for the date) when there is no grounded date at all — never guesses.
    """
    from .conversation_outcomes import _event_fields

    title, _when, where, _prov = _event_fields(decision)
    datetimes, dates = _extract_times(decision)
    if datetimes:
        start = datetimes[0]
        end = datetimes[1] if len(datetimes) > 1 else _plus_hour(start)
        return CalendarEvent(title=title, start=start, end=end, location=where or None,
                             source=source, tentative=False)
    if dates:
        start = dates[0]
        end = _plus_day(dates[-1])          # exclusive end = day after the last inclusive day
        return CalendarEvent(title=title, start=start, end=end, location=where or None,
                             source=source, tentative=False)
    return None


def human_when(event: CalendarEvent) -> str:
    """A student-facing date phrase. All-day multi-day collapses the EXCLUSIVE end back to the
    inclusive last day: start 2026-07-25 / end 2026-07-27 -> "july 25-26" (the en dash between the
    two day numbers is a numeric range, which the outbound gate preserves as a fact)."""
    if is_all_day(event):
        start = _dt.date.fromisoformat(event.start)
        if event.end and event.end != event.start:
            last = _dt.date.fromisoformat(event.end) - _dt.timedelta(days=1)
        else:
            last = start
        month = start.strftime("%B").lower()
        if last <= start:
            return f"{month} {start.day}"
        if last.month == start.month and last.year == start.year:
            return f"{month} {start.day}–{last.day}"
        return f"{month} {start.day}–{last.strftime('%B').lower()} {last.day}"
    dt = _parse_dt(event.start)
    hour = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    minute = f":{dt.minute:02d}" if dt.minute else ""
    return f"{dt.strftime('%B').lower()} {dt.day} at {hour}{minute}{ampm}"


# --------------------------------------------------------------------------- execute (mutating)

async def _write_receipt(user_id: UUID, mission_id: UUID, outcome: str, evidence: dict) -> None:
    async with user_session(user_id) as s:
        s.add(schema.Receipt(user_id=user_id, mission_id=mission_id, outcome=outcome, evidence=evidence))
        await s.flush()


async def schedule_event(
    user_id: UUID,
    mission_id: UUID,
    event: CalendarEvent,
    *,
    source_message_id: str,
    attachment_digest: str = "",
    http_client: httpx.AsyncClient | None = None,
    adapter: calendar_adapter.CalendarAdapter | None = None,
) -> ScheduleResult:
    """The operation graph: bind account -> prepare -> create-once -> fetch back -> verify -> receipt.

    Records every honest state as a durable phase event. Returns a ScheduleResult the handler renders
    into the reply. Marks the mission ``succeeded`` ONLY when the read-back verified.

    ``adapter`` defaults to the real GoogleCalendarAdapter for this user; CI injects a
    FakeCalendarAdapter (which models Google's 409 + account stamping) to exercise the real mission /
    receipt / idempotency / backfill logic against Postgres without a network or OAuth."""
    all_day = is_all_day(event)

    # 1. bind to the EXACT connected integration — never guess an account
    integ = await oauth_google.get_integration(user_id)
    if (integ is None or integ.status != "connected" or not integ.refresh_token_encrypted
            or integ.revoked_at is not None):
        await mission_kernel.record_phase(
            user_id, mission_id, MissionPhase.blocked.value, "calendar_not_connected", status="running")
        await _write_receipt(user_id, mission_id, "not_connected",
                             {"reason": "google_calendar_not_connected"})
        return ScheduleResult(state=ScheduleState.not_connected, mission_id=mission_id,
                              title=event.title, all_day=all_day)

    bound_account = integ.provider_account_id          # may be None on a calendar.events-only connect
    calendar_id = integ.selected_calendar_id or "primary"

    # 2. prepared
    await mission_kernel.record_phase(
        user_id, mission_id, MissionPhase.extracting.value, "prepared", status="running")

    if adapter is None:
        adapter = calendar_adapter.GoogleCalendarAdapter(
            http_client=http_client, user_id=user_id, calendar_id=calendar_id)

    # 3. creation attempted -> create once
    await mission_kernel.record_phase(
        user_id, mission_id, MissionPhase.executing.value, "creation_attempted", status="running")
    try:
        result = await calendar_adapter.execute_and_verify(
            adapter, event, user_id=user_id, mission_id=mission_id,
            source_message_id=source_message_id, attachment_digest=attachment_digest,
            expected_account=bound_account,        # None -> learn+backfill; known -> HARD-verify
            expected_timezone=None,                # all-day carries none; the event model has no tz field
        )
    except calendar_adapter.CalendarError as exc:
        await mission_kernel.record_phase(
            user_id, mission_id, MissionPhase.failed.value, f"provider_error:{type(exc).__name__}",
            status="running")
        await _write_receipt(user_id, mission_id, "failed",
                             {"error": type(exc).__name__, "detail": str(exc)[:200]})
        return ScheduleResult(state=ScheduleState.failed, mission_id=mission_id, title=event.title,
                              all_day=all_day, reason=str(exc)[:200])

    read_account = (result.read_back or {}).get("account")
    # 4. learn the account from the authoritative record if the connection couldn't tell us up front
    if not bound_account and read_account:
        await oauth_google.backfill_account(user_id, read_account)

    # 5. fetched back
    await mission_kernel.record_phase(
        user_id, mission_id, MissionPhase.verifying.value, "fetched_back", status="running")

    evidence = {**result.as_evidence(), "account": read_account, "all_day": all_day}
    if result.verified:
        await mission_kernel.record_phase(
            user_id, mission_id, MissionPhase.succeeded.value, "verified", status="succeeded")
        await _write_receipt(user_id, mission_id, "verified", evidence)
        return ScheduleResult(state=ScheduleState.verified, mission_id=mission_id, title=event.title,
                              all_day=all_day, event_id=result.event_id, account=read_account,
                              html_link=result.html_link, reason=result.reason)

    # a write happened but the read-back did not confirm it: NEVER claim success
    inconclusive = result.read_back is not None
    state = ScheduleState.verification_inconclusive if inconclusive else ScheduleState.failed
    await mission_kernel.record_phase(
        user_id, mission_id, MissionPhase.blocked.value, f"unverified:{result.reason[:80]}",
        status="running")
    await _write_receipt(user_id, mission_id, state.value, evidence)
    return ScheduleResult(state=state, mission_id=mission_id, title=event.title, all_day=all_day,
                          event_id=result.event_id, account=read_account, reason=result.reason)
