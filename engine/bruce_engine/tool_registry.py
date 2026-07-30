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


# --- what the MODEL calls things, mapped onto what the registry calls them --------------------------------
#
# THE DEFECT THIS CLOSES, measured live on 2026-07-30. A founder asked Bruce to send one email. The model
# emitted `required_capabilities=['email.send_message']` with `intent=actionable` and every entity correct.
# The registry id is `gmail.send_message`. `email.send_message` has the right SHAPE, so
# `transitions.is_operation_id` accepted it, and the wrong NAMESPACE, so `get()` returned None —
# `goal_runtime.creation_verdict` answered `capability_not_in_registry`, NO goal and NO Decision were
# created, and the turn fell through to the model's own words, which promised a send that had not been
# arranged. Zero provider calls happened; the safety envelope held. The honesty envelope did not.
#
# This is the transcript's original defect one layer up. That one was `"sending messages"` — free text
# joining to nothing. This one joins to nothing while LOOKING like an id, which is worse, because every
# shape check passes.
#
# DERIVED, NEVER HAND-LISTED. The aliases are generated from `_TOOLS`, so a new tool gets its aliases for
# free and a renamed capability cannot leave a stale mapping pointing at an id that no longer exists —
# the same discipline `authorization_store._capability_for` already uses. A REAL capability id can never
# be shadowed by an alias: the real ids are removed from the map after it is built.
_FAMILY: dict[str, str] = {"google_calendar": "calendar", "gmail": "email"}
"""provider -> the capability FAMILY a model reasons in. `capability_snapshot.is_usable` and
`semantic_rescue_runtime._FAMILY` speak the same two words; a model told to think in families will say
"email" where the registry says "gmail", and that gap is what the alias map spans."""


def _build_aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in _TOOLS:
        # the PROVIDER-prefixed form: `authorization_evidence` keys on "google_calendar.create_event"
        # while the registry keys on "calendar.create_event", so a model echoing either is understood.
        out.setdefault(f"{spec.provider}.{spec.operation}", spec.capability)
        family = _FAMILY.get(spec.provider)
        if family:
            out.setdefault(f"{family}.{spec.operation}", spec.capability)
    for spec in _TOOLS:
        out.pop(spec.capability, None)          # a real id is never an alias for something else
    return out


_ALIASES: dict[str, str] = _build_aliases()


def canonical(capability: str | None) -> str:
    """The registry id for whatever the model called this operation. Total, and never invents one.

    An id the registry already knows is returned unchanged. A known alias resolves to its real id.
    Anything else — free text, a typo, an operation that genuinely does not exist — comes back UNTOUCHED,
    so `get()` still answers None and the caller still refuses. Canonicalizing is a translation, not a
    permission: it can rescue a turn the model named badly, and it can never conjure a capability.
    """
    cap = (capability or "").strip()
    if not cap or _BY_CAP.get(cap) is not None:
        return cap
    return _ALIASES.get(cap, cap)


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
