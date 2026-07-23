"""UserWorldState (R3) — per-user world state for the general agent runtime. First fact: timezone.

The live bug: "i'm in cst" was acknowledged but never stored, so later events kept using a default zone.
This module persists the user's canonical IANA timezone (America/Chicago, NEVER "CST") and resolves the
abbreviations/regions a student actually types. Temporal resolution + calendar writes read the stored tz
instead of a hard-coded default.

Resolution order for the tz used on a write (higher wins): explicit saved preference -> (later: device /
calendar / school region) -> DEFAULT. Only the saved-preference tier exists today; the rest are TODO tiers
the runtime fills in as those signals land.
"""

from __future__ import annotations

import re
from uuid import UUID
from zoneinfo import available_timezones

from sqlalchemy import select

from . import schema
from .db import user_session

# Abbreviation / spoken region -> canonical IANA. We store the IANA; the abbreviation is never persisted.
_TZ_MAP: dict[str, str] = {
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "eastern": "America/New_York",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago", "central": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver", "mountain": "America/Denver",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "az": "America/Phoenix", "arizona": "America/Phoenix",
    "akst": "America/Anchorage", "akdt": "America/Anchorage", "alaska": "America/Anchorage",
    "hst": "Pacific/Honolulu", "hawaii": "Pacific/Honolulu",
    "gmt": "Etc/UTC", "utc": "Etc/UTC", "zulu": "Etc/UTC",
    "bst": "Europe/London", "london": "Europe/London",
    "cet": "Europe/Paris", "ist": "Asia/Kolkata",
}
_IANA = available_timezones()
_IANA_LOWER = {z.lower(): z for z in _IANA}

# First-person "this is MY timezone" context (so "the game's in central time" doesn't set the user's tz).
_FIRST_PERSON_TZ = re.compile(
    r"\b(?:i'?m|i am|im)\s+(?:in|on|at|living\s+in)\b|\bmy\s+(?:time\s*zone|tz|zone)\b|"
    r"\bi\s+live\s+in\b|\bset\s+my\s+(?:time\s*zone|tz)\b|\b(?:my\s+)?time\s*zone\s+is\b|"
    r"\bi'?m\s+\w+\s+time\b|\bfor\s+me\s+(?:it'?s|its)\b", re.IGNORECASE)


def canonical_timezone(text: str | None) -> str | None:
    """A stated timezone ("cst", "central time", "America/Chicago") -> canonical IANA, or None."""
    t = (text or "").lower()
    m = re.search(r"\b([a-z]+/[a-z_]+(?:/[a-z_]+)?)\b", t)   # a literal IANA zone
    if m and m.group(1) in _IANA_LOWER:
        return _IANA_LOWER[m.group(1)]
    for tok in re.findall(r"\b([a-z]{2,8})\b", t):
        z = _TZ_MAP.get(tok)
        if z:
            return z
    return None


def detect_user_timezone_statement(text: str | None) -> str | None:
    """The IANA zone IFF the user is stating THEIR OWN timezone ("yo i'm in cst"), else None."""
    tz = canonical_timezone(text)
    if not tz:
        return None
    return tz if _FIRST_PERSON_TZ.search(text or "") else None


_FRIENDLY: dict[str, str] = {
    "America/New_York": "eastern time", "America/Chicago": "central time",
    "America/Denver": "mountain time", "America/Los_Angeles": "pacific time",
    "America/Phoenix": "arizona time", "America/Anchorage": "alaska time",
    "Pacific/Honolulu": "hawaii time", "Etc/UTC": "utc", "Europe/London": "uk time",
}


def friendly_name(tz: str) -> str:
    """A student-facing name for a zone: 'central time' for America/Chicago, else the city."""
    return _FRIENDLY.get(tz, tz.split("/")[-1].replace("_", " ").lower())


async def get_timezone(user_id: UUID) -> str | None:
    async with user_session(user_id) as s:
        return (await s.execute(select(schema.UserWorldState.timezone).where(
            schema.UserWorldState.user_id == user_id))).scalar_one_or_none()


async def set_timezone(user_id: UUID, tz: str, *, source: str = "user_stated") -> None:
    """Upsert the user's canonical IANA timezone. One row per user."""
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.UserWorldState).where(
            schema.UserWorldState.user_id == user_id))).scalar_one_or_none()
        if row is None:
            row = schema.UserWorldState(user_id=user_id)
            s.add(row)
        row.timezone = tz
        row.timezone_source = source
        await s.flush()


async def resolve_timezone(user_id: UUID, *, default: str) -> str:
    """The timezone to use for THIS user's temporal ops: saved preference, else the caller's default."""
    try:
        tz = await get_timezone(user_id)
    except Exception:
        tz = None
    return tz or default
