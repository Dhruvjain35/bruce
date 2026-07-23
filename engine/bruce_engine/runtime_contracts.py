"""Frozen runtime contracts (R1) — the provider-neutral vocabulary of the Bruce general agent runtime.

These are the shared types every worktree imports read-only: the model proposes a GoalSpec + a plan of
NextActions; deterministic policy validates them; the executor runs a validated NextAction and returns a
ToolResult; time is normalized to a TemporalSpec. Calendar is the first tool; the same contracts serve
Gmail/Drive/Canvas/… later. Deliberately dependency-light dataclasses — no DB, no provider imports — so
BRAIN/WORLD/TIME/HANDS/EVALS can all import them without a cycle.

This module is INTEGRATION-OWNER-OWNED and FROZEN once merged: adding a field is fine, changing/removing
one is a coordinated migration across worktrees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GoalAction(str, Enum):
    answer = "answer"
    remember = "remember"
    create = "create"
    update = "update"
    delete = "delete"
    search = "search"
    send = "send"
    submit = "submit"
    monitor = "monitor"
    follow_up = "follow_up"
    verify = "verify"
    repair = "repair"          # correct a prior operation ("not today, i said 4 days from now")


class Risk(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


@dataclass(frozen=True)
class TemporalSpec:
    """A normalized moment/span. `start`/`end` are ISO strings (date-only when all_day, else naive local
    datetime); `timezone` is the IANA zone the wall-clock is in. Carries its own evidence + ambiguity so
    a higher layer can ask exactly one question instead of guessing."""
    start: str
    end: str
    timezone: str | None = None
    all_day: bool = False
    recurrence: str | None = None            # RRULE when recurring, else None
    original_expression: str | None = None
    confidence: float = 1.0
    ambiguity: tuple[str, ...] = ()          # e.g. ("am_pm",), ("timezone",), ("duration",)
    missing_fields: tuple[str, ...] = ()
    resolution_evidence: str | None = None   # how it was resolved (offset-from-send-time, weekday, iso…)


@dataclass(frozen=True)
class GoalSpec:
    """What the model believes the user wants — the input to planning. The model PROPOSES this; a
    deterministic policy authorizes any mutating action before it executes."""
    action: GoalAction
    domain: str                              # "calendar", "email", "drive", …
    entity: str | None = None                # "event", "thread", "file", …
    target_entity_id: str | None = None      # for update/delete/repair: the existing entity
    title: str | None = None
    desired_outcome: str | None = None
    constraints: dict = field(default_factory=dict)
    temporal: TemporalSpec | None = None
    destination_provider: str | None = None
    destination_account: str | None = None
    risk: Risk = Risk.low
    reversible: bool = True
    confidence: float = 0.5
    missing_information: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    source_attachment_ids: tuple[str, ...] = ()


class ActionType(str, Enum):
    respond = "respond"
    gather_evidence = "gather_evidence"
    request_decision = "request_decision"
    call_tool = "call_tool"
    verify_result = "verify_result"
    repair = "repair"
    wait = "wait"
    complete = "complete"
    fail = "fail"


@dataclass(frozen=True)
class NextAction:
    """One step the planner proposes. Deterministic policy validates capability/scope/args/approval/
    idempotency/risk BEFORE the executor runs a call_tool. The LLM never self-authorizes a mutation."""
    type: ActionType
    capability: str | None = None            # e.g. "calendar.create_event"
    provider: str | None = None              # e.g. "google_calendar"
    operation: str | None = None             # e.g. "create_event" | "update_event" | "delete_event"
    target_entity_id: str | None = None
    arguments: dict = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    required_decision_id: str | None = None
    idempotency_key: str | None = None
    expected_result: dict | None = None
    verification_method: str | None = None   # e.g. "provider_readback"
    risk: Risk = Risk.low
    reversible: bool = True
    confidence: float = 0.5


class ToolOutcome(str, Enum):
    ok = "ok"
    already_exists = "already_exists"        # idempotent retry hit the same entity
    not_found = "not_found"
    unauthorized = "unauthorized"            # reconnect needed
    insufficient_scope = "insufficient_scope"
    rate_limited = "rate_limited"
    provider_error = "provider_error"
    verification_failed = "verification_failed"
    verification_inconclusive = "verification_inconclusive"


@dataclass(frozen=True)
class ToolResult:
    """What the executor observed. `verified` is set ONLY by an independent read-back — never on the
    strength of a write. Carries the provider entity id + normalized read-back so the planner can
    verify, repair, or report from state (never fabricate 'done')."""
    outcome: ToolOutcome
    capability: str
    provider: str
    operation: str
    verified: bool = False
    provider_entity_id: str | None = None
    read_back: dict | None = None
    evidence: dict = field(default_factory=dict)
    reason: str = ""
