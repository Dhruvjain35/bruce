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

from dataclasses import dataclass, field
from uuid import UUID

_CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
_GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
# Gmail is an INHERITED hand: it reuses the ONE Google integration (same refresh token). These providers
# both resolve to that single integration row; the SCOPE check is what separates "can send mail" from
# "can only touch the calendar". No second connect flow, no Gmail-specific handler.
_GOOGLE_PROVIDERS = ("google_calendar", "gmail")


@dataclass(frozen=True)
class ToolSpec:
    capability: str            # "calendar.create_event"
    provider: str             # "google_calendar"
    operation: str            # "create_event"
    write: bool               # mutates provider state
    live: bool                # implemented AND reachable end-to-end for a real user right now
    reversible: bool = True
    requires_scope: str | None = None
    # a COMPACT argument schema {field: type} — a "?" suffix marks optional. This is what the broker hands a
    # planner so it fills args from the schema, never from ad-hoc handler knowledge. Deliberately tiny.
    arg_schema: dict = field(default_factory=dict)


# Calendar is the first tool set. create is live_write_verified; update/delete/search have adapter methods
# but no conversation route yet -> live=False (honest). Add providers by adding rows, never a handler.
_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("calendar.create_event", "google_calendar", "create_event", write=True, live=True,
             reversible=False, requires_scope=_CAL_SCOPE,
             arg_schema={"title": "str", "start": "datetime", "end": "datetime?", "all_day": "bool?",
                         "timezone": "str?"}),
    ToolSpec("calendar.update_event", "google_calendar", "update_event", write=True, live=True,
             requires_scope=_CAL_SCOPE,
             arg_schema={"target_entity_id": "str", "new_start": "datetime", "new_end": "datetime?",
                         "new_timezone": "str?"}),
    ToolSpec("calendar.delete_event", "google_calendar", "delete_event", write=True, live=True,
             reversible=False, requires_scope=_CAL_SCOPE,
             arg_schema={"target_entity_id": "str"}),
    ToolSpec("calendar.search_events", "google_calendar", "search_events", write=False, live=False,
             requires_scope=_CAL_SCOPE,
             arg_schema={"query": "str", "time_min": "datetime?", "time_max": "datetime?"}),
    # --- Gmail (Phase G) — the first inherited hand. `live` is liveness (implemented + reachable via the
    # SAME generic route calendar mutations use), NOT permission: a user who hasn't granted gmail.send sees
    # INSUFFICIENT_SCOPE from the broker, never a fake "sent". Send + reply + the reads the runtime needs to
    # verify a send and detect a reply are live; recipient-resolution / drafts / search / standalone monitor
    # are honest live=False until a route reaches them.
    ToolSpec("gmail.send_message", "gmail", "send_message", write=True, live=True, reversible=False,
             requires_scope=_GMAIL_SEND,
             arg_schema={"to": "str", "subject": "str", "body": "str", "thread_id": "str?"}),
    ToolSpec("gmail.reply_to_thread", "gmail", "reply_to_thread", write=True, live=True, reversible=False,
             requires_scope=_GMAIL_SEND,
             arg_schema={"thread_id": "str", "body": "str", "to": "str?", "subject": "str?"}),
    ToolSpec("gmail.get_message", "gmail", "get_message", write=False, live=True,
             requires_scope=_GMAIL_READ, arg_schema={"message_id": "str"}),
    ToolSpec("gmail.get_thread", "gmail", "get_thread", write=False, live=True,
             requires_scope=_GMAIL_READ, arg_schema={"thread_id": "str"}),
    ToolSpec("gmail.find_reply", "gmail", "find_reply", write=False, live=True,
             requires_scope=_GMAIL_READ,
             arg_schema={"thread_id": "str", "after_message_id": "str"}),
    ToolSpec("gmail.verify_sent", "gmail", "verify_sent", write=False, live=True,
             requires_scope=_GMAIL_READ,
             arg_schema={"message_id": "str", "to": "str", "subject": "str"}),
    ToolSpec("gmail.search_messages", "gmail", "search_messages", write=False, live=False,
             requires_scope=_GMAIL_READ,
             arg_schema={"query": "str", "max_results": "int?"}),
    ToolSpec("gmail.resolve_recipient", "gmail", "resolve_recipient", write=False, live=False,
             requires_scope=_GMAIL_READ, arg_schema={"name": "str"}),
    ToolSpec("gmail.create_draft", "gmail", "create_draft", write=True, live=False,
             requires_scope=_GMAIL_SEND,
             arg_schema={"to": "str", "subject": "str", "body": "str", "thread_id": "str?"}),
    ToolSpec("gmail.monitor_thread", "gmail", "monitor_thread", write=False, live=False,
             requires_scope=_GMAIL_READ,
             arg_schema={"thread_id": "str", "after_message_id": "str?"}),
)
_BY_CAP: dict[str, ToolSpec] = {t.capability: t for t in _TOOLS}


def get(capability: str) -> ToolSpec | None:
    return _BY_CAP.get(capability)


def specs(domain: str | None = None) -> list[ToolSpec]:
    """Read-only view of the declared tools, optionally filtered to a domain ('calendar'). The ToolBroker
    shortlists from this instead of reaching into the private table — so brokering never sees a tool the
    registry didn't declare."""
    return [t for t in _TOOLS if domain is None or t.capability.startswith(f"{domain}.")]


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
    if t.provider in _GOOGLE_PROVIDERS:
        from . import oauth_google
        try:
            integ = await oauth_google.get_integration(user_id)
        except Exception:
            return False
        connected = (integ is not None and integ.status == "connected"
                     and integ.revoked_at is None and bool(integ.refresh_token_encrypted))
        # connected is necessary but not sufficient — the capability's scope must actually be granted, else a
        # calendar-only connection would falsely report Gmail as available. Availability = connected AND scoped.
        if not connected:
            return False
        return (t.requires_scope is None) or (t.requires_scope in tuple(integ.scopes or ()))
    return False


async def granted_scopes(user_id: UUID, provider: str) -> tuple[str, ...]:
    """The scopes the user's CONNECTED integration actually granted for a provider (empty if not connected).
    Lets the broker distinguish 'connected but missing the scope' (insufficient_scope) from disconnected."""
    if provider in _GOOGLE_PROVIDERS:
        from . import oauth_google
        try:
            integ = await oauth_google.get_integration(user_id)
        except Exception:
            return ()
        if integ is None or integ.status != "connected" or integ.revoked_at is not None:
            return ()
        return tuple(integ.scopes or ())
    return ()
