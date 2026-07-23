"""ToolRegistry (R5) — the single source of truth for what Bruce can actually do RIGHT NOW.

Capability claims must never be hard-coded in a handler or invented by the model. They come from here:
a declaration of each provider operation + its live status, joined with the live connection state. This
is what makes the "create_event works but update says i can't" contradiction impossible — both answers
now derive from the same registry, so "i can add events but updating isn't live yet" is the truth, not a
canned denial.

`live` means implemented AND reachable from the conversation runtime for a real user — NOT merely that an
adapter method exists. Google's update/delete adapter methods exist but no conversation flow reaches them
yet, so they are live=False until R6 wires them; flipping the flag here (not editing a handler) turns the
honest "not live yet" into a working capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ToolSpec:
    capability: str            # "calendar.create_event"
    provider: str             # "google_calendar"
    operation: str            # "create_event"
    write: bool               # mutates provider state
    live: bool                # implemented AND reachable end-to-end for a real user right now
    reversible: bool = True
    requires_scope: str | None = None


# Calendar is the first tool set. create is live_write_verified; update/delete/search have adapter methods
# but no conversation route yet -> live=False (honest). Add providers by adding rows, never a handler.
_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("calendar.create_event", "google_calendar", "create_event", write=True, live=True,
             requires_scope="https://www.googleapis.com/auth/calendar.events"),
    ToolSpec("calendar.update_event", "google_calendar", "update_event", write=True, live=False,
             requires_scope="https://www.googleapis.com/auth/calendar.events"),
    ToolSpec("calendar.delete_event", "google_calendar", "delete_event", write=True, live=False,
             requires_scope="https://www.googleapis.com/auth/calendar.events"),
    ToolSpec("calendar.search_events", "google_calendar", "search_events", write=False, live=False,
             requires_scope="https://www.googleapis.com/auth/calendar.events"),
)
_BY_CAP: dict[str, ToolSpec] = {t.capability: t for t in _TOOLS}


def get(capability: str) -> ToolSpec | None:
    return _BY_CAP.get(capability)


def is_live(capability: str) -> bool:
    """Declared live (implemented + wired). Does NOT check the user's connection — see is_available."""
    t = _BY_CAP.get(capability)
    return bool(t and t.live)


def live_operations(domain: str) -> list[str]:
    """The operations in a domain that are live right now (e.g. domain='calendar' -> ['create_event'])."""
    return [t.operation for t in _TOOLS if t.capability.startswith(f"{domain}.") and t.live]


async def is_available(capability: str, user_id: UUID) -> bool:
    """The load-bearing predicate: this capability is declared live AND the provider is connected + healthy
    for THIS user. One check that replaces the four divergent connection checks scattered in the runtime."""
    t = _BY_CAP.get(capability)
    if t is None or not t.live:
        return False
    if t.provider == "google_calendar":
        from . import oauth_google
        try:
            integ = await oauth_google.get_integration(user_id)
        except Exception:
            return False
        return (integ is not None and integ.status == "connected"
                and integ.revoked_at is None and bool(integ.refresh_token_encrypted))
    return False
