"""Typed goal slots — the durable, provenance-tagged answer to "what do I still need before I act?".

WHY THIS EXISTS. A real transcript: the student gives an address, says what the note should say, and
Bruce drafts it. On the NEXT turn Bruce asks for "the recipient and the subject line first" — the
recipient was handed over one turn earlier, the content three turns earlier — and then says it cannot
send messages at all, while the broker is reporting gmail.send_message ok. Nothing was broken at the
tool layer. The run had nowhere to PUT a recipient, so every turn re-derived "what is still missing"
from prose, and a model re-reading a conversation is free to reach a different answer each time. Twenty
two turns produced zero missions and zero agent_runs.

So the missing information gets a type. Slots live INSIDE the AgentRun.goal JSONB that already exists —
no new table, no migration — each carrying its value AND where the value came from, and
`missing_required` (not the model) decides whether a question is owed. A model that believes it has no
question can no longer talk over a genuinely empty slot, and a model that forgot the recipient can no
longer un-remember one the student typed.

TWO INVARIANTS THIS MODULE ENFORCES.

  * A slot cannot drift from the tool it feeds. Slot names and required-ness are DERIVED from the
    ToolSpec arg_schema, and `check_alignment` fails at import if a registry rename leaves a slot
    pointing at an argument no tool accepts. The sibling defect in the same transcript was the model
    emitting a required capability as free text ("sending messages"), which joined to no operation id. A
    slot set maintained by hand beside a schema rots exactly the same way, only more quietly.

  * A value the student TYPED and a value the model GUESSED are not the same fact. Both fill the slot,
    but only provenance can tell you whether an irreversible send is about to go to an address nobody
    ever confirmed. Merging respects that ordering: a guess never overwrites a stated value, while a
    later stated value does — that is a correction, and a correction is the one thing this layer must
    never lose.

Pure by construction: no DB, no network, no model call. Nothing here logs, because every value in a slot
is message content.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from . import tool_registry

# The ONE reserved key inside AgentRun.goal. Everything slot-shaped nests under it so a GoalSpec key and
# a slot name can never collide, and so writing slots is a single-key update on a dict Bruce does not own.
SLOT_KEY = "slots"


class Source(str, Enum):
    """Where a slot value came from. Stored, not inferred — by the time a send is authorized the
    conversation that produced the value may be far out of context."""

    user_stated = "user_stated"      # the student said it, in their own words
    tool_result = "tool_result"      # a provider read-back said it (a resolved address, a real event id)
    model_derived = "model_derived"  # the model inferred/guessed it from context
    default = "default"              # a runtime default nobody asked for


# Trust ordering. `tool_result` outranks a model guess because a provider read-back is an observation,
# not an inference — but it stays BELOW `user_stated`: when the student names an address and a lookup
# disagrees, the student is the authority on who they meant to write to.
_TRUST: dict[Source, int] = {
    Source.default: 0,
    Source.model_derived: 1,
    Source.tool_result: 2,
    Source.user_stated: 3,
}


def _normalize(value: Any) -> Any:
    """Canonical in-memory shape, applied on construction so a value that survived a JSONB round trip
    compares equal to the one that went in. Tuples become lists (JSON has no tuple) and strings lose
    surrounding whitespace (a recipient with a trailing newline is the same recipient)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def _is_empty(value: Any) -> bool:
    """Empty means "carries no information", NOT "falsy". `False` and `0` are answers — an all-day flag
    of False and a duration of 0 are things the student told us, and treating them as empty would make
    Bruce re-ask a question it already had the answer to, which is the whole defect this module closes."""
    if value is None:
        return True
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return len(value) == 0
    return False


def _canonical(value: Any) -> str:
    """A stable string form used ONLY to break a tie deterministically. Never logged, never returned."""
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


@dataclass(frozen=True)
class SlotValue:
    """One filled slot plus its provenance.

    `turn_index` is the position of the turn within the run, and it exists for correctness rather than
    for display: merging has to be order-independent, so the recency that breaks a tie must live in the
    DATA. If recency meant "whichever merge call happened last", replaying the same turns in a different
    order — a retry, a resumed background run, a backfilled inbound message — could settle on a different
    recipient than the live conversation did.
    """

    value: Any
    source: Source
    turn_id: str | None = None
    turn_index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", Source(self.source))
        object.__setattr__(self, "value", _normalize(self.value))

    @property
    def filled(self) -> bool:
        return not _is_empty(self.value)

    @property
    def provenance(self) -> dict:
        return {"turn_id": self.turn_id, "source": self.source.value, "turn_index": self.turn_index}

    @property
    def stated(self) -> bool:
        """Did a human or a provider actually assert this, or did the model make it up?"""
        return self.source in (Source.user_stated, Source.tool_result)

    def to_json(self) -> dict:
        return {"value": self.value, "source": self.source.value,
                "turn_id": self.turn_id, "turn_index": self.turn_index}

    @classmethod
    def from_json(cls, raw: Any) -> SlotValue | None:
        """Tolerant on purpose: a run whose goal blob was written by an older build, or hand-edited, must
        still LOAD. A slot that cannot be parsed is dropped (and will be asked for again), which is
        recoverable; refusing to load the run is not."""
        if not isinstance(raw, dict):
            return None
        try:
            source = Source(raw.get("source"))
        except ValueError:
            return None
        turn_index = raw.get("turn_index")
        if not isinstance(turn_index, int) or isinstance(turn_index, bool):
            turn_index = 0
        turn_id = raw.get("turn_id")
        return cls(value=raw.get("value"), source=source,
                   turn_id=turn_id if isinstance(turn_id, str) else None, turn_index=turn_index)


def _rank(sv: SlotValue) -> tuple[int, int, str]:
    """The total order that decides a conflict: trust first, then recency, then a stable tie-break.

    Trust dominating recency is the point — a model guess on turn 9 must not displace an address the
    student typed on turn 1 — while recency inside the same trust level is what makes a correction work.
    The canonical form is the last resort for two equally-trusted values from the SAME turn: an arbitrary
    winner, but the same arbitrary winner regardless of which order the merges ran in. Because a merge is
    a max over this order, folding turns is associative and commutative, i.e. order-independent.
    """
    return (_TRUST.get(sv.source, 0), sv.turn_index, _canonical(sv.value))


def temporal_pair(kind: GoalKind | str) -> tuple[str | None, str | None]:
    """(the slot that holds a moment, the slot that holds its end) for this kind, or (None, None).

    Read off the SLOT SCHEMA rather than named: the datetime-typed slots, in declaration order, are the
    moment and its end — the convention `_DECLARED` already writes down (`start` before `end`). Naming
    them in a handler would be product knowledge, and product knowledge inside a generic handler is how
    the calendar path and the email path came to disagree about what a turn was.
    """
    dts = [s.name for s in slot_specs(kind) if s.value_kind.rstrip("?") == "datetime"]
    return (dts[0] if dts else None, dts[1] if len(dts) > 1 else None)


_DEFAULT_DURATION = datetime.timedelta(hours=1)


def _is_date_only(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 10


def _is_moment(value: Any) -> bool:
    """Is this a resolved moment, or is it still the words the student typed? A slot holding "whenever
    works" is a question Bruce owes them, never a time anything may be executed against."""
    if not isinstance(value, str):
        return False
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _known_duration(known: Mapping[str, SlotValue] | None, start_name: str,
                    end_name: str) -> datetime.timedelta:
    """How long the event ALREADY is. Defaulted to an hour rather than to zero, and never negative: a
    change to the day must not silently reshape a two-hour event into a moment."""
    start = (known or {}).get(start_name)
    end = (known or {}).get(end_name)
    if start is None or end is None or not start.filled or not end.filled:
        return _DEFAULT_DURATION
    if _is_date_only(start.value) or _is_date_only(end.value):
        return _DEFAULT_DURATION
    try:
        span = (datetime.datetime.fromisoformat(str(end.value))
                - datetime.datetime.fromisoformat(str(start.value)))
    except ValueError:
        return _DEFAULT_DURATION
    return span if span > datetime.timedelta(0) else _DEFAULT_DURATION


def reconcile_temporal(kind: GoalKind | str, known: Mapping[str, SlotValue] | None,
                       incoming: Mapping[str, SlotValue] | None) -> dict[str, SlotValue]:
    """A DATE-ONLY change keeps the clock the student already set, and moves the end with the start.

    "MOVE IT TO FRIDAY" CHANGES THE DAY. It says nothing about the hour, and a 4pm rehearsal moved to
    friday is a 4pm rehearsal on friday. `temporal.resolve` correctly reports a date-only phrase as a
    whole day; writing both halves of that over a start and an end the student had already pinned turns
    their event into an all-day block, against something they never said and with no way to notice.

    THIS IS THE ONLY PLACE THAT CAN DECIDE IT, which is why it lives at the merge rather than in the
    resolver. Turning a phrase into a moment happens before the goal is loaded and cannot see the clock it
    is about to overwrite; the merge is where the old value and the new one are both in hand.

    Three rules, and the date-only one is `calendar_mutation.recompute`'s — the reference implementation
    on the other calendar path, pinned against it in `test_temporal_patching`:

      * an explicit new time REPLACES the clock          "move it to friday at 5pm" -> 5pm
      * a date-only change KEEPS it                      "move it to friday"        -> still 4pm
      * the end always moves with the start, by the duration the event already had

    The two lanes still differ on ONE thing, and it is recorded rather than smoothed over: when the
    student states a new TIME, `recompute` keeps the event's old duration while this lane takes
    `temporal.resolve`'s (an hour, when no range was stated). They could only agree if the merge could
    tell a defaulted end from a stated one, and `Resolved` does not carry that distinction — guessing it
    from the span would stretch a genuine one-hour range to two. See the test that asserts the gap.

    An event that was all-day to begin with stays all-day: there is no clock to keep, and inventing one
    would put a whole-day block at midnight. A phrase that resolved to no moment at all is dropped rather
    than merged, because prose in a start slot is a call nothing can build.
    """
    out = dict(incoming or {})
    start_name, end_name = temporal_pair(kind)
    if start_name is None or end_name is None:
        return out
    new = out.get(start_name)
    old = (known or {}).get(start_name)
    if new is None or not new.filled or old is None or not old.filled:
        return out

    if not _is_moment(new.value) and _is_moment(old.value):
        # AN UNREADABLE PHRASE IS NOT A NEW TIME. `resolve_temporal` leaves a when-phrase it cannot read
        # exactly as the student typed it, so Bruce asks rather than inventing a moment — but on a goal
        # that already HAS one, merging that prose in replaces a real start with "whenever works" and the
        # executor can no longer build the call at all. The pinned moment stands and the offer is made
        # again against it, which is visible to the student in a way a corrupted slot is not.
        out.pop(start_name, None)
        out.pop(end_name, None)
        return out

    if not _is_date_only(new.value) or _is_date_only(old.value):
        return out
    try:
        day = datetime.date.fromisoformat(str(new.value))
        base = datetime.datetime.fromisoformat(str(old.value))
    except ValueError:
        return out
    start = datetime.datetime.combine(day, base.time())
    end = start + _known_duration(known, start_name, end_name)
    out[start_name] = replace(new, value=start.isoformat(timespec="seconds"))
    # The end is REWRITTEN, not merged. `slot_patch` is replacement-shaped and can only name one slot, so
    # a start moved on its own would leave the previous end beside it — an event that starts on friday and
    # ends on tuesday. Same provenance and same turn index as the start, which is what lets `merge_slots`
    # prefer it over the stale one.
    out[end_name] = SlotValue(end.isoformat(timespec="seconds"), new.source, turn_id=new.turn_id,
                              turn_index=new.turn_index)
    return out


def merge_slots(existing: dict[str, SlotValue] | None,
                incoming: dict[str, SlotValue] | None) -> dict[str, SlotValue]:
    """Fold new slot values onto known ones. Returns a NEW dict; neither argument is mutated.

    Two rules, both learned from the transcript:

      * Silence is not a retraction. A turn that mentions only tone says nothing about the recipient, so
        an absent or empty incoming value can never blank a filled slot. This is the direct fix for
        "i need the recipient first" one turn after being given the recipient.
      * A guess never beats an assertion, but a later assertion beats an earlier one. "actually send it
        to the other address" has to win; the model deciding on turn 6 that it probably meant someone
        else must not.
    """
    merged = {name: sv for name, sv in (existing or {}).items() if sv is not None and sv.filled}
    for name, candidate in (incoming or {}).items():
        if candidate is None or not candidate.filled:
            continue
        current = merged.get(name)
        if current is None or _rank(candidate) > _rank(current):
            merged[name] = candidate
    return merged


# --- what each kind of goal needs ----------------------------------------------------------------------

class GoalKind(str, Enum):
    """The goal shapes that have a typed slot set. Adding one is a row below, never a branch in a handler."""

    send_email = "send_email"
    schedule_event = "schedule_event"


@dataclass(frozen=True)
class _SlotDecl:
    """Declaration only. `required` is left None wherever the tool schema already knows the answer, so
    required-ness has exactly one source of truth."""

    name: str
    tool_arg: str | None = None
    required: bool | None = None
    value_kind: str = "str"


@dataclass(frozen=True)
class SlotSpec:
    """A resolved slot: its name, the tool argument it becomes, and whether execution is legal without it."""

    name: str
    tool_arg: str | None
    required: bool
    value_kind: str


# Slot sets, declared against the capability that will execute them. Slots WITH a tool_arg inherit their
# type and their required-ness from tool_registry's arg_schema ("str?" means optional), so the question
# Bruce asks and the argument the broker receives cannot diverge. Slots WITHOUT one (tone, purpose,
# attendees) are runtime-only: they shape the draft or the plan, never an argument, and are optional
# because no send has ever been blocked on not knowing a tone.
_DECLARED: dict[GoalKind, tuple[str, tuple[_SlotDecl, ...]]] = {
    GoalKind.send_email: ("gmail.send_message", (
        _SlotDecl("recipient", tool_arg="to"),
        _SlotDecl("subject", tool_arg="subject"),
        _SlotDecl("body", tool_arg="body"),
        _SlotDecl("tone", required=False),
        _SlotDecl("purpose", required=False),
    )),
    GoalKind.schedule_event: ("calendar.create_event", (
        _SlotDecl("title", tool_arg="title"),
        _SlotDecl("start", tool_arg="start"),
        _SlotDecl("end", tool_arg="end"),
        _SlotDecl("timezone", tool_arg="timezone"),
        _SlotDecl("attendees", required=False, value_kind="list"),
    )),
}


def check_alignment(lookup=tool_registry.get) -> tuple[str, ...]:
    """Every declared tool_arg must exist in the live ToolSpec arg_schema. Returns the drifts it found.

    This is the anti-drift mechanism itself, not a test helper. `lookup` is injectable so the check can be
    exercised against a renamed schema without mutating the real registry.
    """
    problems: list[str] = []
    for kind, (capability, decls) in _DECLARED.items():
        spec = lookup(capability)
        if spec is None:
            problems.append(f"{kind.value} -> unknown capability {capability}")
            continue
        schema = spec.arg_schema or {}
        for d in decls:
            if d.tool_arg is not None and d.tool_arg not in schema:
                problems.append(f"{kind.value}.{d.name} -> {capability} has no argument {d.tool_arg!r}")
    return tuple(problems)


def _resolve(kind: GoalKind) -> tuple[SlotSpec, ...]:
    capability, decls = _DECLARED[kind]
    spec = tool_registry.get(capability)
    schema = (spec.arg_schema if spec else {}) or {}
    out: list[SlotSpec] = []
    for d in decls:
        declared_type = schema.get(d.tool_arg) if d.tool_arg else None
        if declared_type is None:
            out.append(SlotSpec(d.name, d.tool_arg, bool(d.required), d.value_kind))
            continue
        # "?" in the arg schema is the tool saying the argument is optional. An explicit override exists
        # for the reverse case — a slot Bruce refuses to guess even though the provider would accept the
        # call without it — and today nothing needs one.
        optional = declared_type.endswith("?")
        required = (not optional) if d.required is None else bool(d.required)
        out.append(SlotSpec(d.name, d.tool_arg, required, declared_type.rstrip("?")))
    return tuple(out)


# Resolved once. Drift is fatal AT IMPORT rather than at send time: a slot pointing at an argument no tool
# accepts means Bruce would ask the student for something it can never use, which is precisely the class
# of silent nonsense ("sending messages" as a capability id) this layer exists to make impossible.
_DRIFT = check_alignment()
if _DRIFT:
    raise RuntimeError("goal slots have drifted from the tool registry: " + "; ".join(_DRIFT))

_SPECS: dict[GoalKind, tuple[SlotSpec, ...]] = {k: _resolve(k) for k in _DECLARED}


def _kind(kind: GoalKind | str) -> GoalKind:
    """Coerce, and raise on an unknown kind. Returning "nothing missing" for a kind nobody declared would
    report an unfillable goal as ready to execute."""
    return kind if isinstance(kind, GoalKind) else GoalKind(kind)


def slot_specs(kind: GoalKind | str) -> tuple[SlotSpec, ...]:
    """Declaration order, which is also the order to ask in: the load-bearing arguments first."""
    return _SPECS[_kind(kind)]


def spec_for(kind: GoalKind | str, name: str) -> SlotSpec | None:
    return next((s for s in slot_specs(kind) if s.name == name), None)


def required_slots(kind: GoalKind | str) -> tuple[str, ...]:
    return tuple(s.name for s in slot_specs(kind) if s.required)


def capability_for(kind: GoalKind | str) -> str:
    return _DECLARED[_kind(kind)][0]


def kind_for_capability(capability: str) -> GoalKind | None:
    return next((k for k, (cap, _) in _DECLARED.items() if cap == capability), None)


def missing_required(kind: GoalKind | str, slots: dict[str, SlotValue] | None) -> tuple[str, ...]:
    """The typed answer to "what do I still need" — and the thing that replaces the model deciding it has
    a question. Empty EXACTLY when every argument the tool requires is present, so a clarifying question
    can be generated from this and only from this."""
    filled = slots or {}
    out: list[str] = []
    for s in slot_specs(kind):
        if not s.required:
            continue
        sv = filled.get(s.name)
        if sv is None or not sv.filled:
            out.append(s.name)
    return tuple(out)


def is_ready(kind: GoalKind | str, slots: dict[str, SlotValue] | None) -> bool:
    """Execution is legal. Says nothing about whether it is AUTHORIZED — approval and provenance are
    separate gates (see `guessed_required`)."""
    return not missing_required(kind, slots)


def guessed_required(kind: GoalKind | str, slots: dict[str, SlotValue] | None) -> tuple[str, ...]:
    """Required slots that are filled but that nobody actually asserted — the model inferred them.

    This is why provenance is stored rather than recomputed. gmail.send_message is reversible=False: once
    a thank-you note reaches a guessed address there is no undo, so "complete" and "confirmed" have to be
    answerable separately. A caller may execute on a stated set and must stop for a guessed one.
    """
    filled = slots or {}
    out: list[str] = []
    for s in slot_specs(kind):
        sv = filled.get(s.name)
        if s.required and sv is not None and sv.filled and not sv.stated:
            out.append(s.name)
    return tuple(out)


def tool_arguments(kind: GoalKind | str, slots: dict[str, SlotValue] | None) -> dict:
    """The arguments for `capability_for(kind)`, keyed by the tool's OWN names.

    Raises when a required slot is still empty. A partially-filled send is not a degraded send, it is a
    wrong one — an email with a guessed-blank subject reaching a real person cannot be taken back. The
    error names the missing SLOTS and never their values.
    """
    k = _kind(kind)
    missing = missing_required(k, slots)
    if missing:
        raise ValueError(f"{k.value} is missing required slots: {', '.join(missing)}")
    filled = slots or {}
    args: dict[str, Any] = {}
    for s in slot_specs(k):
        sv = filled.get(s.name)
        if s.tool_arg and sv is not None and sv.filled:
            args[s.tool_arg] = sv.value
    return args


# --- persistence into the goal blob that already exists --------------------------------------------------

def to_goal_jsonb(goal: dict | None, kind: GoalKind | str,
                  slots: dict[str, SlotValue] | None) -> dict:
    """Write the slot block into an existing goal dict, returning a NEW dict.

    Copy-then-set rather than in-place mutation: the caller's dict is usually the AgentRun.goal payload,
    and a GoalSpec key (action, domain, temporal, source_message_ids…) written by another layer must
    survive this untouched. Only SLOT_KEY is ever replaced.
    """
    out = dict(goal or {})
    out[SLOT_KEY] = {
        "kind": _kind(kind).value,
        "values": {n: sv.to_json() for n, sv in (slots or {}).items() if sv is not None and sv.filled},
    }
    return out


def from_goal_jsonb(goal: dict | None) -> tuple[GoalKind | None, dict[str, SlotValue]]:
    """Read the slot block back. A goal with no slots, or a damaged one, yields (None, {}) instead of an
    exception — an unreadable slot costs one question, an unloadable run costs the whole mission."""
    block = (goal or {}).get(SLOT_KEY)
    if not isinstance(block, dict):
        return None, {}
    try:
        kind: GoalKind | None = GoalKind(block.get("kind"))
    except ValueError:
        kind = None
    values = block.get("values")
    slots: dict[str, SlotValue] = {}
    if isinstance(values, dict):
        for name, raw in values.items():
            if not isinstance(name, str):
                continue
            sv = SlotValue.from_json(raw)
            if sv is not None and sv.filled:
                slots[name] = sv
    return kind, slots
