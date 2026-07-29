"""TurnContext — ONE immutable snapshot of what is true this turn, handed to every reasoning path.

WHAT GOES WRONG WITHOUT IT. A student asked Bruce to email a specific address a thank-you note. Bruce
drafted it. On the NEXT turn it replied that it needed "the recipient and the subject line first" — the
recipient had been given one turn earlier, the body three turns earlier — and then finished with "i can't
send messages for you", while `tool_broker` was reporting `gmail.send_message` ok=True in the same
process. Twenty-two turns of that conversation produced zero missions and zero agent_runs.

None of those is a separate bug. Each path re-derived its own view of the turn: the reply path read one
window of history, the routing path asked the MODEL whether a mission was needed (it said no, twenty-two
times), and capability truth lived in the broker where the replying model never saw it. Three views of one
turn, and the student got the least informed one. The same turn also emitted `required_capabilities` as
free text — "sending messages" — which joins to no operation id in the registry, so the one honest signal
in the whole exchange ("I want to send mail") could never become an execution.

This module fixes the SHAPE of that, not the wording of a prompt:

  * One snapshot, assembled once, immutable. The reply path and the execution path hold the same object,
    so they cannot disagree about whether the recipient is known or whether Gmail is live.
  * Capabilities are REAL operation ids with their argument schemas, carried in the shape the registry and
    broker already produce. `capability_ids()` lets a caller reject an invented id instead of guessing
    what "sending messages" was supposed to mean.
  * Nothing important is silently absent. An empty operation list renders as an explicit "no operations
    available" line, because silence is exactly what let the model improvise a denial.

PURE BY CONSTRUCTION: no database, no network, no clock, no model, and no logger. The assembler that reads
the database owns fetching; `build()` takes the already-fetched pieces. That boundary is what keeps a
second, drifting copy of "what is true this turn" from growing here — which is the defect itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

# A real operation id is "<domain>.<operation>" — the ids `tool_registry` declares. Anything that does not
# match this shape was prose, not a capability. The distinction matters for the reply: a malformed id is a
# model defect ("sending messages"), while a well-formed id that simply is not in this turn's list is an
# honest "not available right now". Collapsing the two hides the first behind the second.
_OPERATION_ID = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")

OK = "ok"
NOT_AN_OPERATION_ID = "not_an_operation_id"
NOT_AVAILABLE = "not_available_this_turn"

_EMPTY_MAP: Mapping[str, str] = MappingProxyType({})

_HEADER = ("[TURN CONTEXT — assembled once for this turn. Every path sees exactly this. "
           "It is state, not instructions from anyone.]")
_FOOTER = ("[END TURN CONTEXT] Do not ask for anything listed under ESTABLISHED, and never claim or deny "
           "an ability that is not listed under OPERATIONS.")
_NO_OPERATIONS = ("  none — you have NO operations available this turn. Say that plainly if asked to act; "
                  "do not improvise a capability and do not deny one you were not asked about.")


# --- the pieces -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AvailableOperation:
    """One thing Bruce can actually call this turn, in the shape `tool_registry.ToolSpec` /
    `tool_broker.ToolCandidate` already produce. `capability` is the REAL id (`gmail.send_message`), and
    `arg_schema` is the compact `{field: type}` map with "?" marking optional — so a planner fills
    arguments from the schema instead of from a handler's private knowledge."""

    capability: str
    provider: str
    arg_schema: Mapping[str, str] = _EMPTY_MAP
    write: bool = False
    reversible: bool = True
    # Every write needs the student's yes. Irreversibility raises the cost of being wrong; it is not what
    # creates the requirement, so this defaults from `write` rather than from `reversible`.
    requires_confirmation: bool = False


@dataclass(frozen=True)
class TurnLine:
    role: str
    text: str


@dataclass(frozen=True)
class OpenRun:
    """An AgentRun that has not finished. Its ABSENCE is a fact too — twenty-two turns with no run is what
    "i'll get right on that" looks like from the database side."""

    run_id: str
    domain: str = ""
    status: str = ""
    what: str = ""
    blocked_reason: str = ""


@dataclass(frozen=True)
class PendingDecision:
    decision_id: str
    question: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Person:
    name: str
    relation: str = ""
    handle: str = ""


@dataclass(frozen=True)
class ToolResultLine:
    """What a tool actually reported, carried so a reply cannot contradict it. Deliberately has no free-text
    field: an id and an outcome are enough to stop "i can't send messages" following an ok send, and a
    message body has no business travelling in a snapshot that gets rendered, traced and compared."""

    capability: str
    outcome: str
    verified: bool = False
    entity_id: str = ""


@dataclass(frozen=True)
class TurnContext:
    """Everything one turn is allowed to know, frozen before any reasoning starts.

    Immutable on purpose and all the way down: tuples for sequences, read-only mappings for maps. A path
    that could edit the snapshot would recreate the original defect one call frame later.
    """

    latest_message: str = ""
    reply_reference: str = ""
    recent_turns: tuple[TurnLine, ...] = ()
    thread_summary: str = ""
    open_runs: tuple[OpenRun, ...] = ()
    pending_decisions: tuple[PendingDecision, ...] = ()
    # value None/"" means the slot is known to be needed and is NOT filled yet. The transcript asked for a
    # recipient it had already been given, so filled and missing are both rendered, explicitly.
    goal_slots: Mapping[str, str | None] = _EMPTY_MAP
    relevant_memories: tuple[str, ...] = ()
    people: tuple[Person, ...] = ()
    connected_providers: tuple[str, ...] = ()
    available_operations: tuple[AvailableOperation, ...] = ()
    recent_tool_results: tuple[ToolResultLine, ...] = ()
    current_time: str = ""
    timezone: str = ""


# --- normalization (pure; the assembler fetches, this only shapes) -----------------------------------------

def _get(src: object, *names: str, default: object = None) -> object:
    """Read a field from a mapping row (`agent_run_store` returns dicts) or an ORM/dataclass object
    (`schema.AgentRun`, `runtime_contracts.ToolResult`) without the assembler having to convert first."""
    for name in names:
        if isinstance(src, Mapping):
            if name in src:
                return src[name]
        elif hasattr(src, name):
            return getattr(src, name)
    return default


def _text(value: object) -> str:
    """A field's text, stripped. `None` becomes "" so a missing value and an empty one render alike, and an
    enum renders as its value — `ToolOutcome.ok` must reach the model as "ok", never "ToolOutcome.ok"."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(getattr(value, "value", value)).strip()


def _when(value: object) -> str:
    """A moment as text. A datetime is rendered to seconds — a snapshot whose header changed every
    microsecond could never be compared to the one another path was holding."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso(timespec="seconds")
        except TypeError:                       # date.isoformat() takes no arguments
            return iso()
    return _text(value)


def _message_text(value: object) -> str:
    """The student's own words. Handed an `InputEnvelope`, this takes `authorizing_text()` and nothing
    else: merging OCR or a forwarded email into `latest_message` would rebuild the exact confusion the
    envelope exists to prevent, and a snapshot is precisely where such a merge would become permanent."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    trusted = getattr(value, "authorizing_text", None)
    if callable(trusted):
        return _text(trusted())
    return _text(_get(value, "text", "trusted_text", default=""))


def _bool(value: object, default: bool = False) -> bool:
    return default if value is None else bool(value)


def _schema_map(raw: object) -> Mapping[str, str]:
    """A read-only COPY of an argument schema. The copy is not ceremony: `tool_broker` hands out a dict it
    built from the registry, and a planner that mutated it would silently change what every later turn
    believes the tool takes."""
    if not isinstance(raw, Mapping) or not raw:
        return _EMPTY_MAP
    return MappingProxyType({_text(k): _text(v) for k, v in raw.items()})


def _operation(src: object) -> AvailableOperation | None:
    """One available operation from a `ToolSpec`, a broker `ToolCandidate`, a mapping, or an already-built
    `AvailableOperation`. An entry with no capability id is dropped — an operation the caller cannot name
    is not an operation, and rendering it would put unnameable ability in front of the model."""
    if isinstance(src, AvailableOperation):
        return src
    capability = _text(_get(src, "capability", "operation_id", "id"))
    if not capability:
        return None
    write = _bool(_get(src, "write"))
    confirm = _get(src, "requires_confirmation")
    return AvailableOperation(
        capability=capability,
        provider=_text(_get(src, "provider")),
        arg_schema=_schema_map(_get(src, "arg_schema")),
        write=write,
        reversible=_bool(_get(src, "reversible"), default=True),
        requires_confirmation=write if confirm is None else bool(confirm))


def _operations(items: Iterable[object]) -> tuple[AvailableOperation, ...]:
    """Deduped by capability and sorted by id, so two assemblers that gathered the same tools in different
    orders produce the same snapshot and therefore the same prompt."""
    seen: dict[str, AvailableOperation] = {}
    for item in items or ():
        op = _operation(item)
        if op is not None and op.capability not in seen:
            seen[op.capability] = op
    return tuple(seen[cap] for cap in sorted(seen))


def _turn_line(src: object) -> TurnLine | None:
    if isinstance(src, TurnLine):
        return src
    text = _text(_get(src, "text", "content", "body"))
    if not text:
        return None
    return TurnLine(role=_text(_get(src, "role", "direction", default="user")) or "user", text=text)


def _goal_text(goal: object) -> str:
    """What an open run is FOR. JSONB `goal` can arrive as a str/list/number, so a non-dict degrades to its
    own text rather than raising and taking the whole snapshot with it."""
    if isinstance(goal, Mapping):
        return _text(goal.get("desired_outcome") or goal.get("title") or goal.get("action"))
    return _text(goal) if isinstance(goal, str) else ""


def _open_run(src: object) -> OpenRun | None:
    if isinstance(src, OpenRun):
        return src
    run_id = _text(_get(src, "id", "run_id"))
    if not run_id:
        return None
    return OpenRun(run_id=run_id, domain=_text(_get(src, "domain")), status=_text(_get(src, "status")),
                   what=_goal_text(_get(src, "goal")) or _text(_get(src, "desired_outcome")),
                   blocked_reason=_text(_get(src, "blocked_reason")))


def _decision(src: object) -> PendingDecision | None:
    if isinstance(src, PendingDecision):
        return src
    decision_id = _text(_get(src, "id", "decision_id"))
    if not decision_id:
        return None
    raw_options = _get(src, "options", "choices", default=())
    # A bare string is not a list of options; iterating it would offer the student one letter per choice.
    listed = raw_options if isinstance(raw_options, Sequence) and not isinstance(raw_options, str) else ()
    options = tuple(o for o in (_text(x) for x in listed) if o)
    return PendingDecision(decision_id=decision_id,
                           question=_text(_get(src, "question", "prompt", "summary")), options=options)


def _person(src: object) -> Person | None:
    if isinstance(src, Person):
        return src
    name = _text(_get(src, "name", "display_name", "label"))
    if not name:
        return None
    return Person(name=name, relation=_text(_get(src, "relation", "role", "relationship")),
                  handle=_text(_get(src, "handle", "email", "address")))


def _tool_result(src: object) -> ToolResultLine | None:
    if isinstance(src, ToolResultLine):
        return src
    capability = _text(_get(src, "capability"))
    if not capability:
        return None
    return ToolResultLine(capability=capability, outcome=_text(_get(src, "outcome", "status")),
                          verified=_bool(_get(src, "verified")),
                          entity_id=_text(_get(src, "provider_entity_id", "entity_id")))


def _collect(items: Iterable[object], fn) -> tuple:
    return tuple(built for built in (fn(i) for i in (items or ())) if built is not None)


def build(*, latest_message: object = "", reply_reference: object = "",
          recent_turns: Iterable[object] = (), thread_summary: object = "",
          open_runs: Iterable[object] = (), pending_decisions: Iterable[object] = (),
          goal_slots: Mapping[str, object] | None = None, relevant_memories: Iterable[object] = (),
          people: Iterable[object] = (), connected_providers: Iterable[object] = (),
          available_operations: Iterable[object] = (), recent_tool_results: Iterable[object] = (),
          current_time: object = "", timezone: object = "") -> TurnContext:
    """Freeze already-fetched pieces into the turn's one snapshot. Pure: no DB, no network, no clock.

    Every sequence is copied into a tuple and every mapping into a read-only copy, so a caller that keeps
    mutating its own working dicts after this call cannot change what the turn believes — the snapshot is
    a fact about a moment, not a live view of the assembler's variables.
    """
    slots = {_text(k): (None if v is None else _text(v)) for k, v in (goal_slots or {}).items()}
    return TurnContext(
        latest_message=_message_text(latest_message),
        reply_reference=_text(reply_reference),
        recent_turns=_collect(recent_turns, _turn_line),
        thread_summary=_text(thread_summary),
        open_runs=_collect(open_runs, _open_run),
        pending_decisions=_collect(pending_decisions, _decision),
        goal_slots=MappingProxyType(slots),
        relevant_memories=tuple(m for m in (_text(x) for x in (relevant_memories or ())) if m),
        people=_collect(people, _person),
        connected_providers=tuple(sorted({p for p in (_text(x) for x in (connected_providers or ())) if p})),
        available_operations=_operations(available_operations),
        recent_tool_results=_collect(recent_tool_results, _tool_result),
        current_time=_when(current_time),
        timezone=_text(timezone))


# --- validation: a proposed operation id, against reality ---------------------------------------------------

@dataclass(frozen=True)
class CapabilityVerdict:
    ok: bool
    capability: str
    reason: str
    operation: AvailableOperation | None = None


def capability_ids(ctx: TurnContext) -> frozenset[str]:
    """The legal operation ids for this turn. A caller validates a model's proposal against THIS, which is
    the whole answer to `required_capabilities: "sending messages"` — it is not in here, so it does not
    become an execution and does not become a shrug either."""
    return frozenset(op.capability for op in ctx.available_operations)


def operation(ctx: TurnContext, capability: str) -> AvailableOperation | None:
    for op in ctx.available_operations:
        if op.capability == capability:
            return op
    return None


def validate_capability(ctx: TurnContext, proposed: str) -> CapabilityVerdict:
    """Exact match only. No normalization, no nearest-neighbour, no "sending messages" ≈ send_message:
    inferring an operation from prose is how free text became a real side effect in the first place, and a
    fuzzy match would put that back one refactor later."""
    cap = (proposed or "").strip()
    if not _OPERATION_ID.fullmatch(cap):
        return CapabilityVerdict(False, cap, NOT_AN_OPERATION_ID)
    op = operation(ctx, cap)
    if op is None:
        return CapabilityVerdict(False, cap, NOT_AVAILABLE)
    return CapabilityVerdict(True, cap, OK, op)


def unknown_capabilities(ctx: TurnContext, proposed: Iterable[str]) -> tuple[str, ...]:
    """Everything in `proposed` this turn cannot honour, in the order proposed and without repeats — what a
    caller names when it explains what it refused to run."""
    out: list[str] = []
    for item in proposed or ():
        verdict = validate_capability(ctx, item)
        if not verdict.ok and verdict.capability not in out:
            out.append(verdict.capability)
    return tuple(out)


# --- rendering --------------------------------------------------------------------------------------------

def _ordered_schema(schema: Mapping[str, str]) -> list[tuple[str, str]]:
    """Required arguments first, then optional, alphabetical within each. Optionality is carried by the "?"
    suffix rather than by position, so sorting loses nothing — and it makes the rendering independent of
    the insertion order of whichever dict the assembler happened to build."""
    return sorted(schema.items(), key=lambda kv: (kv[1].endswith("?"), kv[0]))


def _render_operation(op: AvailableOperation) -> str:
    args = ", ".join(f"{name}: {kind}" for name, kind in _ordered_schema(op.arg_schema))
    flags = ["write" if op.write else "read-only"]
    if op.write:
        flags.append("reversible" if op.reversible else "NOT reversible")
    if op.requires_confirmation:
        flags.append("needs the student's yes first")
    provider = f" [{op.provider}]" if op.provider else ""
    return f"  {op.capability}({args}){provider} — {', '.join(flags)}"


def render_for_model(ctx: TurnContext) -> str:
    """The deterministic text block for the prompt. Same snapshot in, byte-identical text out — a prompt
    that varied with dict ordering would make two paths disagree for reasons no one could reproduce.

    Bounding is NOT done here. Whatever the assembler decided to include is what the model sees, so the
    trimming decision lives in one place instead of drifting between this and the compiler.
    """
    lines: list[str] = [_HEADER]

    when = ctx.current_time or "unknown — do not assume today's date"
    lines.append(f"NOW: {when}" + (f" ({ctx.timezone})" if ctx.timezone else ""))
    lines.append(f"MESSAGE: {ctx.latest_message}" if ctx.latest_message else "MESSAGE: (no text)")
    if ctx.reply_reference:
        lines.append(f"REPLYING TO: {ctx.reply_reference}")
    if ctx.thread_summary:
        lines.append(f"THREAD SO FAR: {ctx.thread_summary}")

    if ctx.recent_turns:
        lines.append("RECENT TURNS (oldest first):")
        lines.extend(f"  {t.role}: {t.text}" for t in ctx.recent_turns)

    # Filled slots and missing slots BOTH get said. The transcript's second turn asked for a recipient the
    # first turn had supplied; a section that listed only what was missing would have read the same way.
    if ctx.goal_slots:
        filled = [(k, v) for k, v in sorted(ctx.goal_slots.items()) if v]
        missing = [k for k, v in sorted(ctx.goal_slots.items()) if not v]
        lines.append("ESTABLISHED (already known — do NOT ask for these again):")
        lines.extend(f"  {name} = {value}" for name, value in filled)
        if not filled:
            lines.append("  nothing yet")
        if missing:
            lines.append("STILL MISSING (ask for these and nothing else): " + ", ".join(missing))

    # Always rendered, even when empty: "no run is in flight" is the fact that twenty-two turns of
    # "i'll take care of it" concealed.
    lines.append("OPEN RUNS:")
    if ctx.open_runs:
        lines.extend(f"  {r.run_id} [{r.domain or 'no domain'}] {r.status or 'unknown status'}"
                     + (f" — {r.what}" if r.what else "")
                     + (f" (blocked: {r.blocked_reason})" if r.blocked_reason else "")
                     for r in ctx.open_runs)
    else:
        lines.append("  none in flight")

    lines.append("PENDING DECISIONS:")
    if ctx.pending_decisions:
        lines.extend(f"  {d.decision_id} — {d.question or 'awaiting an answer'}"
                     + (f" (options: {', '.join(d.options)})" if d.options else "")
                     for d in ctx.pending_decisions)
    else:
        lines.append("  none")

    if ctx.people:
        lines.append("PEOPLE:")
        lines.extend("  " + " — ".join(p for p in (person.name, person.relation, person.handle) if p)
                     for person in ctx.people)

    if ctx.relevant_memories:
        lines.append("WHAT YOU REMEMBER:")
        lines.extend(f"  {m}" for m in ctx.relevant_memories)

    lines.append("CONNECTED ACCOUNTS: " + (", ".join(ctx.connected_providers) or "none connected"))

    # The section that must never be silent. The model previously had no list at all, so it invented both
    # the capability it claimed and the one it denied.
    lines.append("OPERATIONS YOU MAY CALL (exact ids — anything not listed here does not exist):")
    if ctx.available_operations:
        lines.extend(_render_operation(op) for op in sorted(ctx.available_operations,
                                                            key=lambda o: o.capability))
    else:
        lines.append(_NO_OPERATIONS)

    lines.append("RECENT TOOL RESULTS:")
    if ctx.recent_tool_results:
        lines.extend(f"  {r.capability} -> {r.outcome or 'no outcome'}"
                     + (" (verified)" if r.verified else " (unverified)")
                     + (f" id={r.entity_id}" if r.entity_id else "")
                     for r in ctx.recent_tool_results)
    else:
        lines.append("  none this turn — nothing has been executed, so claim nothing was")

    lines.append(_FOOTER)
    return "\n".join(lines)
