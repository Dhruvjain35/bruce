"""TurnTrace — one measured record per inbound turn, and nothing else.

THIS MODULE OPTIMIZES NOTHING. It exists to produce a baseline of the system exactly as it is, before
anything is made faster, because every latency argument this codebase has had so far was an intuition.
The 500ms triage gate was aimed at the wrong boundary; the "cache the agent" fix was found only because
someone measured a 2.8s TLS handshake. A number nobody collected is a number people invent.

TWO CLOCKS, ON PURPOSE. Durations come from `time.perf_counter()`, which is monotonic and unaffected by
NTP stepping or a leap second; correlation timestamps come from the wall clock, because a trace nobody
can line up against a log or a provider's own record is not much use in an incident. Mixing them —
subtracting two wall-clock readings to get a duration — is how a negative latency ends up in a
dashboard.

A MISSING STAGE IS NULL, AND SAYS WHY. The tempting shortcut is to default an unset timestamp to zero,
or to the turn's start; both produce a duration of 0ms that reads as "instantaneous" rather than "did not
happen". A tool-free conversation must not report a tool stage that took no time — it must report no tool
stage, with `tool` in `absent_stages`. Percentiles computed over fabricated zeros are worse than no
percentiles.

NO MESSAGE CONTENT, EVER. Everything persisted here is an id, a label, a count or a timestamp. A latency
record that quotes the student is a privacy incident wearing an engineering label, and it is the kind of
thing that gets added later "just for debugging" — so `assert_content_free` exists and the correctness
suite runs it over real traces.

TRACING NEVER BREAKS A TURN, AND NEVER HIDES THAT IT FAILED. Every mutating call is guarded, and every
guard increments `TRACING_FAILURES`. A tracing layer that swallows its own errors reports a suspiciously
healthy system.
"""

from __future__ import annotations

import logging
import os
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("bruce.turn_trace")   # CONTENT-FREE: ids, stages, durations

# Counted rather than raised. A tracing bug must be visible in telemetry without ever costing a student
# their reply — see `_guard`.
TRACING_FAILURES = 0

# The stages a trace can record, in the order they ACTUALLY occur in the current pipeline. The ORDER is
# the contract the monotonicity test checks: a trace whose `model_finished` precedes its `model_started`
# is a bug in the instrumentation, not a fast model.
#
# NOTE THE ROUTER'S POSITION, because it was the first thing this instrumentation found. The obvious
# reading of the pipeline — trusted input, then state, then route on that state — is not what the code
# does. `fast_router.route` runs FIRST and builds its own TurnContext internally (capability snapshot,
# pending Decision, active AgentRun); the runtime then compiles a SECOND context for the reasoner, which
# reads capabilities, entities and agent runs again. So routing sits before the state stages here, and
# the duplicated reads are a real finding for #127 rather than a quirk of the ordering.
#
# This tuple is DESCRIPTIVE of the implementation as it stands, not prescriptive. When #127 makes the
# context reads concurrent, several of these stop being sequential and this order has to be revisited
# along with the monotonicity contract.
STAGES: tuple[str, ...] = (
    "received", "trusted_input_ready", "state_reads_started",
    "router_started", "router_finished",
    # Observed order inside the context compile: the capability snapshot is taken by the runtime, then
    # memory is retrieved at the top of `compile`, then the world/operational/entity blocks are awaited.
    "decisions_ready", "capabilities_ready", "memory_ready", "agent_runs_ready", "entities_ready",
    "conversation_state_ready",
    "model_started", "model_first_token", "model_finished",
    "tool_started", "tool_finished", "verification_finished", "response_generation_started",
    "response_ready", "relay_send_started", "relay_guid_received", "completed",
)

# Why a stage is absent. Explicit, because "null" alone cannot tell a turn that legitimately had no tool
# from a turn whose tool timing was dropped by a bug.
NOT_APPLICABLE = "not_applicable"     # this path genuinely has no such stage
NOT_REACHED = "not_reached"           # the turn ended before getting here
FAILED = "failed"                     # the stage ran and errored

_KILL = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """BRUCE_TURN_TRACE_OFF disables collection. Present because instrumentation on the hot path of every
    turn is exactly the kind of thing that must be switchable without a deploy — not because it is
    expected to cost anything (`test_tracing_overhead_is_negligible` measures it)."""
    return os.environ.get("BRUCE_TURN_TRACE_OFF", "").strip().lower() not in _KILL


@dataclass
class TurnTrace:
    """One inbound turn, measured. Mutable by design — a trace is filled in as the turn happens — and
    read-only once `finish` has been called."""

    trace_id: str
    user_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None

    # --- dimensions
    execution_path: str | None = None      # deterministic_conversation | semantic_conversation | ...
    cold: bool = False
    model_role: str | None = None          # triage | reasoner | composer
    provider: str | None = None            # openai | google_calendar | gmail | relay
    cache_state: str | None = None         # memory_hit | memory_miss | n/a
    timeout_stage: str | None = None
    fallback_reason: str | None = None
    error_class: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    # --- timing
    started_wall: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _t0: float = field(default_factory=time.perf_counter, repr=False)
    marks: dict[str, float] = field(default_factory=dict, repr=False)      # stage -> ms since t0
    wall: dict[str, datetime] = field(default_factory=dict, repr=False)
    absent_stages: dict[str, str] = field(default_factory=dict)
    parent_trace_id: str | None = None     # a wake or callback links back to the turn that planned it
    attempt: int = 1                       # a retry is a separate trace with the same parent

    def mark(self, stage: str) -> None:
        """Record a stage. Idempotent-by-first-write: a stage marked twice keeps the FIRST time, because
        a retry loop that re-marks `model_started` would otherwise report only the last attempt and make
        a slow turn look fast."""
        if stage not in STAGES:
            _fail(f"unknown stage {stage}")
            return
        if stage in self.marks:
            return
        self.marks[stage] = (time.perf_counter() - self._t0) * 1000.0
        self.wall[stage] = datetime.now(timezone.utc)

    def absent(self, stage: str, reason: str) -> None:
        """Say why a stage did not happen. The alternative — leaving it null and silent — makes a
        measurement gap indistinguishable from a path that legitimately skips it."""
        if stage in self.marks:
            return
        self.absent_stages[stage] = reason

    def at(self, stage: str) -> float | None:
        return self.marks.get(stage)

    def duration(self, start: str, end: str) -> float | None:
        """Milliseconds between two stages, or None if either is missing. Never 0 for a stage that did
        not happen — that is the whole point of returning None."""
        a, b = self.marks.get(start), self.marks.get(end)
        return None if a is None or b is None else b - a

    @property
    def total_ms(self) -> float | None:
        return self.marks.get("completed")

    def stage_breakdown(self) -> dict[str, float]:
        """Consecutive gaps between the stages that were actually recorded. Only observed stages appear,
        so the percentages below sum over real time rather than over a template."""
        seen = [(s, self.marks[s]) for s in STAGES if s in self.marks]
        return {b[0]: b[1] - a[1] for a, b in zip(seen, seen[1:])}

    def slowest_stage(self) -> tuple[str, float] | None:
        breakdown = self.stage_breakdown()
        return max(breakdown.items(), key=lambda kv: kv[1]) if breakdown else None

    def finish(self, *, error_class: str | None = None) -> "TurnTrace":
        """Close the trace. Called on EVERY exit including the error paths — a turn that fails is exactly
        the turn whose timing matters, and a trace that only completes on success measures a system that
        never has incidents."""
        if error_class:
            self.error_class = error_class
        self.mark("completed")
        for stage in STAGES:
            if stage not in self.marks and stage not in self.absent_stages:
                self.absent_stages[stage] = NOT_REACHED
        return self

    def as_dict(self) -> dict[str, Any]:
        """The persisted shape. Ids, labels, counts and numbers — nothing a person wrote."""
        return {
            "trace_id": self.trace_id, "parent_trace_id": self.parent_trace_id,
            "user_id": self.user_id, "conversation_id": self.conversation_id,
            "message_id": self.message_id, "attempt": self.attempt,
            "execution_path": self.execution_path, "cold": self.cold,
            "model_role": self.model_role, "provider": self.provider,
            "cache_state": self.cache_state, "timeout_stage": self.timeout_stage,
            "fallback_reason": self.fallback_reason, "error_class": self.error_class,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "started_at": self.started_wall.isoformat(),
            "total_ms": self.total_ms,
            "marks_ms": dict(self.marks),
            "wall": {k: v.isoformat() for k, v in self.wall.items()},
            "absent_stages": dict(self.absent_stages),
        }


# --- process state ---------------------------------------------------------------------------------------
# `cold` is a property of the PROCESS, not of the turn: the first turn after a deploy pays import, TLS and
# pool warm-up that no later turn pays. Reporting them together is how a p95 hides a 4.5s cold start,
# which is the specific thing the acceleration program forbids.

_SEEN_A_TURN = False


def _is_cold() -> bool:
    global _SEEN_A_TURN
    cold = not _SEEN_A_TURN
    _SEEN_A_TURN = True
    return cold


def reset_process_state() -> None:
    """Tests only — makes the next trace read as cold again."""
    global _SEEN_A_TURN
    _SEEN_A_TURN = False


def _fail(what: str) -> None:
    global TRACING_FAILURES
    TRACING_FAILURES += 1
    log.warning("turn_trace_failure %s", what)


def start(*, user_id=None, conversation_id: str | None = None, message_id: str | None = None,
          parent_trace_id: str | None = None, attempt: int = 1) -> "TurnTrace":
    """Open a trace for one inbound message. Exactly one root trace per message; a provider wake or an
    AgentRun continuation opens its own trace carrying `parent_trace_id`, because it is a separate
    latency event that must not be averaged into the turn that caused it."""
    t = TurnTrace(trace_id=str(_uuid.uuid4()), user_id=str(user_id) if user_id else None,
                  conversation_id=conversation_id, message_id=message_id,
                  parent_trace_id=parent_trace_id, attempt=attempt)
    t.cold = _is_cold()
    t.mark("received")
    return t


# --- the collection sink ---------------------------------------------------------------------------------
# In-process and bounded. Persisting traces is a separate decision with its own retention question; this
# PR measures, and a ring buffer is what the benchmark reads. `record` is the seam a durable sink
# replaces.

MAX_RETAINED = 2000
_RECENT: list[dict] = []


def record(trace: TurnTrace) -> None:
    if not enabled():
        return
    try:
        payload = trace.as_dict()
        _RECENT.append(payload)
        if len(_RECENT) > MAX_RETAINED:
            del _RECENT[:len(_RECENT) - MAX_RETAINED]
        log.info("turn_trace id=%s path=%s cold=%s total_ms=%s slowest=%s err=%s",
                 trace.trace_id, trace.execution_path, trace.cold,
                 None if trace.total_ms is None else round(trace.total_ms, 1),
                 trace.slowest_stage(), trace.error_class)
    except Exception:
        _fail("record")


def recent(*, path: str | None = None, cold: bool | None = None) -> list[dict]:
    return [t for t in _RECENT
            if (path is None or t["execution_path"] == path)
            and (cold is None or t["cold"] == cold)]


def clear() -> None:
    _RECENT.clear()


# --- guards ----------------------------------------------------------------------------------------------

def guard(trace: "TurnTrace | None", stage: str) -> None:
    """Mark a stage without ever letting a tracing bug reach the student. Counted, not swallowed."""
    if trace is None:
        return
    try:
        trace.mark(stage)
    except Exception:
        _fail(f"mark {stage}")


def note(trace: "TurnTrace | None", **fields) -> None:
    if trace is None:
        return
    try:
        for k, v in fields.items():
            if hasattr(trace, k):
                setattr(trace, k, v)
    except Exception:
        _fail("note")


# --- correctness helpers used by the tests and by the benchmark -------------------------------------------

def monotonic_violations(trace: TurnTrace) -> list[tuple[str, str]]:
    """Pairs of recorded stages that occur out of order. Empty is the only acceptable answer: a trace
    that is internally inconsistent cannot be reasoned about, and every percentile built on it is
    suspect."""
    seen = [(s, trace.marks[s]) for s in STAGES if s in trace.marks]
    return [(a[0], b[0]) for a, b in zip(seen, seen[1:]) if b[1] < a[1]]


_CONTENT_KEYS = ("text", "body", "message", "content", "snippet", "subject", "value", "prompt")


def assert_content_free(payload: dict) -> None:
    """A latency record that quotes the student is a privacy incident wearing an engineering label.

    Identifier fields are exempt by suffix, not by name: `message_id` correlates a trace to a message
    without carrying a word of it, and an exemption list of specific names would need editing every time
    a field is added — which is exactly when the check stops being run.
    """
    for key in payload:
        lowered = key.lower()
        if lowered.endswith("_id") or lowered.endswith("_ids"):
            continue
        if any(c in lowered for c in _CONTENT_KEYS):
            raise AssertionError(f"trace field {key!r} could carry message content")


def percentiles(values: list[float]) -> dict[str, float]:
    """p50/p95/p99/max over whatever was actually observed. Deliberately no interpolation and no
    smoothing: with the sample sizes a benchmark produces, a smoothed p99 is a guess with a decimal
    point on it."""
    if not values:
        return {"n": 0}
    v = sorted(values)
    def at(q: float) -> float:
        return v[min(len(v) - 1, max(0, int(round(q * len(v))) - 1))]
    return {"n": len(v), "p50": at(0.50), "p95": at(0.95), "p99": at(0.99), "max": v[-1]}
