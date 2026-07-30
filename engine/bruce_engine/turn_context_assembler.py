"""The ONE database read per turn — assembled into a single immutable snapshot before anything reasons.

WHAT WENT WRONG WITHOUT IT. A student asked Bruce to email one named address a thank-you note. Turn 1
gave the address and the substance; turn 2 asked who the recipient was; an inline reply of "this"
resolved nothing at all; the last of twenty-two turns said "i can't send messages for you" at
confidence 0.97 while `tool_broker` was answering ok=True for `gmail.send_message` in the same process.
Twenty-two turns, zero missions, zero agent_runs.

That is not four bugs. Every path built its OWN view of the turn: the reply path read one window of
chat, the routing path asked the MODEL whether a mission was needed, and capability truth stayed in the
broker where the replying model never saw it. Three views of one turn, and the student got the least
informed one. `turn_context.build()` fixed the SHAPE of the snapshot but is deliberately pure — it takes
already-fetched pieces. This module is the half that reads, and it is the half where the defect lived:
whoever fetches gets to decide what is true, so exactly one thing may fetch.

WHAT THIS MODULE GUARANTEES.

  * ONE snapshot, from ONE read, handed to every path. No handler re-derives context; if it needs
    something the snapshot lacks, the field belongs here, not in a second fetch at the call site.
  * OPERATIONS ARE LIVE TRUTH, not a list of what exists. Every capability is resolved through
    `tool_broker.availability` for THIS user right now, and only `ok` survives. An operation the student
    cannot currently run never reaches the model, and one they CAN run is never absent — which is the
    exact pair of errors in "i can't send messages for you".
  * NO STORE CAN DROP THE TURN. Every read is guarded: a raising store degrades ITS field to empty and
    is recorded in the `AssemblyReport`. A student who says "send it" while Postgres is having a bad
    minute still gets a reply, and the reply still knows what it does not know.
  * AN EMPTY OPERATION LIST IS NOT THE SAME AS A FAILED LOOK. `turn_context.render_for_model` prints
    "none — you have NO operations available this turn" when the list is empty; printed after a failed
    capability probe that sentence is a lie of exactly the kind this whole workstream exists to kill. So
    the report carries `capabilities_read`, `capabilities_were_read(ctx)` answers it for anyone holding
    only the context, and this module's `render_for_model` appends an explicit caveat instead of letting
    the model improvise around silence.

WHY THE REPORT RIDES ALONG. `TurnContext` is owned by `turn_context.py` and has no field for assembly
health — nor should it: it is a snapshot of the STUDENT'S world, not of our infrastructure. So the
report is returned beside the context (`assemble_with_report`) and attached to it (`report_for`), which
keeps the two from being separated by a call frame while leaving the snapshot's own shape alone.

CONTENT-FREE LOGGING. Field names, capability ids, run ids, exception TYPES. Never a value, never a
message, never a handle — a driver's exception message can quote the row that failed, and that row is
usually somebody's email address.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone as _timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import (agent_run_store, calendar_schedule, goal_slots, mission_kernel, schema, tool_broker,
               tool_registry, turn_context, turn_trace, world_state)
from .contract import TERMINAL_STATES
from .db import user_session
from .turn_context import TurnContext

log = logging.getLogger("bruce.turn_context_assembler")   # CONTENT-FREE: names, ids, counts, error types

# --- what a degraded field is called ---------------------------------------------------------------------
# The TurnContext field that came back empty, named as a constant so a caller can test for a specific
# degradation instead of matching prose. `ASSEMBLY` means the bug was in HERE, not in a store.

OPEN_RUNS = "open_runs"
GOAL_SLOTS = "goal_slots"
PENDING_DECISIONS = "pending_decisions"
AVAILABLE_OPERATIONS = "available_operations"
RECENT_TOOL_RESULTS = "recent_tool_results"
RELEVANT_MEMORIES = "relevant_memories"
PEOPLE = "people"
TIMEZONE = "timezone"
ASSEMBLY = "assembly"

# Terminal for the purpose of "is work in flight". This is `agent_run_store`'s set (the background-lease
# vocabulary: completed / failed / cancelled / dead_letter) UNION `contract.TERMINAL_STATES` (the machine
# vocabulary: succeeded / cancelled). One predicate, used for BOTH sources below, so the anchor read and
# the scan can never disagree about which runs are open. `succeeded` is the row `latest_active` does not
# yet exclude; a verified-and-finished send must not be presented as still in flight, or the model will
# say "i'm on it" about work that is already done.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "dead_letter"} | {s.value for s in TERMINAL_STATES})

# Bounds live here rather than in `turn_context.render_for_model`, which renders whatever it is given.
MAX_OPEN_RUNS = 8
MAX_TOOL_RESULTS = 5
MAX_SLOT_CHARS = 300

CAPABILITY_READ_FAILED_LINE = (
    "[CAPABILITY CHECK FAILED — the operations list above is INCOMPLETE because a capability lookup "
    "errored, not because you have no tools. Do not tell the student what you can or cannot do this "
    "turn; say the check failed.]")

_GUESSED_MARK = " (unconfirmed — the model guessed this, do not act on it without asking)"
_TRUNCATED_MARK = "... (truncated)"
_REPORT_ATTR = "_bruce_assembly_report"


# --- the health of one assembly -------------------------------------------------------------------------

@dataclass(frozen=True)
class Degradation:
    """One field that came back empty because a read failed. `error` is the exception TYPE only —
    a message from a database driver routinely quotes the row it choked on."""

    field: str
    source: str
    error: str


@dataclass(frozen=True)
class AssemblyReport:
    """Whether this snapshot is complete, and where it is not.

    `capabilities_read` is the load-bearing one. Everything else degrades to "we know of none", which is
    survivable; capabilities degrade to a list that LOOKS like an answer ("you have no tools") and would
    be read as one.
    """

    degraded: tuple[Degradation, ...] = ()
    capabilities_read: bool = False
    operations_probed: int = 0

    @property
    def ok(self) -> bool:
        return not self.degraded

    @property
    def degraded_fields(self) -> tuple[str, ...]:
        out: list[str] = []
        for d in self.degraded:
            if d.field not in out:
                out.append(d.field)
        return tuple(out)

    def failed(self, field: str) -> bool:
        return any(d.field == field for d in self.degraded)


@dataclass(frozen=True)
class Assembled:
    """The snapshot plus the honesty about how it was gathered. Returned together so no caller can hold
    one without being able to reach the other."""

    context: TurnContext
    report: AssemblyReport


class _Faults:
    """Mutable only while assembling; frozen into an `AssemblyReport` at the end."""

    __slots__ = ("items",)

    def __init__(self) -> None:
        self.items: list[Degradation] = []

    def record(self, field: str, source: str, exc: BaseException) -> None:
        # Recorded, not logged. A provider outage fails every capability probe at once, and nine warning
        # lines per turn is how a real signal gets tuned out; `assemble_with_report` emits ONE line naming
        # every affected field, and the per-source detail stays in the report for whoever asks.
        self.items.append(Degradation(field=field, source=source, error=type(exc).__name__))


async def _guard(faults: _Faults, field: str, source: str, fallback, awaitable):
    """Await one store. A failure costs THAT field and nothing else.

    Catches `Exception`, never `BaseException`: a cancelled turn must stay cancelled, and swallowing
    `CancelledError` here would leave the caller awaiting a snapshot of a turn that no longer exists.
    """
    try:
        return await awaitable
    except Exception as exc:
        faults.record(field, source, exc)
        return fallback


def _derive(faults: _Faults, field: str, fn, fallback):
    """Same isolation for the PURE shaping steps. JSONB is untyped by construction — a goal blob written
    by an older build, or by hand, must cost its own field and not the student's reply."""
    try:
        return fn()
    except Exception as exc:
        faults.record(field, "derive", exc)
        return fallback


# --- open runs -------------------------------------------------------------------------------------------

def _is_open(status: object) -> bool:
    """A run with an unknown or empty status counts as OPEN. Unknown is not finished, and treating it as
    finished would hide exactly the in-flight work the transcript never had."""
    return str(status or "").strip() not in _TERMINAL_STATUSES


def _run_row(r) -> dict:
    """Only the columns the snapshot uses, keyed exactly as `agent_run_store` keys them, so a row from
    the scan and a row from the store merge without a translation layer between them."""
    return {"id": str(r.id), "domain": r.domain, "status": r.status, "goal": r.goal,
            "blocked_reason": r.blocked_reason, "last_tool_result": r.last_tool_result,
            "verification_result": r.verification_result, "active_decision": r.active_decision}


async def _scan_open_runs(user_id: UUID) -> list[dict]:
    """Every non-terminal run, not just the newest.

    `latest_active` returns ONE row, and a student with a half-finished email and a half-finished event
    has two. Whichever one the single read happened to return, the other would have been invisible — and
    an invisible run is indistinguishable from no run, which is the state the model was hallucinating
    over. Ordered newest-first with the id as a tiebreak: two runs created in the same transaction share
    a `created_at`, and an unstable order would make the rendered prompt differ between two assemblies of
    the identical database.
    """
    async with user_session(user_id) as s:
        rows = (await s.execute(
            select(schema.AgentRun)
            .where(schema.AgentRun.user_id == user_id,
                   schema.AgentRun.status.notin_(tuple(sorted(_TERMINAL_STATUSES))))
            .order_by(schema.AgentRun.created_at.desc(), schema.AgentRun.id.desc())
            .limit(MAX_OPEN_RUNS))).scalars().all()
        return [_run_row(r) for r in rows]


async def _read_open_runs(user_id: UUID, faults: _Faults) -> list[dict]:
    """The store's authoritative answer, plus everything else still in flight.

    Two reads on purpose. `latest_active` is the API the rest of the runtime already trusts for "what is
    this student in the middle of"; the scan is what makes a SECOND goal visible. Keeping both means a
    failure in either still leaves the turn with the runs the other found, and the shared `_is_open`
    predicate keeps them from contradicting each other about what terminal means.
    """
    anchor = await _guard(faults, OPEN_RUNS, "agent_run_store.latest_active", None,
                          agent_run_store.latest_active(user_id, domain=None))
    scanned = await _guard(faults, OPEN_RUNS, "agent_runs.scan", [], _scan_open_runs(user_id))

    merged: dict[str, dict] = {}
    candidates = ([anchor] if isinstance(anchor, Mapping) else []) + list(scanned or [])
    for row in candidates:
        if not isinstance(row, Mapping):
            continue
        run_id = str(row.get("id") or "").strip()
        if not run_id or run_id in merged or not _is_open(row.get("status")):
            continue
        merged[run_id] = dict(row)
    return list(merged.values())[:MAX_OPEN_RUNS]


# --- goal slots ------------------------------------------------------------------------------------------

def _slot_text(value: object) -> str:
    """A slot value as the prompt should see it: whole when short, marked when cut.

    Truncation is safe HERE and only here — the flat map is for rendering, while execution reads the
    run's own typed slots through `goal_slots.tool_arguments`. A 4kB draft body would otherwise crowd
    out the operations list, and the operations list is the part that stops the denial.
    """
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value).strip()
    elif isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    return text if len(text) <= MAX_SLOT_CHARS else text[:MAX_SLOT_CHARS].rstrip() + _TRUNCATED_MARK


def _slot_view(runs: list[dict]) -> dict[str, str | None]:
    """Every open goal's slots, flattened for the prompt and namespaced by kind.

    Namespacing is not decoration. Two goals can want a `timezone` or a `title`, and a flat merge would
    let one goal's value answer the other goal's question — a subtler version of the same "asked for
    something it already had" failure. Two goals of the SAME kind (two emails in flight) are numbered for
    the same reason: blending one send's recipient into the other is how a note reaches the wrong person.
    `None` marks a REQUIRED slot that is still empty, which is what `render_for_model` turns into STILL
    MISSING; optional slots are omitted when empty so Bruce never interrogates a student about a tone no
    send is blocked on.

    This flat map is for the PROMPT. Execution reads the run's own typed slots through
    `goal_slots.tool_arguments`, which is why truncating and labelling here costs nothing real.
    """
    view: dict[str, str | None] = {}
    seen: dict[str, int] = {}
    for run in runs:
        goal = run.get("goal")
        kind, slots = goal_slots.from_goal_jsonb(goal if isinstance(goal, Mapping) else None)
        if kind is None:
            continue
        seen[kind.value] = nth = seen.get(kind.value, 0) + 1
        label = kind.value if nth == 1 else f"{kind.value}#{nth}"
        guessed = set(goal_slots.guessed_required(kind, slots))
        for name, sv in slots.items():
            if sv is None or not sv.filled:
                continue
            view[f"{label}.{name}"] = _slot_text(sv.value) + (_GUESSED_MARK if name in guessed else "")
        for name in goal_slots.missing_required(kind, slots):
            view.setdefault(f"{label}.{name}", None)
    return view


# --- decisions -------------------------------------------------------------------------------------------

def _first_text(src: Mapping, *names: str) -> str:
    for name in names:
        value = src.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _options(raw: object) -> list[str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    out: list[str] = []
    for item in raw:
        text = (_first_text(item, "label", "text", "title", "value")
                if isinstance(item, Mapping) else str(item))
        if text.strip():
            out.append(text.strip())
    return out


def _run_decisions(runs: list[dict]) -> list[dict]:
    """Decisions parked on an AgentRun.

    A decision with no id of its own falls back to the RUN id rather than being dropped. Dropping it
    would render "PENDING DECISIONS: none" over a run that is genuinely stopped waiting for the student,
    and "nothing is waiting on you" is the single most expensive thing this snapshot could get wrong.
    """
    out: list[dict] = []
    for run in runs:
        raw = run.get("active_decision")
        entries = raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else [raw]
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            decision_id = _first_text(entry, "decision_id", "id") or str(run.get("id") or "")
            if not decision_id:
                continue
            out.append({"decision_id": decision_id,
                        "question": _first_text(entry, "question", "prompt", "summary", "proposed_goal"),
                        "options": _options(entry.get("options") or entry.get("choices"))})
    return out


async def _read_mission_decisions(user_id: UUID, faults: _Faults) -> list[dict]:
    """Missions parked in `awaiting_approval` — the offer the student's next "ya" is answering.

    Both kernel queries are asked because they are deliberately disjoint (one filters on the calendar
    capability, the other on the rescue discriminator), and a pending decision that no path can see is
    how "add it to ur calendar?" becomes a question asked twice.
    """
    sources = (("mission_kernel.latest_pending_calendar_mission",
                mission_kernel.latest_pending_calendar_mission(user_id)),
               ("mission_kernel.latest_pending_rescue_proposal",
                mission_kernel.latest_pending_rescue_proposal(user_id)))
    out: list[dict] = []
    for source, awaitable in sources:
        row = await _guard(faults, PENDING_DECISIONS, source, None, awaitable)
        if not isinstance(row, Mapping):
            continue
        decision_id = str(row.get("mission_id") or "").strip()
        if not decision_id:
            continue
        goal = row.get("goal") if isinstance(row.get("goal"), Mapping) else {}
        question = _first_text(goal, "proposed_goal", "capability") or "awaiting your ok"
        out.append({"decision_id": decision_id, "question": question, "options": []})
    return out


def _dedupe_decisions(*groups: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for group in groups:
        for d in group:
            seen.setdefault(d["decision_id"], d)
    return list(seen.values())


# --- what a tool actually reported ------------------------------------------------------------------------

def _tool_results(runs: list[dict]) -> list[dict]:
    """The last tool outcome per open run, with the verification folded in.

    `verification_result` carries no capability of its own ({"verified": …, "reason": …}), so on its own
    it cannot be attributed to anything and would be dropped. Folded onto the tool result it belongs to,
    it becomes the distinction the transcript never made: a send that returned 200 versus a send that was
    read back. `reason` is deliberately not carried — it is free text written near message content.
    """
    out: list[dict] = []
    for run in runs:
        last = run.get("last_tool_result")
        if not isinstance(last, Mapping):
            continue
        capability = _first_text(last, "capability")
        if not capability:
            continue
        verification = run.get("verification_result")
        verified = bool(last.get("verified")) or bool(
            verification.get("verified") if isinstance(verification, Mapping) else False)
        out.append({"capability": capability,
                    "outcome": _first_text(last, "outcome", "status"),
                    "verified": verified,
                    "entity_id": _first_text(last, "provider_entity_id", "entity_id")})
        if len(out) >= MAX_TOOL_RESULTS:
            break
    return out


# --- capabilities ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Capabilities:
    operations: tuple[dict, ...] = ()
    providers: tuple[str, ...] = ()
    read: bool = False
    probed: int = 0


async def _read_capabilities(user_id: UUID, faults: _Faults) -> _Capabilities:
    """Resolve every declared-live capability against THIS user, right now, and keep only the ok ones.

    The registry says what exists; only the broker knows what this student can actually call, and the
    gap between those two is the whole content of "i can't send messages for you" while
    `gmail.send_message` was ok. So availability is asked per capability and nothing is inferred from a
    provider being present: a Google connection granted for calendar only must yield a calendar
    operation and NO gmail send.

    Probed concurrently because each probe is its own round trip and a turn cannot afford nine of them in
    series. Deliberately NOT cached here — a second cache of capability truth beside the broker's is how
    two answers to "can you send mail" come to exist, which is the defect, not the fix.

    `connected` is tracked separately from `ok`: an account can be connected while a specific operation
    is unscoped, and "your Google account is connected but I was never granted permission to send mail"
    is a true, actionable sentence that a list of ok-operations alone cannot produce.
    """
    specs = [t for t in tool_registry.specs(None) if t.live]
    results = await asyncio.gather(
        *(tool_broker.availability(user_id, t.capability) for t in specs), return_exceptions=True)

    operations: list[dict] = []
    providers: set[str] = set()
    read = True
    for spec, availability in zip(specs, results):
        if isinstance(availability, BaseException):
            if not isinstance(availability, Exception):
                raise availability                     # a cancellation is not a degraded capability
            faults.record(AVAILABLE_OPERATIONS, f"tool_broker.availability:{spec.capability}", availability)
            read = False
            continue
        if getattr(availability, "connected", False):
            providers.add(spec.provider)
        if not getattr(availability, "ok", False):
            continue
        operations.append({
            "capability": spec.capability, "provider": spec.provider,
            # a COPY: the registry's dict must not be reachable through a snapshot a planner is holding
            "arg_schema": dict(spec.arg_schema or {}),
            "write": spec.write, "reversible": spec.reversible})
    return _Capabilities(operations=tuple(operations), providers=tuple(sorted(providers)),
                         read=read, probed=len(specs))


# --- clock -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Clock:
    now: str = ""
    timezone: str = ""


async def _read_clock(user_id: UUID, faults: _Faults) -> _Clock:
    """Now, in the student's own timezone.

    Rendering a UTC instant next to the label "America/New_York" would be a four-hour lie in a snapshot
    whose entire job is to stop the model inventing facts, so the conversion happens here and an unusable
    zone falls back to UTC AND relabels itself UTC rather than keeping a name it did not honour.
    """
    tz = await _guard(faults, TIMEZONE, "world_state.resolve_timezone", calendar_schedule.DEFAULT_TZ,
                      world_state.resolve_timezone(user_id, default=calendar_schedule.DEFAULT_TZ))
    now = datetime.now(_timezone.utc)
    try:
        return _Clock(now=now.astimezone(ZoneInfo(str(tz))).isoformat(timespec="seconds"), timezone=str(tz))
    except Exception as exc:
        faults.record(TIMEZONE, "zoneinfo", exc)
        return _Clock(now=now.isoformat(timespec="seconds"), timezone="UTC")


# --- memory (already retrieved; this only shapes it) --------------------------------------------------------

def _sequence(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return [value]


def _from_memory(memory_context: object, *keys: str) -> list:
    """Pull one list out of whatever the memory layer handed over.

    Mapping is checked BEFORE attributes on purpose: `dict.items` is a method, and a duck-typed read of
    `.items` on a plain dict would return a bound method instead of the memories.
    """
    if memory_context is None:
        return []
    if isinstance(memory_context, Mapping):
        for key in keys:
            if key in memory_context:
                return _sequence(memory_context[key])
        return []
    for key in keys:
        if hasattr(memory_context, key):
            return _sequence(getattr(memory_context, key))
    return []


def _fact(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        return _first_text(item, "fact", "text", "summary", "content")
    fact = getattr(item, "fact", None)
    return fact.strip() if isinstance(fact, str) else ""


def _memories(memory_context: object) -> list[str]:
    items = _from_memory(memory_context, "relevant_memories", "memories", "facts", "items")
    return [f for f in (_fact(i) for i in items) if f]


def _people(memory_context: object) -> list:
    # Passed through unshaped: `turn_context._person` already reads name/relation/handle off a mapping or
    # an object and drops anything unnameable, and a second normalization here would be a second opinion.
    return _from_memory(memory_context, "people", "contacts", "persons")


# --- the entry points ---------------------------------------------------------------------------------------

def _attach(ctx: TurnContext, report: AssemblyReport) -> None:
    """Bind the report to the snapshot without touching `TurnContext`'s declared shape.

    `object.__setattr__` bypasses the frozen dataclass's setter, which is the point: this adds no field,
    changes no rendering, and leaves the snapshot exactly as immutable as it was for anyone who does not
    know the attribute is there. If a future `TurnContext` grows `__slots__` this simply cannot bind, and
    the report still travels back on `Assembled`.
    """
    try:
        object.__setattr__(ctx, _REPORT_ATTR, report)
    except Exception:
        log.warning("turn_context_report_unattached degraded=%d", len(report.degraded))


def report_for(ctx: TurnContext) -> AssemblyReport | None:
    """How the snapshot in hand was gathered, or None if this module did not gather it."""
    report = getattr(ctx, _REPORT_ATTR, None)
    return report if isinstance(report, AssemblyReport) else None


def capabilities_were_read(ctx: TurnContext) -> bool:
    """Whether the capability probe actually completed for this snapshot.

    A context of unknown provenance answers False, and that direction is chosen deliberately: the cost of
    a needless caveat is a longer prompt, while the cost of the opposite mistake is Bruce telling a
    student it cannot do something it can — the sentence this workstream exists to delete.
    """
    report = report_for(ctx)
    return bool(report and report.capabilities_read)


def render_for_model(ctx: TurnContext) -> str:
    """`turn_context.render_for_model`, plus the caveat it cannot know to add.

    That renderer prints "none — you have NO operations available this turn" for an empty list, which is
    correct when the probe ran and false when it did not. Only the assembler knows which happened.
    """
    text = turn_context.render_for_model(ctx)
    return text if capabilities_were_read(ctx) else text + "\n" + CAPABILITY_READ_FAILED_LINE


async def assemble_with_report(
    user_id: UUID, *, latest_message: object, reply_reference: object = None,
    recent_turns=(), thread_summary: object = None, memory_context: object = None, trace=None,
) -> Assembled:
    """Read the turn out of the database ONCE and freeze it, alongside the health of that read.

    The four store reads are concurrent because they are independent and a turn pays for them in series
    otherwise; everything derived from the runs (slots, decisions, tool results) is pure and happens
    after. Each read is already isolated, so the gather cannot raise — the outer guard exists for a bug
    in THIS module, because a defect here would otherwise take out every path at once, which is the one
    failure mode the whole design is meant to make impossible.
    """
    faults = _Faults()
    turn_trace.guard(trace, "state_reads_started")
    try:
        runs, capabilities, mission_decisions, clock = await asyncio.gather(
            _read_open_runs(user_id, faults),
            _read_capabilities(user_id, faults),
            _read_mission_decisions(user_id, faults),
            _read_clock(user_id, faults))
    except Exception as exc:
        faults.record(ASSEMBLY, "state_reads", exc)
        runs, capabilities, mission_decisions, clock = [], _Capabilities(), [], _Clock()

    # Marked in STAGES order rather than completion order: these ran concurrently, and a trace whose
    # stages go backwards is unusable for the latency work the marks exist for.
    turn_trace.guard(trace, "decisions_ready")
    turn_trace.guard(trace, "capabilities_ready")
    turn_trace.guard(trace, "agent_runs_ready")

    slots = _derive(faults, GOAL_SLOTS, lambda: _slot_view(runs), {})
    decisions = _derive(faults, PENDING_DECISIONS,
                        lambda: _dedupe_decisions(_run_decisions(runs), mission_decisions),
                        list(mission_decisions))
    tool_results = _derive(faults, RECENT_TOOL_RESULTS, lambda: _tool_results(runs), [])
    memories = _derive(faults, RELEVANT_MEMORIES, lambda: _memories(memory_context), [])
    people = _derive(faults, PEOPLE, lambda: _people(memory_context), [])

    try:
        ctx = turn_context.build(
            latest_message=latest_message, reply_reference=reply_reference or "",
            recent_turns=recent_turns or (), thread_summary=thread_summary or "",
            open_runs=runs, pending_decisions=decisions, goal_slots=slots,
            relevant_memories=memories, people=people,
            connected_providers=capabilities.providers, available_operations=capabilities.operations,
            recent_tool_results=tool_results, current_time=clock.now, timezone=clock.timezone)
    except Exception as exc:
        # A snapshot Bruce cannot build must still not cost the student their turn: rebuild from the
        # message alone, loudly degraded, rather than raising into the inbound path.
        #
        # Through `build` even here, never `TurnContext(latest_message=str(...))`. `_message_text` is
        # what takes `authorizing_text()` off an InputEnvelope; stringifying the envelope instead would
        # splice OCR'd or forwarded text — content nobody authorized — into the student's own words, in
        # the one field the model treats as the instruction.
        faults.record(ASSEMBLY, "turn_context.build", exc)
        try:
            ctx = turn_context.build(latest_message=latest_message)
        except Exception as inner:
            faults.record(ASSEMBLY, "turn_context.build:message_only", inner)
            ctx = TurnContext()

    report = AssemblyReport(degraded=tuple(faults.items), capabilities_read=capabilities.read,
                            operations_probed=capabilities.probed)
    _attach(ctx, report)
    turn_trace.guard(trace, "conversation_state_ready")
    log.info("turn_context_assembled runs=%d ops=%d probed=%d capabilities_read=%s degraded=%s faults=%d",
             len(ctx.open_runs), len(ctx.available_operations), report.operations_probed,
             report.capabilities_read, ",".join(report.degraded_fields) or "none", len(report.degraded))
    return Assembled(context=ctx, report=report)


async def assemble(
    user_id: UUID, *, latest_message: object, reply_reference: object = None,
    recent_turns=(), thread_summary: object = None, memory_context: object = None, trace=None,
) -> TurnContext:
    """THE turn snapshot. Every reasoning path takes this one and reads nothing else from the database.

    Never raises: a store that is down costs its own field and says so in `report_for(ctx)`, and the
    student still gets an answer that knows what it does not know.
    """
    assembled = await assemble_with_report(
        user_id, latest_message=latest_message, reply_reference=reply_reference,
        recent_turns=recent_turns, thread_summary=thread_summary, memory_context=memory_context,
        trace=trace)
    return assembled.context
