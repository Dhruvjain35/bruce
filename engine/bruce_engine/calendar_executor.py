"""Calendar CapabilityExecutors — the calendar domain's plug into the general AgentRun loop.

`CalendarMutationExecutor` builds a NextAction for a mutation and, on execute, delegates to the
ALREADY-VERIFIED calendar_tools (update_event / delete_event) — which do the provider write +
independent read-back + _matches and return a ToolResult. The executor reimplements NONE of that
verification, so the live-verified behavior is unchanged; the loop just gains a durable run + the frozen
contracts around the same trusted I/O. Adding create is another executor here, never a new branch in the
runtime.

`CalendarCreateExecutor` is that second executor, and it exists because of a hole with a name.
`goal_slots` declares a full slot set for `calendar.create_event` and `goal_handler._EXECUTORS` had no
row for it, so `goal_handler` DECLINED every calendar turn with `NO_EXECUTOR` and a `schedule_event` goal
could never exist. The acceptance suite calls that D3: "one goal id across a move", "a replacement
Decision", "attendees retained" all presuppose a calendar goal, and none was ever created. The fix is one
executor and one registry row — deliberately NOT a calendar branch in the runtime, and deliberately not a
second execution path:

    Goal -> Decision -> AuthorizationEvidence -> MutationGateway -> ExecutionAttempt
         -> calendar_adapter.execute_and_verify (create-once, fetch back, compare) -> Receipt -> verified

Everything after `Goal` is code that already existed and is already tested. `execute_and_verify` is the
same function `calendar_schedule.schedule_event` uses for a flyer, including the deterministic event id
(Google 409s a duplicate insert, so exactly-once is enforced REMOTELY rather than by local state a crash
could lose) and including `execution_gate.require` at the adapter. This file adds no verification of its
own except the one comparison the shared code cannot make — attendees, which `models.CalendarEvent`
cannot express.

WHAT THIS EXECUTOR REFUSES TO DO, and why the refusal is the honest answer. A student who says "invite
mr. kim" and gets an event with no invitation on it has been told a lie by a green checkmark. Attendees
and a description can reach Google only if BOTH `execution_gate.calendar_create_args` (the canonical shape
consent is fingerprinted over) and `models.CalendarEvent` (what the adapter turns into a request body)
have somewhere to put them, and today neither does. So a create carrying either one is refused BEFORE any
provider call, with a reason that names the field. See `undeliverable()`: the rule is derived from those
two modules rather than written down here, so the day they gain the field this file starts delivering it
without a line changing.

Nothing here logs a title, an address, a location or a description. Ids, outcomes, field NAMES and counts.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from . import calendar_adapter, calendar_tools, execution_gate, oauth_google, tool_registry
from .models import CalendarEvent
from .runtime_contracts import ActionType, NextAction, Risk, ToolOutcome, ToolResult

log = logging.getLogger("bruce.calendar")   # CONTENT-FREE: ids, outcomes, field names, counts

_PROVIDER = "google_calendar"


class CalendarMutationExecutor:
    """Drives a delete OR an update/repair on one resolved calendar entity through the verified tools."""

    domain = "calendar"

    def __init__(self, kind: str, entity: dict, *, new_start: str | None = None,
                 new_end: str | None = None, new_timezone: str | None = None, adapter=None) -> None:
        if kind not in ("delete", "update", "repair"):
            raise ValueError(f"unsupported mutation kind: {kind}")
        self.kind = kind
        self.entity = entity
        self.new_start = new_start
        self.new_end = new_end
        self.new_timezone = new_timezone
        self._adapter = adapter
        self._is_delete = kind == "delete"
        self.capability = "calendar.delete_event" if self._is_delete else "calendar.update_event"
        self._operation = "delete_event" if self._is_delete else "update_event"

    def goal(self) -> dict:
        # provider-neutral summary the AgentRun/world model can read back — title + what changed, no secrets
        g: dict = {"action": self.kind, "entity_id": self.entity.get("id"),
                   "title": self.entity.get("title"), "desired_outcome": f"{self.kind} {self.entity.get('title', 'event')}"}
        if not self._is_delete:
            g["new_start"], g["new_end"] = self.new_start, self.new_end
        return g

    gate_provider = "google_calendar"

    @property
    def gate_operation(self) -> str:
        return self._operation

    def gate_arguments(self) -> dict:
        """The canonical arguments the execution gate binds an authorization to. Distinct from
        `build_action().arguments`, which carries the CANONICAL entity id for the run record; the gate
        binds to the PROVIDER entity id, because that is the thing that actually gets changed."""
        from . import execution_gate
        eid = str(self.entity.get("provider_event_id") or "")
        if self._is_delete:
            return execution_gate.calendar_delete_args(provider_event_id=eid)
        return execution_gate.calendar_update_args(
            provider_event_id=eid, new_start=self.new_start, new_end=self.new_end,
            new_timezone=self.new_timezone)

    def build_action(self) -> NextAction:
        args: dict = {} if self._is_delete else {
            "new_start": self.new_start, "new_end": self.new_end, "new_timezone": self.new_timezone}
        return NextAction(
            type=ActionType.call_tool, capability=self.capability, provider=_PROVIDER,
            operation=self._operation, target_entity_id=str(self.entity.get("id")) if self.entity.get("id") else None,
            arguments=args, verification_method="provider_readback",
            risk=Risk.medium if not self._is_delete else Risk.high, reversible=not self._is_delete)

    async def execute(self, user_id: UUID) -> ToolResult:
        if self._is_delete:
            return await calendar_tools.delete_event(user_id, self.entity, adapter=self._adapter)
        return await calendar_tools.update_event(
            user_id, self.entity, new_start=self.new_start, new_end=self.new_end,
            new_timezone=self.new_timezone, adapter=self._adapter)


# =========================================================================================================
# calendar.create_event — the executor `goal_handler` had no row for
# =========================================================================================================

_CREATE = "calendar.create_event"
_ALL_DAY_LEN = 10                 # "YYYY-MM-DD" — Google's own marker for an all-day event
_DEFAULT_MINUTES = 60             # a timed event with no stated end, same convention as calendar_schedule


def _clean(value: Any) -> str | None:
    """A trimmed string, or None for anything that carries no information. `""` and `"  "` are the same
    absence, and treating them differently is how a blank title reaches a provider."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _moment(value: str) -> tuple[_dt.datetime, bool] | None:
    """(the moment, is it a whole day) for an ISO value, or None if it is not a moment at all.

    `goal_handler.resolve_temporal` leaves a when-phrase it could not read EXACTLY as the student typed
    it, on purpose — asking one question beats inventing a time. So "next tuesdayish" can still be sitting
    in the `start` slot when an executor is built, and a create that shipped that string to Google would
    either 400 or land an event on a date nobody chose. Returning None here is what turns that into a
    question instead of an event.
    """
    try:
        return _dt.datetime.fromisoformat(value), len(value) == _ALL_DAY_LEN
    except ValueError:
        return None


def _plus_days(iso_date: str, days: int) -> str:
    return (_dt.date.fromisoformat(iso_date) + _dt.timedelta(days=days)).isoformat()


def normalize_span(start: str, end: str | None, timezone: str | None, *,
                   all_day: bool | None = None) -> tuple[str, str, str | None]:
    """(start, end, timezone) in the exact shape the adapter and the verifier both expect.

    Three normalizations, each of which is a bug somewhere if it is skipped:

      * An all-day event carries DATES and no zone. `calendar_adapter._to_google_body` sends `date` for a
        ten-character value and `dateTime` otherwise, and Google 400s if they are mixed — so `all_day=True`
        beside a datetime start is resolved to the date rather than sent as-is.
      * An all-day end is EXCLUSIVE (Google's rule, and what `temporal.resolve` already returns). An end
        that does not lie past the start names one whole day.
      * A timed event with no usable end gets one hour, which is what `calendar_schedule` has always done
        for the same situation. `end` is declared optional in the tool schema, so `goal_runtime` will never
        ask for it — refusing here would strand a goal AFTER the student said yes, which is the exchange
        this workstream exists to delete. A zero-length event is not a smaller version of the right answer.

    Raises ValueError when the start is not a moment; see `_moment`.
    """
    parsed = _moment(start)
    if parsed is None:
        raise ValueError("start is not a resolvable moment")
    started, is_day = parsed
    finished = _moment(end) if end else None
    if all_day is True or is_day:
        day = started.date().isoformat()
        end_day = finished[0].date().isoformat() if finished is not None else ""
        return day, (end_day if end_day > day else _plus_days(day, 1)), None
    ended = finished[0] if finished is not None else None
    if ended is not None and (ended.tzinfo is None) != (started.tzinfo is None):
        # A naive end beside an offset-aware start (or the reverse) is not comparable, and an end nobody
        # can order against its own start is not an end. Derive one rather than raise a TypeError from
        # inside an executor the student is waiting on.
        ended = None
    if ended is None or ended <= started:
        ended = started + _dt.timedelta(minutes=_DEFAULT_MINUTES)
    return start, ended.isoformat(), timezone


def normalize_attendees(value: Any) -> tuple[str, ...]:
    """Addresses, lowercased and de-duplicated, in a stable order.

    Deliberately the same rule `authorization_evidence._normalize_value` applies to a recipient list —
    inviting two people in the other order is the same act — so a normalized list here and the
    fingerprint the student's consent is bound to can never disagree about whether anything changed.
    Accepts both a list of addresses and Google's own read-back shape (`[{"email": ...}, ...]`), because
    this same function normalizes what was ASKED FOR and what came BACK, and a comparison between two
    different normalizations proves nothing.
    """
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    out: set[str] = set()
    for item in items:
        address = _clean(item.get("email")) if isinstance(item, dict) else _clean(item)
        if address:
            out.add(address.lower())
    return tuple(sorted(out))


# What a create can actually carry all the way to Google, derived from the two modules that decide it:
# the canonical argument shape consent is fingerprinted over, and the event model the adapter serializes.
# Intersected rather than listed, so this cannot drift from either one.
_GATE_FIELDS: frozenset[str] = frozenset(
    execution_gate.calendar_create_args(title="t", start="s", end=None, timezone=None, location=None))
DELIVERABLE: frozenset[str] = _GATE_FIELDS & frozenset(CalendarEvent.model_fields)


def undeliverable(fields: dict) -> tuple[str, ...]:
    """The supplied fields this executor cannot honestly carry to the provider.

    Either reason alone is disqualifying, and both are true of `attendees` today:

      * `execution_gate.calendar_create_args` has no place for it, so NO authorization could ever name it.
        Sending it anyway would mean performing part of an act the student's yes was never bound to —
        `execution_gate.require` fingerprints the arguments that actually reach the adapter precisely so
        that a field added after the decision cannot ride along.
      * `models.CalendarEvent` has no such field, so `calendar_adapter._to_google_body` would drop it
        silently on the way out and the student would get an event with nobody invited to it.

    Empty values are not refused: a goal that carries `attendees=[]` is asking for nothing.
    """
    return tuple(name for name, value in fields.items()
                 if name not in DELIVERABLE and value not in (None, "", [], (), {}))


def verify_attendees(expected: Any, read_back: dict | None) -> tuple[bool, str]:
    """Did the people the student named actually land on the event the provider now holds?

    `calendar_adapter._matches` already compares title, start, end, location and the connected account —
    everything `CalendarEvent` can express — and this executor delegates all of it rather than restating
    it. Attendees are the one field it cannot compare, so they are compared here and only here.

    An absent attendee list is NOT read as a match. A provider that returned no attendees has not proven
    the invitations exist, and "verified" is a claim about evidence, not about the absence of bad news.
    """
    want = normalize_attendees(expected)
    if not want:
        return True, "no attendees were requested"
    got = normalize_attendees((read_back or {}).get("attendees"))
    if got == want:
        return True, "read-back confirmed every attendee"
    missing = len([a for a in want if a not in got])
    if missing:
        return False, f"read-back is missing {missing} of {len(want)} attendees — NOT verified"
    return False, "read-back carries attendees that were never approved — NOT verified"


class CalendarCreateExecutor:
    """Create ONE calendar event, exactly once, and only call it done once it has been read back.

    Built from `GoalView.tool_arguments()`, whose keys are the tool registry's own argument names, so
    `goal_handler._EXECUTORS` needs a factory row and no translation layer. `idempotency_key` is the
    caller's (`goal:{run_id}:{capability}` from the goal lane) and is REQUIRED: the deterministic provider
    event id is derived from it, and that id is the whole exactly-once guarantee. A second confirmation —
    a double tap, a redelivered webhook, two turns racing — derives the same id, Google answers 409, and
    `execute_and_verify` falls through to the read-back instead of creating a second event.
    """

    domain = "calendar"
    capability = _CREATE
    # A create is ONE provider operation and the gate names it the same way at both ends: this method and
    # `execute_and_verify`'s own `execution_gate.require` both build their arguments from `self._event`,
    # so the authorization that is opened and the arguments that reach the socket cannot describe
    # different events.
    gate_provider = _PROVIDER
    gate_operation = "create_event"

    def __init__(self, *, title: str, start: str, end: str | None = None, timezone: str | None = None,
                 all_day: bool | None = None, location: str | None = None, attendees: Any = None,
                 description: str | None = None, idempotency_key: str,
                 source_message_id: str | None = None, adapter=None) -> None:
        if not idempotency_key:
            # No key means no stable event id, which means a retry creates a SECOND event on a real
            # student's calendar. Fail loudly at construction rather than quietly at the provider.
            raise ValueError("calendar create requires an idempotency_key")
        clean_title, clean_start = _clean(title), _clean(start)
        if not clean_title or not clean_start:
            raise ValueError("calendar create requires a title and a start")
        start_value, end_value, zone = normalize_span(
            clean_start, _clean(end), _clean(timezone), all_day=all_day)
        self.attendees = normalize_attendees(attendees)
        self.description = _clean(description)
        # Ordered so a refusal names fields in a stable order; see `undeliverable`.
        self._extras: dict = {"attendees": list(self.attendees), "description": self.description}
        self._event = CalendarEvent(title=clean_title, start=start_value, end=end_value, timezone=zone,
                                    location=_clean(location), tentative=False)
        self._key = idempotency_key
        # The entity record wants a message this event came from. A goal-lane create has a run rather than
        # a single message, so the durable key stands in — never a fabricated message id.
        self._source_message_id = source_message_id or idempotency_key
        self._adapter = adapter
        # `execute_and_verify` keys the deterministic event id on a mission id. The goal lane has no
        # mission; the durable unit of work is the AgentRun the idempotency key names, so it is folded
        # into a stable UUID here. Derived, never random: a fresh uuid4 per attempt would defeat the one
        # guarantee this key exists to provide.
        self._origin = uuid5(NAMESPACE_URL, f"bruce:calendar-create:{idempotency_key}")

    # --- what the run record and the gate see -------------------------------------------------------------

    def goal(self) -> dict:
        """A provider-neutral summary for the AgentRun. Attendees appear as a COUNT: a run record is read
        by anyone auditing this student's account, and an invitee list is message content."""
        e = self._event
        return {"action": "create", "domain": "calendar", "entity": "event", "title": e.title,
                "start": e.start, "end": e.end, "timezone": e.timezone,
                "attendee_count": len(self.attendees),
                "desired_outcome": f"create {e.title}"}

    def gate_arguments(self) -> dict:
        """The canonical arguments consent binds to — built from the SAME `CalendarEvent` that
        `execute_and_verify` hands to `execution_gate.require`, so the two cannot diverge."""
        e = self._event
        return execution_gate.calendar_create_args(
            title=e.title, start=e.start, end=e.end, timezone=e.timezone, location=e.location)

    def build_action(self) -> NextAction:
        spec = tool_registry.get(_CREATE)
        return NextAction(
            type=ActionType.call_tool, capability=_CREATE, provider=_PROVIDER, operation="create_event",
            target_entity_id=None, arguments=self.gate_arguments(),
            verification_method="provider_readback", risk=Risk.medium,
            reversible=bool(spec.reversible) if spec is not None else False)

    # --- the provider call ---------------------------------------------------------------------------------

    def _result(self, outcome: ToolOutcome, **fields) -> ToolResult:
        return ToolResult(outcome, _CREATE, _PROVIDER, "create_event", **fields)

    async def execute(self, user_id: UUID) -> ToolResult:
        """Refuse what cannot be delivered, bind the account, create once, read back, verify.

        Ordering is the point. Everything that can be known WITHOUT touching Google is decided before
        Google is touched, so a create that was always going to be wrong costs zero provider calls and
        leaves nothing behind on the student's calendar to clean up.
        """
        blocked = undeliverable(self._extras)
        if blocked:
            log.info("calendar_create_refused fields=%s", ",".join(blocked))
            return self._result(
                ToolOutcome.provider_error,
                reason=f"nothing was created: this create cannot carry {', '.join(blocked)}")

        # The SAME connection rule `calendar_tools` applies to an update or a delete. A create that
        # guessed an account would put a student's event on someone else's calendar.
        integration = await calendar_tools._bound(user_id)
        if integration is None:
            return self._result(ToolOutcome.unauthorized, reason="google_calendar_not_connected")
        calendar_id = integration.selected_calendar_id or "primary"
        expected_account = integration.provider_account_id
        adapter = calendar_tools._adapter(user_id, calendar_id, None, self._adapter)

        try:
            # THE existing verified I/O: deterministic id -> insert (409 == already executed) -> the
            # adapter's own independent read-back -> `_matches`. No part of it is re-implemented here, and
            # `execution_gate.require` inside it is the backstop that catches anything rewritten between
            # the gateway's decision and the socket.
            verification = await calendar_adapter.execute_and_verify(
                adapter, self._event, user_id=user_id, mission_id=self._origin,
                source_message_id=self._source_message_id, expected_account=expected_account)
        except calendar_adapter.InsufficientScope as exc:
            return self._result(ToolOutcome.insufficient_scope, reason=str(exc)[:200])
        except calendar_adapter.CalendarAuthError as exc:
            return self._result(ToolOutcome.unauthorized, reason=str(exc)[:200])
        except calendar_adapter.RateLimited as exc:
            return self._result(ToolOutcome.rate_limited, reason=str(exc)[:200])
        except calendar_adapter.CalendarError as exc:
            return self._result(ToolOutcome.provider_error, reason=str(exc)[:200])
        return await self._settle(user_id, verification, calendar_id=calendar_id,
                                  expected_account=expected_account)

    async def _settle(self, user_id: UUID, verification, *, calendar_id: str,
                      expected_account: str | None) -> ToolResult:
        """Turn the read-back into a ToolResult. `verified=True` is COPIED from the evidence, never
        decided here — and the attendee comparison can only ever take it away."""
        attendees_ok, attendee_reason = verify_attendees(self.attendees, verification.read_back)
        if not verification.verified or not attendees_ok:
            # Absent means unproven, mismatched means disproven, and the two need different repairs —
            # the same split `calendar_tools.update_event` makes. Both land the run in `failed`
            # (`agent_loop._status_for`), which is the honest place for a create nobody can prove.
            outcome = (ToolOutcome.verification_inconclusive if verification.read_back is None
                       else ToolOutcome.verification_failed)
            reason = verification.reason if not verification.verified else attendee_reason
            log.info("calendar_create_unverified outcome=%s", outcome.value)
            return self._result(outcome, provider_entity_id=verification.event_id,
                                read_back=verification.read_back, reason=reason[:200])

        read_account = (verification.read_back or {}).get("account")
        if not expected_account and read_account:
            # A `calendar.events`-only connection cannot be asked who it belongs to before a write, so the
            # authoritative created-event record is where the account is learned. Same rule, same source
            # of truth as `calendar_schedule`.
            try:
                await oauth_google.backfill_account(user_id, read_account)
            except Exception:
                log.info("calendar_create_account_backfill_failed")
        try:
            # Best-effort, and it must stay that way: an entity-memory hiccup may never downgrade a
            # verified create into a reply that says nothing happened. Without this row the event exists
            # on Google but Bruce cannot later move or delete it.
            await calendar_tools.record_created(
                user_id, event=self._event, provider_event_id=verification.event_id,
                provider_account_id=read_account or expected_account, calendar_id=calendar_id,
                source_message_id=self._source_message_id)
        except Exception:
            log.info("calendar_create_entity_record_failed")

        log.info("calendar_create_verified")
        return self._result(ToolOutcome.ok, verified=True, provider_entity_id=verification.event_id,
                            read_back=verification.read_back, reason=verification.reason,
                            evidence={"calendar_id": calendar_id, "html_link": verification.html_link,
                                      "attendee_count": len(self.attendees)})
