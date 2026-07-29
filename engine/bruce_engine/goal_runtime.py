"""The goal lifecycle the BACKEND owns — when durable state exists, what it still needs, what happens next.

WHAT WENT WRONG WITHOUT IT (a real transcript, verified in the database, not a hypothetical). A student
asked Bruce to email one named address a thank-you note. Turn 2 asked for a recipient that turn 1 had
supplied. An inline reply of "this" resolved nothing. The last turn said "i can't send messages for you"
at confidence 0.97 while `tool_broker` was reporting `gmail.send_message` ok=True. Twenty-two turns
produced ZERO missions and ZERO agent_runs.

The reason there was nothing in the database is the thing this module removes: the MODEL decided whether
durable state should exist. It emitted `needs_mission=false` and `required_capabilities=["sending
messages"]` — free text that joins to no operation id — and the runtime believed both. A language model
asked "does this warrant a mission?" answers fluently at a rate unrelated to whether the answer is
correct, and it answers again next turn with no memory of the last one. So twenty-two turns each decided
independently that no run was needed, and each was individually plausible.

Here the backend decides:

  * `should_create_goal` reads `decision.needs_mission` as ADVISORY and nothing more. What creates a goal
    is an actionable turn naming a REAL registry operation id, or a turn continuing a goal that is
    already open. A conversational turn creates nothing no matter how confidently the model asks for one.
  * `ensure_goal` is the ONLY way a goal is created or advanced, and it is generic. Email and calendar
    differ by slot kind, capability id and adapter — not by a handler, not by a branch. Adding a third
    product is a row in `goal_slots._DECLARED`, and nothing in this file changes.
  * `next_step` answers "what now" with a typed disposition, and the clarifying question is GENERATED
    from missing slot NAMES. The model no longer gets to decide it has a question; if `missing` is empty
    there is no question to ask, and if it is not, the question names exactly what is absent and nothing
    the student already said.

TWO INVARIANTS WORTH STATING OUTRIGHT.

  * Selection is by KIND, never by "the newest run". A student can be booking a rehearsal and writing to
    a teacher in the same thread; if the newest run won, the recipient would land in the calendar goal
    and the start time in the email. Two goals of different kinds coexist and neither contaminates the
    other.
  * Nothing here executes anything. This module decides WHAT should happen; the provider call stays
    behind MutationGateway. It imports no adapter, and it never will — the moment a send lives here,
    "should we" and "did we" are the same code path again, which is how "i can't send messages" came to
    coexist with a working broker.

Nothing in this module logs. Every slot value is message content.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sqlalchemy import select

from . import agent_run_store, goal_slots, schema, tool_registry
from .contract import MachineState
from .db import user_session
from .goal_slots import GoalKind, SlotValue
from .transitions import is_operation_id

# --- why a goal was or was not created ------------------------------------------------------------------
# Typed and closed, like `transitions.REASONS`. A refusal has to be COUNTABLE — "how often did Bruce
# decline to open a goal, and for which of the four different reasons" is the question the transcript
# could not answer, because the refusal was a sentence in a reply rather than a value anywhere.

CONTINUATION = "continuation_of_open_goal"          # a goal of this kind is already open — always continue
ACTIONABLE = "actionable_turn_with_operation_id"    # the turn asks for something Bruce can actually name
NOT_ACTIONABLE = "conversation_only_turn"           # talking, not asking — advisory needs_mission ignored
NO_CAPABILITY = "no_capability_named"               # nothing to do; there is no operation in this turn
NOT_AN_OPERATION_ID = "capability_not_an_operation_id"   # "sending messages" — free text, joins to nothing
UNKNOWN_CAPABILITY = "capability_not_in_registry"   # right shape, no such row (a typo, or an invented id)
NO_GOAL_KIND = "capability_has_no_goal_kind"        # a real operation with no declared slot set to track

REASONS: frozenset[str] = frozenset({
    CONTINUATION, ACTIONABLE, NOT_ACTIONABLE, NO_CAPABILITY, NOT_AN_OPERATION_ID, UNKNOWN_CAPABILITY,
    NO_GOAL_KIND,
})

# The intents that can OPEN durable state on their own. Deliberately one: everything else — a question, a
# summary, an approval, a correction — matters as a CONTINUATION of a goal that already exists, and a
# continuation is decided by looking in the database rather than by reading the intent. `approval` with no
# open goal is an approval of nothing, and the transcript is what "of nothing" looks like in production.
_GOAL_BEARING_INTENTS: frozenset[str] = frozenset({"actionable"})

# Statuses that mean "this run is over; do not attach new turns to it".
#
# `succeeded` / `cancelled` are `contract.TERMINAL_STATES`. `completed` / `dead_letter` belong to the
# background-lease vocabulary (`background_runner`), which shares this column. `failed` is here even
# though `contract` calls it RECOVERABLE, because the background lane uses it as a terminal outcome —
# and nothing is lost by that: a failed run keeps its slots, and the recovery path reopens it with an
# explicit failed -> preparing transition, after which this module sees it as open again. What must not
# happen is a dead run silently swallowing the next thing the student says.
_CLOSED_STATUSES: frozenset[str] = frozenset({
    "succeeded", "cancelled", "completed", "failed", "dead_letter"})

# How far back a continuation may reach. A student with a long history should not cost an unbounded scan
# to answer "is a send already in flight", and a goal older than the last few open runs is not what the
# current turn is continuing.
_SCAN_LIMIT = 20

# Where a run records the conversation it belongs to, INSIDE the goal blob. The column
# `agent_runs.conversation_id` exists but `agent_run_store.create_run` has no parameter for it, so a run
# created through the store is always NULL there. Writing it into the goal keeps attribution honest today;
# when the store learns the parameter this key becomes a fallback rather than the primary.
CONVERSATION_KEY = "conversation_id"

# Approval words a decision block may carry. Tolerant because the layer that WRITES `active_decision`
# lives elsewhere; a shape this module cannot parse reads as "not approved", which fails safe — it costs
# one extra confirmation, never an unapproved send.
_APPROVED = frozenset({"approved", "accepted", "confirmed", "approve", "yes", "true"})


# --- 1. should a goal exist at all? ---------------------------------------------------------------------

@dataclass(frozen=True)
class CreationVerdict:
    """Whether durable state should exist after this turn, and WHY — one of the constants above."""

    create: bool
    reason: str


def _intent(decision: Any) -> str:
    """The decision's intent as plain text. Accepts the Pydantic `ConversationDecision`, a dict, or None —
    an enum renders as its value so `IntentKind.actionable` and "actionable" are the same fact."""
    if decision is None:
        return ""
    raw = decision.get("intent") if isinstance(decision, Mapping) else getattr(decision, "intent", None)
    if raw is None:
        return ""
    return str(getattr(raw, "value", raw)).strip()


def creation_verdict(*, decision: Any, capability: str | None, continuation: bool) -> CreationVerdict:
    """The backend's decision, with its reason attached.

    ORDER MATTERS. A continuation is checked FIRST and short-circuits everything: once a goal is open, the
    turn that answers its question is often intent=clarification and names no capability at all ("sarah@
    school.edu"), and a rule that demanded an actionable intent would drop the answer on the floor and ask
    again. That specific drop is the transcript's second turn.
    """
    if continuation:
        return CreationVerdict(True, CONTINUATION)

    # `decision.needs_mission` is NEVER read. It is the field that said "no" twenty-two times.
    if _intent(decision) not in _GOAL_BEARING_INTENTS:
        return CreationVerdict(False, NOT_ACTIONABLE)

    cap = (capability or "").strip()
    if not cap:
        return CreationVerdict(False, NO_CAPABILITY)
    if not is_operation_id(cap):
        # "sending messages" gets its own reason instead of dissolving into "unknown": one means the model
        # emitted prose where an id belongs, the other means it named an operation that does not exist.
        # Different defects, different fixes, and the runtime should be able to count them apart.
        return CreationVerdict(False, NOT_AN_OPERATION_ID)
    if tool_registry.get(cap) is None:
        return CreationVerdict(False, UNKNOWN_CAPABILITY)
    if goal_slots.kind_for_capability(cap) is None:
        # A real operation with no declared slot set — gmail.get_message, say. There is nothing to collect
        # and nothing to remember between turns, so a goal would be an empty row that never closes.
        return CreationVerdict(False, NO_GOAL_KIND)
    return CreationVerdict(True, ACTIONABLE)


def should_create_goal(*, decision: Any, capability: str | None, continuation: bool) -> bool:
    """THE BACKEND DECIDES. Pure and synchronous — no DB, no model, no clock.

    `continuation` is a fact about the database (see `is_continuation`), not a model opinion, and
    `decision` is consulted only for its intent. Resolve `capability` against `turn_context.capability_ids`
    before calling: this checks that an id is real, not that the student is allowed to use it.
    """
    return creation_verdict(decision=decision, capability=capability, continuation=continuation).create


# --- 2. finding the goal that is already open -----------------------------------------------------------

def _row_dict(r: "schema.AgentRun") -> dict:
    """The subset of a run this module reasons about, shaped like `agent_run_store._to_dict` where the keys
    overlap, plus the `conversation_id` that dict drops."""
    return {
        "id": str(r.id), "user_id": str(r.user_id), "domain": r.domain, "status": r.status,
        "goal": r.goal or {}, "active_decision": r.active_decision,
        "conversation_id": str(r.conversation_id) if r.conversation_id else None,
        "mission_id": str(r.mission_id) if r.mission_id else None,
        "blocked_reason": r.blocked_reason,
    }


def _conversation_of(run: Mapping) -> str:
    """Which conversation a run belongs to: the column if the writer set it, else the goal blob, else "".

    An empty answer means UNATTRIBUTED, and unattributed runs stay visible to every conversation on
    purpose. Background missions are created by the worker with no conversation at all; hiding them would
    make a student who asks about a send in a new thread open a SECOND send for the same thing.
    """
    column = run.get("conversation_id")
    if column:
        return str(column)
    goal = run.get("goal")
    if isinstance(goal, Mapping) and goal.get(CONVERSATION_KEY):
        return str(goal[CONVERSATION_KEY])
    return ""


async def open_runs(user_id: UUID, *, conversation_id: str | UUID | None = None,
                    limit: int = _SCAN_LIMIT) -> list[dict]:
    """Every non-terminal run for this user, newest first, across ANY domain.

    ANY domain is the whole point. `agent_run_store.latest_active` defaults to domain="calendar", which
    made every gmail run structurally invisible to a caller that did not think to pass a domain — and the
    caller that did not think to pass one was the conversation runtime. It also returns only ONE run, so
    it cannot answer "is there an open goal OF THIS KIND"; selecting by kind is what keeps a calendar goal
    and an email goal from merging into one confused row.
    """
    async with user_session(user_id) as s:
        rows = (await s.execute(
            select(schema.AgentRun)
            .where(schema.AgentRun.user_id == user_id,
                   schema.AgentRun.status.notin_(tuple(_CLOSED_STATUSES)))
            # id breaks a created_at tie so two runs opened in the same instant still have ONE order — a
            # continuation that depended on which row the planner happened to return is not a continuation.
            .order_by(schema.AgentRun.created_at.desc(), schema.AgentRun.id.desc())
            .limit(max(1, limit)))).scalars().all()
    runs = [_row_dict(r) for r in rows]
    if conversation_id is None:
        return runs
    wanted = str(conversation_id)
    return [r for r in runs if _conversation_of(r) in ("", wanted)]


async def open_goal(user_id: UUID, *, conversation_id: str | UUID | None = None) -> dict | None:
    """The newest non-terminal run, any domain — or None. The absence IS the answer twenty-two turns
    never produced."""
    runs = await open_runs(user_id, conversation_id=conversation_id, limit=1 if conversation_id is None else _SCAN_LIMIT)
    return runs[0] if runs else None


def _kind_of(run: Mapping) -> GoalKind | None:
    kind, _ = goal_slots.from_goal_jsonb(run.get("goal") if isinstance(run.get("goal"), Mapping) else {})
    return kind


async def open_goal_of_kind(user_id: UUID, kind: GoalKind | str, *,
                            conversation_id: str | UUID | None = None) -> dict | None:
    """The newest open run whose goal blob declares THIS kind. The selector `ensure_goal` uses, and the
    reason an email goal and a calendar goal can be open at once without exchanging slots."""
    wanted = kind if isinstance(kind, GoalKind) else GoalKind(kind)
    for run in await open_runs(user_id, conversation_id=conversation_id):
        if _kind_of(run) is wanted:
            return run
    return None


async def resolve_capability(user_id: UUID, *, proposed: str | None = None,
                             conversation_id: str | UUID | None = None) -> str:
    """The capability THIS turn is about — the proposal when it is real, otherwise the open goal's.

    The transcript's inline reply of "this" resolved nothing, and the model's own suggestion was the free
    text "sending messages". Both are turns that name no operation while an obvious one is already in
    flight, and both were dropped. A turn that names nothing continues what is open; that is what a
    student means by "this".

    Two refusals keep it safe:
      * a REAL operation id is never rewritten. `gmail.get_message` stays `gmail.get_message`, because
        silently promoting a read into the open goal's send is a far worse failure than dropping a turn.
      * when goals of two different KINDS are open, an unnamed turn resolves to nothing. Guessing between
        an email and a calendar goal is how a recipient ends up as an event title — the one thing
        selection-by-kind exists to prevent. The caller must disambiguate instead.
    """
    cap = (proposed or "").strip()
    if cap and (goal_slots.kind_for_capability(cap) is not None or tool_registry.get(cap) is not None):
        return cap
    kinds = {k for k in (_kind_of(r) for r in await open_runs(user_id, conversation_id=conversation_id))
             if k is not None}
    if len(kinds) == 1:
        return goal_slots.capability_for(next(iter(kinds)))
    return cap


async def is_continuation(user_id: UUID, *, capability: str | None,
                          conversation_id: str | UUID | None = None) -> bool:
    """Whether this turn would CONTINUE an existing goal rather than start one — read from the database,
    which is the point. Hand the result to `should_create_goal` instead of letting a caller (or a model)
    assert that a run exists. Resolve `capability` through `resolve_capability` first, or a turn that
    answers Bruce's question without naming an operation reads as a brand-new conversation."""
    kind = goal_slots.kind_for_capability((capability or "").strip()) if capability else None
    if kind is None:
        return False
    return await open_goal_of_kind(user_id, kind, conversation_id=conversation_id) is not None


# --- 3. the goal as this turn leaves it ------------------------------------------------------------------

@dataclass(frozen=True)
class GoalView:
    """What is durably true about one goal after this turn. Everything a reply or an execution needs, and
    nothing either of them has to re-derive from chat text."""

    run_id: str
    kind: GoalKind
    capability: str
    status: str
    slots: Mapping[str, SlotValue]
    missing: tuple[str, ...]
    ready_to_execute: bool          # every required ARGUMENT is filled. Says nothing about authorization.
    decision_id: str | None = None
    confirmed: bool = False         # a decision exists AND it was approved
    created: bool = False           # this turn opened the run (the caller may want to say so, once)

    @property
    def guessed(self) -> tuple[str, ...]:
        """Required slots the MODEL filled in and nobody asserted. gmail.send_message is reversible=False:
        a guessed address is a letter that cannot be taken back."""
        return goal_slots.guessed_required(self.kind, dict(self.slots))

    def tool_arguments(self) -> dict:
        """Arguments for `capability`, keyed by the tool's own names. Raises while anything is missing —
        a partially filled send is not a degraded send, it is a wrong one."""
        return goal_slots.tool_arguments(self.kind, dict(self.slots))


def unknown_slots(kind: GoalKind | str, slots: Mapping[str, SlotValue] | None) -> tuple[str, ...]:
    """Slot names this kind never declared. Kept rather than dropped (silently discarding an extracted
    value is how information disappears), but countable — an extractor emitting "to" instead of
    "recipient" would otherwise look exactly like a student who never gave an address."""
    declared = {s.name for s in goal_slots.slot_specs(kind)}
    return tuple(n for n in sorted(slots or {}) if n not in declared)


def _coerce(name: str, value: Any) -> SlotValue:
    """Accept a `SlotValue` or its JSON form. Anything else raises, naming the SLOT and never its value —
    a bare string would have to be assigned a provenance, and inventing one turns a model's guess into
    something the student appears to have said."""
    if isinstance(value, SlotValue):
        return value
    if isinstance(value, Mapping):
        parsed = SlotValue.from_json(dict(value))
        if parsed is not None:
            return parsed
    raise TypeError(f"slot {name!r} must be a SlotValue (or its JSON form), not a bare value")


def _stamp(slots: Mapping[str, Any] | None, turn_index: int) -> dict[str, SlotValue]:
    """Tag this turn's values with this turn's index, so merge order cannot change the outcome.

    A value that already carries a non-zero index keeps it: a replay handing over historical turns is
    stating when each was said, and overwriting that would make the last-replayed turn win regardless of
    what the student actually corrected.
    """
    out: dict[str, SlotValue] = {}
    for name, raw in (slots or {}).items():
        sv = _coerce(name, raw)
        out[name] = sv if sv.turn_index else replace(sv, turn_index=turn_index)
    return out


def _decision_block(run: Mapping) -> tuple[str | None, bool]:
    """(decision_id, approved) from `active_decision`. Unreadable or absent -> (None, False)."""
    block = run.get("active_decision")
    if not isinstance(block, Mapping):
        return None, False
    raw_id = block.get("id") or block.get("decision_id")
    decision_id = str(raw_id).strip() if raw_id else None
    state = block.get("status") or block.get("state") or block.get("outcome") or block.get("approved")
    approved = str(getattr(state, "value", state)).strip().lower() in _APPROVED
    return decision_id, bool(decision_id) and approved


def _view(run: Mapping, kind: GoalKind, slots: Mapping[str, SlotValue], *, created: bool) -> GoalView:
    missing = goal_slots.missing_required(kind, dict(slots))
    decision_id, confirmed = _decision_block(run)
    return GoalView(
        run_id=str(run["id"]), kind=kind, capability=goal_slots.capability_for(kind),
        status=str(run.get("status") or ""), slots=MappingProxyType(dict(slots)),
        missing=missing, ready_to_execute=not missing,
        decision_id=decision_id, confirmed=confirmed, created=created)


async def ensure_goal(user_id: UUID, *, capability: str, conversation_id: str | UUID | None,
                      slots_in: Mapping[str, Any] | None, turn_index: int,
                      decision: Any = None) -> GoalView:
    """Create or continue THE goal for `capability`, fold in this turn's slots, persist, and describe it.

    One generic path for every product. The kind, the capability id and (later) the adapter are the only
    things that differ between sending an email and booking an event, so all three are looked up rather
    than branched on, and a third product adds a row to `goal_slots._DECLARED` and nothing here.

    Continuation is by KIND — never "the newest run" — so a student with an open email goal and an open
    calendar goal keeps two clean sets of slots instead of one contaminated one.
    """
    cap = (capability or "").strip()
    kind = goal_slots.kind_for_capability(cap)
    if kind is None:
        # Refuses rather than inventing a kind: a goal whose slots match no tool would ask the student for
        # arguments nothing can consume, which is the free-text-capability defect wearing a schema.
        raise ValueError(f"no goal kind is declared for capability {cap!r}")

    incoming = _stamp(slots_in, turn_index)
    run = await open_goal_of_kind(user_id, kind, conversation_id=conversation_id)

    if run is None:
        merged = goal_slots.merge_slots(None, incoming)
        goal = goal_slots.to_goal_jsonb(_seed_goal(cap, conversation_id, decision), kind, merged)
        # domain is derived from the capability id ("gmail.send_message" -> "gmail"), so a new product does
        # not need a domain branch either. It stays the column the rest of the runtime already filters on.
        created = await agent_run_store.create_run(
            user_id, domain=cap.split(".", 1)[0][:32], goal=goal, status=MachineState.understanding.value)
        return _view(created, kind, merged, created=True)

    existing_goal = run.get("goal") if isinstance(run.get("goal"), Mapping) else {}
    _, known = goal_slots.from_goal_jsonb(dict(existing_goal))
    merged = goal_slots.merge_slots(known, incoming)
    goal = goal_slots.to_goal_jsonb(_carry_forward(dict(existing_goal), cap, conversation_id, decision),
                                    kind, merged)
    if goal != dict(existing_goal):
        # A turn that added nothing must not write: an UPDATE per conversational turn would churn
        # updated_at and make "when did this goal last actually move" unanswerable.
        await agent_run_store.update_run(user_id, UUID(str(run["id"])), goal=goal)
        run = {**run, "goal": goal}
    return _view(run, kind, merged, created=False)


def _seed_goal(capability: str, conversation_id: str | UUID | None, decision: Any) -> dict:
    """The non-slot part of a new goal blob. `desired_outcome` is the model's plain-language proposal —
    the one thing it IS good at — and it is what `turn_context.OpenRun.what` renders, so an open run reads
    as "email Ms. Alvarez a thank-you" instead of a bare uuid."""
    goal: dict = {"action": capability, "capability": capability}
    if conversation_id is not None:
        goal[CONVERSATION_KEY] = str(conversation_id)
    proposed = _proposed_goal(decision)
    if proposed:
        goal["desired_outcome"] = proposed
    return goal


def _carry_forward(goal: dict, capability: str, conversation_id: str | UUID | None, decision: Any) -> dict:
    """Fill in what an existing goal blob is missing without overwriting what it has. A later turn's
    paraphrase of the goal is not a correction of the original one; corrections travel through slots,
    where provenance can adjudicate them."""
    out = dict(goal)
    out.setdefault("action", capability)
    out.setdefault("capability", capability)
    if conversation_id is not None:
        out.setdefault(CONVERSATION_KEY, str(conversation_id))
    proposed = _proposed_goal(decision)
    if proposed and not out.get("desired_outcome"):
        out["desired_outcome"] = proposed
    return out


def _proposed_goal(decision: Any) -> str:
    if decision is None:
        return ""
    raw = (decision.get("proposed_goal") if isinstance(decision, Mapping)
           else getattr(decision, "proposed_goal", None))
    return str(raw).strip() if raw else ""


# --- 4. what happens next --------------------------------------------------------------------------------

ASK_MISSING = "ask_missing"
PROPOSE_CONFIRMATION = "propose_confirmation"
EXECUTE = "execute"
BLOCKED_CAPABILITY = "blocked_capability"

DISPOSITIONS: frozenset[str] = frozenset({ASK_MISSING, PROPOSE_CONFIRMATION, EXECUTE, BLOCKED_CAPABILITY})

# Slot name -> the words a student would recognise. DATA, not a handler: a new kind adds rows here and to
# `goal_slots._DECLARED`, and no code changes. A name with no phrase asks for the name itself — clumsy,
# but it still asks for the right thing, which beats a fluent question about the wrong one.
_ASK_PHRASE: dict[str, str] = {
    "recipient": "who this should go to",
    "subject": "the subject line",
    "body": "what you want it to say",
    "tone": "what tone to use",
    "purpose": "what it's for",
    "title": "what to call it",
    "start": "when it starts",
    "end": "when it ends",
    "timezone": "which timezone that's in",
    "attendees": "who should be there",
}


def clarifying_question(missing: tuple[str, ...] | list[str]) -> str:
    """The question, built from missing slot NAMES and nothing else.

    This is the whole correction. Previously the model decided both whether it had a question and what it
    was, so it asked for a recipient it had been given and a subject it had been told. A question derived
    from `missing_required` cannot ask for a filled slot, because a filled slot is not in the list. Empty
    `missing` returns "" — there is no question, and the model does not get to invent one.
    """
    phrases = [_ASK_PHRASE.get(name, name) for name in (missing or ())]
    if not phrases:
        return ""
    if len(phrases) == 1:
        joined = phrases[0]
    elif len(phrases) == 2:
        joined = f"{phrases[0]} and {phrases[1]}"
    else:
        joined = ", ".join(phrases[:-1]) + f", and {phrases[-1]}"
    return f"I still need {joined}."


@dataclass(frozen=True)
class NextStep:
    """The typed answer to "what now". `target_state` is a PROPOSAL: `transitions.propose_transition`
    still decides whether the run may actually move there, with the guards this module does not hold."""

    disposition: str
    run_id: str
    capability: str
    target_state: MachineState
    missing: tuple[str, ...] = ()
    question: str = ""
    availability_status: str = ""
    decision_id: str | None = None


def _needs_confirmation(view: GoalView) -> bool:
    """Whether a human yes is required before this runs.

    Two independent triggers. Every WRITE needs the student's yes — irreversibility raises the cost of
    being wrong but is not what creates the requirement. And any required slot the model GUESSED needs one
    too, even for a reversible operation: "complete" and "confirmed" are different questions, and an
    address nobody asserted is exactly the case where they diverge. An unknown spec asks — failing closed
    costs a confirmation, failing open costs a letter that cannot be recalled.
    """
    spec = tool_registry.get(view.capability)
    return spec is None or spec.write or bool(view.guessed)


def next_step(view: GoalView, *, availability_ok: bool, availability_status: str = "") -> NextStep:
    """What the runtime should do with this goal now. Pure — the caller resolves availability from
    `tool_broker.availability` and hands in the verdict.

    THE ORDER IS THE FIX. Availability is checked BEFORE missing slots, because collecting a recipient and
    a subject line and a body and only then admitting the account is not connected is precisely the
    exchange that made Bruce feel broken — and it is the reverse of what the transcript did, which was to
    collect nothing and deny the ability at the end.
    """
    if not availability_ok:
        return NextStep(BLOCKED_CAPABILITY, view.run_id, view.capability, MachineState.blocked,
                        availability_status=availability_status or "unavailable",
                        decision_id=view.decision_id)
    if view.missing:
        return NextStep(ASK_MISSING, view.run_id, view.capability, MachineState.preparing,
                        missing=view.missing, question=clarifying_question(view.missing),
                        availability_status=availability_status, decision_id=view.decision_id)
    if _needs_confirmation(view) and not view.confirmed:
        return NextStep(PROPOSE_CONFIRMATION, view.run_id, view.capability, MachineState.awaiting_approval,
                        availability_status=availability_status, decision_id=view.decision_id)
    return NextStep(EXECUTE, view.run_id, view.capability, MachineState.executing,
                    availability_status=availability_status, decision_id=view.decision_id)
