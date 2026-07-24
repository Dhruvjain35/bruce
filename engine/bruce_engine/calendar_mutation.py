"""Calendar mutation + repair (R6 conversation surface + R9). "move chess class to 9pm", "delete chess
class", and corrections ("not today, i said 4 days from now") resolve a canonical entity, recompute only
the parts the user changed, call the verified provider tool, and reply from the verified result.

Deterministic verbs classify the mutation; the model's actionable/correction intent supplements. Entity
resolution fails CLOSED on ambiguity (asks which one) — a mutation never touches a guessed event.
"""

from __future__ import annotations

import datetime as _dt
import re
from uuid import UUID
from zoneinfo import ZoneInfo

from . import calendar_tools, entity_resolution, temporal, world_state
from .calendar_schedule import DEFAULT_TZ
from .runtime_contracts import ToolOutcome

_DELETE_RE = re.compile(r"\b(delete|remove|cancel|get\s+rid\s+of|take\s+off)\b", re.IGNORECASE)
# Unambiguous mutate verbs only. "make it"/"set it"/"clear" are excluded — they co-occur with CREATE
# ("add chess class and make it 5pm") and would hijack it.
_UPDATE_RE = re.compile(r"\b(move|reschedul\w*|change|update|push\s+back|push\s+it|shift|switch|bump)\b",
                        re.IGNORECASE)
# corrections: "not today", "i meant", "i said", "actually ... not", "no i said", "that's wrong"
_CORRECTION_RE = re.compile(
    r"\bnot\s+(?:today|tomorrow|then|that)\b|\bi\s+(?:meant|said)\b|\bactually\b|\bno,?\s+i\b|"
    r"\bthat'?s\s+(?:wrong|not\s+right)\b|\bwrong\s+(?:date|time|day)\b|\bmy\s+bad\b", re.IGNORECASE)


def classify(text: str | None) -> str | None:
    """'delete' | 'update' | 'repair' | None. Delete beats update; a correction is a repair."""
    t = text or ""
    if _DELETE_RE.search(t):
        return "delete"
    if _CORRECTION_RE.search(t):
        return "repair"
    if _UPDATE_RE.search(t):
        return "update"
    return None


def _parse(iso: str) -> tuple[_dt.date, _dt.time | None]:
    """(date, time|None) from an ISO start — time is None for an all-day date."""
    if len(iso) == 10:
        return _dt.date.fromisoformat(iso), None
    dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.date(), dt.time()


def recompute(entity: dict, text: str, *, now: _dt.datetime) -> tuple[str, str | None, str | None] | None:
    """Merge the change the user stated onto the entity's current time — keep what they DIDN'T change.
    "move to 9pm" keeps the date, changes the time; "not today, 4 days from now" keeps the time, fixes the
    date. Returns (start_iso, end_iso, timezone) or None if nothing temporal was stated."""
    base_date, base_time = _parse(entity["start"])
    base_tz = entity.get("timezone") or DEFAULT_TZ
    # duration to PRESERVE when only the start moves (never silently shrink a 2h event to 1h)
    duration = _dt.timedelta(hours=1)
    if base_time is not None and entity.get("end") and len(entity["end"]) > 10:
        try:
            duration = (_dt.datetime.fromisoformat(entity["end"].replace("Z", "+00:00"))
                        - _dt.datetime.fromisoformat(entity["start"].replace("Z", "+00:00")))
        except ValueError:
            pass
    if duration <= _dt.timedelta(0):
        duration = _dt.timedelta(hours=1)

    date_part = temporal._resolve_date(text, now)                 # (date, end_date) | None
    rng = temporal._resolve_time_range(text)
    time_part = temporal._resolve_time(text) if rng is None else None
    if date_part is None and time_part is None and rng is None:
        return None
    new_date = date_part[0] if date_part else base_date

    if rng is not None:
        sh, sm, eh, em, _c, _n = rng
        start = _dt.datetime.combine(new_date, _dt.time(sh, sm))
        end = _dt.datetime.combine(new_date, _dt.time(eh, em))
        if end <= start:
            end += _dt.timedelta(days=1)
        return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"), base_tz
    if time_part is not None:
        h, mn, _c, _n = time_part
        start = _dt.datetime.combine(new_date, _dt.time(h, mn))
        return start.isoformat(timespec="seconds"), (start + duration).isoformat(timespec="seconds"), base_tz
    if base_time is not None:                                     # date changed, keep clock + duration
        start = _dt.datetime.combine(new_date, base_time)
        return start.isoformat(timespec="seconds"), (start + duration).isoformat(timespec="seconds"), base_tz
    # all-day: date changed (keep a multi-day span if the correction gave a range)
    last = date_part[1] if (date_part and date_part[1]) else new_date
    return new_date.isoformat(), (last + _dt.timedelta(days=1)).isoformat(), None


def _human(iso: str, *, now: _dt.datetime) -> str:
    from .calendar_schedule import human_when
    from .models import CalendarEvent
    ev = CalendarEvent(title="x", start=iso, end=None)
    return human_when(ev, now=now)


async def handle(user_id: UUID, kind: str, text: str, *, adapter=None) -> str:
    """Resolve the entity, perform the verified mutation, and return the honest reply text."""
    tz = await world_state.resolve_timezone(user_id, default=DEFAULT_TZ)
    now = _dt.datetime.now(ZoneInfo(tz))

    # A correction may NAME the event ("actually move chess club to friday"); resolve by title first, and
    # fall back to the most-recent event only for a genuinely title-less correction ("not today, ...").
    res = await entity_resolution.resolve(user_id, text)
    if kind == "repair" and res.status == "not_found":
        res = await entity_resolution.resolve_most_recent(user_id)
    if res.status == "not_found":
        return "i don't see that one on ur calendar. which event do u mean?"
    if res.status == "ambiguous":
        names = ", ".join(dict.fromkeys(c["title"].lower() for c in res.candidates))
        return f"which one do u mean, {names}?"
    entity = res.entity

    if kind == "delete":
        tr = await calendar_tools.delete_event(user_id, entity, adapter=adapter)
        if tr.verified:
            return f"done, deleted {entity['title'].lower()} from ur calendar ✅"
        if tr.outcome is ToolOutcome.unauthorized:
            return "ur google calendar isn't connected, so i can't delete anything."
        return f"i tried to delete {entity['title'].lower()} but couldn't confirm it's gone, so i'm not calling it done."

    # update / repair -> recompute the time and PUT
    recomputed = recompute(entity, text, now=now)
    if recomputed is None:
        return f"what time should i move {entity['title'].lower()} to?"
    start, end, new_tz = recomputed
    tr = await calendar_tools.update_event(user_id, entity, new_start=start, new_end=end,
                                           new_timezone=new_tz, adapter=adapter)
    if tr.verified:
        when = _human(start, now=now)
        verb = "fixed" if kind == "repair" else "done"
        return f"{verb}, {entity['title'].lower()} is now {when} ✅"
    if tr.outcome is ToolOutcome.unauthorized:
        return "ur google calendar isn't connected, so i can't update anything."
    return f"google rejected the update, so {entity['title'].lower()} is still at its old time. i'm looking into it."
