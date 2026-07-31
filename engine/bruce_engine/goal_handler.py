"""The ONE generic handler that turns the brain spine into behaviour a student can see.

WHAT WENT WRONG WITHOUT IT — a real transcript, verified in the database. A student asked Bruce to email
one named address a thank-you note. Turn 2 asked for the recipient turn 1 had supplied. An inline reply of
"this" resolved nothing. The last of twenty-two turns said "i can't send messages for you" while
`tool_broker` was answering ok=True for `gmail.send_message` in the same process. Twenty-two turns, ZERO
missions, ZERO agent_runs.

Layers 1-6 of the spine were built and tested against exactly that transcript — `goal_slots`,
`transitions`, `turn_context`, `continuation`, `goal_runtime`, `memory_finalize` — and then nothing called
them. A green suite over an unwired module changes nothing a student experiences. This file is the call.

WHAT IS DELIBERATELY GENERIC, AND WHY THAT IS THE POINT

There is no email handler here and no calendar handler here. `evaluate` and `execute` contain no product
branch at all: the goal kind, the slot set, the tool arguments, the executor and the copy are all LOOKED
UP by capability id. A third product is a row in `goal_slots._DECLARED`, a row in `_EXECUTORS` and a row
in `_COPY`; nothing in the control flow below changes. The previous shape — one handler per capability,
each re-deriving "what is still missing" from chat prose — is how three views of one turn came to exist,
and how the student got the least informed one.

WHAT THE MODEL IS NOT ALLOWED TO DO

  * It does not decide whether durable state exists. `decision.needs_mission` is never read; that is the
    field which said "no" twenty-two times. `goal_runtime.creation_verdict` decides, from the intent plus
    a REAL registry operation id plus what is already open.
  * It does not create runs, set statuses, authorize anything, execute anything, or declare completion.
    Every status write goes through `agent_run_store.update_run`, which refuses whatever
    `transitions.propose_transition` rejects. Completion is `agent_loop`'s verified read-back and nothing
    else.
  * It MAY propose slot values, and they are stored as `Source.model_derived`, so a guess never
    overwrites something the student stated and `next_step` can insist on a confirmation for a recipient
    nobody actually asserted.

WHY EXECUTION IS BOUND TO A CAPABILITY, NOT TO A HANDLER

`_EXECUTORS` maps a capability id onto the `CapabilityExecutor` that already knows how to perform and
VERIFY it. It has two rows — `gmail.send_message` and `calendar.create_event` — and the second one is
worth remembering, because it was MISSING for the whole of D3 while its executor sat written and tested
in `calendar_executor`. The declared slot set existed, the executor existed, and the row joining them did
not, so this handler declined every calendar turn with `NO_EXECUTOR` and a `schedule_event` goal could
never come into being. Nothing was broken; a table had a hole in it, and the hole was invisible from
either side.

The refusal itself stays, and it is deliberate rather than an omission: a slot-bearing capability with no
executor is declined UP FRONT, because collecting a title, a start and a timezone and only then admitting
nothing can perform the call is the precise exchange this workstream exists to delete. A third product is
a row in `goal_slots._DECLARED`, a row here and a row in `_COPY` — no code.

Nothing in this module logs a slot value, a recipient, a subject or a body. Every one of those is message
content. Ids, capability names, dispositions, statuses and counts only.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from . import continuation as continuation_mod
from . import goal_runtime, goal_slots, temporal, tool_registry, transitions
from .goal_slots import GoalKind, SlotValue, Source

log = logging.getLogger("bruce.goal_handler")   # CONTENT-FREE: ids, capabilities, dispositions, statuses

_KILL_ON = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """Whether the goal seam is live. `BRUCE_GOAL_SPINE_OFF` turns it off without a deploy.

    A switch exists for the same reason `execution_gate.armed()` has one: a path that cannot be turned
    off during an incident is a path that gets reverted during an incident, and a revert loses the
    reasoning with it. The default is ON — the entire point of this branch is that the spine is live.
    """
    return os.environ.get("BRUCE_GOAL_SPINE_OFF", "").strip().lower() not in _KILL_ON


# --- why the handler did or did not take the turn ---------------------------------------------------------
# Typed and closed, like `transitions.REASONS` and `goal_runtime.REASONS`. "How often did the goal seam
# decline, and for which reason" has to be COUNTABLE — the transcript could not answer that question,
# because every refusal was a sentence in a reply rather than a value anywhere.

OFF = "goal_spine_off"
MISSION_LANE = "background_mission_owns_turn"
NO_EXECUTOR = "capability_has_no_executor"
UNTOUCHED = "open_goal_untouched_by_this_turn"
AMBIGUOUS_GOAL = "several_open_goals_and_the_turn_names_none"

DECLINE_REASONS: frozenset[str] = frozenset(
    {OFF, MISSION_LANE, NO_EXECUTOR, UNTOUCHED} | goal_runtime.REASONS)


# --- capability -> the executor that can actually perform and verify it ------------------------------------

def _gmail_send(arguments: dict, *, idempotency_key: str, adapter=None):
    """`gmail.send_message`. The slot set is derived from this tool's own `arg_schema`, so
    `GoalView.tool_arguments()` already hands back exactly the keyword arguments the executor takes —
    there is no translation layer here, and therefore nothing here that can drift from the registry."""
    from .gmail_executor import GmailSendExecutor
    return GmailSendExecutor(idempotency_key=idempotency_key, adapter=adapter, **arguments)


def _calendar_create(arguments: dict, *, idempotency_key: str, adapter=None):
    """`calendar.create_event`. The row that was missing, and the whole of defect D3.

    `goal_slots` declared a full slot set for this capability and this map had no entry for it, so the
    handler DECLINED every calendar turn with `NO_EXECUTOR` and a `schedule_event` goal could never exist
    — which meant every calendar acceptance criterion (one goal id across a move, a replacement Decision,
    attendees retained) had nothing live to hold. `CalendarCreateExecutor` was written and tested at
    `565024b` and never wired.

    `attendees` is deliberately absent from `arguments`: it is a slot with no `tool_arg`, so
    `GoalView.tool_arguments()` never emits it and the executor's `undeliverable()` refusal never fires
    from this lane. That is the honest shape — the list is collected and remembered, and it does not reach
    Google until `execution_gate.calendar_create_args` and `models.CalendarEvent` gain somewhere to put it.
    """
    from .calendar_executor import CalendarCreateExecutor
    return CalendarCreateExecutor(idempotency_key=idempotency_key, adapter=adapter, **arguments)


_EXECUTORS: dict[str, Any] = {
    "gmail.send_message": _gmail_send,
    "calendar.create_event": _calendar_create,
}


def executable(capability: str) -> bool:
    """Whether a capability can be carried all the way to a VERIFIED provider call. Checked before the
    handler claims anything, so a goal is never opened for work that would stall after the last
    question."""
    return capability in _EXECUTORS


# --- capability -> the words a student reads ---------------------------------------------------------------
# DATA, not branches. `proposal` / `receipt` / `memory` are formatted with the goal's own slot values; a
# name with no value renders empty rather than raising, because a sentence with a gap in it is recoverable
# and an exception in the reply path is not.

@dataclass(frozen=True)
class _Copy:
    noun: str                    # what the student would call the thing ("email")
    verb: str                    # what Bruce would be doing ("send")
    past: str                    # the same, done ("sent") — so a failure line reads like English
    proposal: str                # shown BEFORE the yes, and fingerprinted, so consent binds to it
    receipt: str                 # said ONLY after a verified read-back
    memory: str                  # the durable fact, written only after that same read-back


_COPY: dict[str, _Copy] = {
    "gmail.send_message": _Copy(
        noun="email", verb="send", past="sent",
        proposal=("ok here's the email, going to {recipient}:\n\n"
                  "subject: {subject}\n\n{body}\n\nwant me to send it?"),
        receipt="sent it to {recipient} ✅",
        memory="emailed {recipient} about {subject}"),
    # `{when}` is rendered by `_values` from whichever slots this kind declares as its moment, so the
    # sentence says "friday at 4pm" rather than an ISO timestamp. Data, like every other row here.
    "calendar.create_event": _Copy(
        noun="event", verb="add", past="added",
        proposal="ok, {title} {when}. want me to put it on ur calendar?",
        receipt="{title} is on ur calendar {when} ✅",
        memory="put {title} on the calendar {when}"),
}

_FALLBACK_COPY = _Copy(noun="that", verb="do", past="done",
                       proposal="want me to go ahead with that?", receipt="done ✅",
                       memory="completed {capability}")


class _Blank(dict):
    """`format_map` over a mapping that answers "" for anything absent."""

    def __missing__(self, key):
        return ""


def _render(template: str, values: dict) -> str:
    try:
        return template.format_map(_Blank(values))
    except Exception:
        return template


def _lower_first(text: str) -> str:
    """Bruce writes in lowercase. `goal_runtime.clarifying_question` builds a sentence, not a voice."""
    return (text[:1].lower() + text[1:]) if text else text


# --- reading this turn's slot values -----------------------------------------------------------------------

def entity_slots(kind: GoalKind, entities) -> dict[str, str]:
    """Model-proposed slot values, matched to the KIND'S OWN declared slot names.

    Generic by construction: the rule is "does this entity's type name one of this goal's slots", judged
    against `goal_slots.slot_specs`, so a new product's entities land in its new slots without a line
    here. The model emitted `required_capabilities=["sending messages"]` in the transcript, free text that
    joined to nothing; an entity type that joins to no declared slot is dropped for the same reason, and
    dropping it is safe precisely because dropping it means Bruce ASKS rather than guesses.
    """
    specs = goal_slots.slot_specs(kind)
    out: dict[str, str] = {}
    for e in entities or ():
        raw = re.sub(r"[^a-z0-9]+", "_", str(getattr(e, "type", "") or "").lower()).strip("_")
        if not raw:
            continue
        parts = {t for t in raw.split("_") if t}
        for spec in specs:
            name = spec.name
            if name in out:
                continue
            # THE DECLARED ALIAS IS CHECKED FIRST and matched WHOLE. Token overlap alone lost a live
            # recipient: the model labelled the address `email`, the slot is `recipient`, and those two
            # words share nothing — so a perfectly extracted address was dropped between the model and
            # the goal, and Bruce asked for what it had just been given. The alias list lives on the slot
            # (`goal_slots._SlotDecl.entity_aliases`) so it cannot drift from the thing it names.
            if raw == name or raw in spec.entity_aliases or (
                    {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t} & parts):
                value = getattr(e, "normalized", None) or getattr(e, "value", None)
                if value:
                    out[name] = str(value)
                break
    return out


def _when_phrase(kind: GoalKind | None, values: dict, *, timezone_name: str = "") -> str:
    """"friday at 4pm" for whatever moment this kind declares, or "" for a kind that has none.

    Generic by the same rule as everything else here: the moment is found through
    `goal_slots.temporal_pair`, off the schema, so an email goal renders an empty `{when}` rather than
    needing a branch. A student confirming an irreversible-ish write should be reading the time in their
    own words — an ISO timestamp in a confirmation is a thing nobody checks.

    `timezone_name` is the STUDENT'S, and it is here for one reason: `human_when` says "today" and
    "tomorrow" relative to now, and now is a different day in Chicago than it is in Auckland. A
    confirmation that called tonight "tomorrow" would be wrong in the one sentence the student is being
    asked to check.
    """
    if kind is None:
        return ""
    start_name, end_name = goal_slots.temporal_pair(kind)
    start = values.get(start_name) if start_name else None
    if not start:
        return ""
    from .calendar_schedule import human_when
    from .models import CalendarEvent
    end = values.get(end_name) if end_name else None
    try:
        event = CalendarEvent(title="x", start=str(start), end=str(end) if end else None)
        return human_when(event, now=_now_in(timezone_name) if timezone_name else None)
    except Exception:
        # A sentence with a gap in it is recoverable; an exception in the reply path is not.
        return ""


def _now_in(timezone_name: str, now: datetime | None = None) -> datetime:
    from . import calendar_schedule
    try:
        zone = ZoneInfo(timezone_name)
    except Exception:
        zone = ZoneInfo(calendar_schedule.DEFAULT_TZ)
    return (now or datetime.now(zone)).astimezone(zone)


def resolve_temporal(kind: GoalKind, incoming: dict[str, SlotValue], *, timezone_name: str,
                     now: datetime | None = None) -> dict[str, SlotValue]:
    """Turn a when-PHRASE the student typed into a real moment, and rewrite its END in the same breath.

    Two rules, both of which have gone wrong somewhere before:

      * The zone is the STUDENT'S (`world_state.resolve_timezone`), never the process's. "next tuesday at
        3" for a Central student is not 3pm Pacific, and a resolver that guessed a zone would place every
        event hours off with no visible error at all.
      * `start` and `end` move TOGETHER. `continuation.slot_patch` is replacement-shaped and can only name
        one slot, so patching a start alone would leave the previous end sitting beside it — an event that
        starts on Friday and ends on Tuesday. The derived end is written here at the same provenance and
        the same turn index, which is what lets `merge_slots` prefer it over the stale one.

    A phrase `temporal.resolve` cannot read is left EXACTLY as the student typed it, so Bruce asks one
    question instead of sending an invented moment to a provider.
    """
    start_name, end_name = goal_slots.temporal_pair(kind)
    if start_name is None:
        return dict(incoming)
    sv = incoming.get(start_name)
    if sv is None or not sv.filled or not isinstance(sv.value, str):
        return dict(incoming)
    resolved = temporal.resolve(sv.value, now=_now_in(timezone_name, now))
    if resolved is None:
        return dict(incoming)
    out = dict(incoming)
    out[start_name] = replace(sv, value=resolved.start)
    if end_name:
        out[end_name] = SlotValue(resolved.end, sv.source, turn_id=sv.turn_id, turn_index=sv.turn_index)
    return out


_ADDRESS = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def address_from_trusted_text(kind: GoalKind, text: str | None) -> dict[str, SlotValue | None]:
    """An address the student TYPED, for a slot that takes one and does not have one yet.

    THE MODEL IS NOT THE ONLY WAY TO KNOW. When the address is sitting in the student's own sentence,
    asking for it is indefensible however the extraction went wrong — that exact re-ask is the transcript's
    signature failure and it has now been reached by three different routes. This closes the last one by
    not depending on the reading at all.

    Three deliberate limits:

      * TRUSTED TEXT ONLY, through `decision_resolver`'s own separation plus inline-quote stripping. An
        address inside a forwarded email or a pasted line is somebody else's correspondent, and letting it
        become the recipient is the shape of defect the authorization corpus is full of.
      * EXACTLY ONE address, or nothing. Two addresses in one sentence is a question ("which of them?"),
        and picking the first would be a guess about where mail goes.
      * `user_stated`, because the student typed it — which is the truth, and which lets it correct a
        model guess through `merge_slots` rather than losing to one.

    It never overwrites: the caller applies it only to a slot that is still empty.
    """
    spec = next((s for s in goal_slots.slot_specs(kind) if s.name == "recipient"), None)
    if spec is None:
        return {}
    from . import decision_resolver
    trusted = decision_resolver.strip_inline_quotes(decision_resolver.trusted_reply_text(text))
    found = list(dict.fromkeys(_ADDRESS.findall(trusted)))
    return {"recipient": found[0]} if len(found) == 1 else {}


def turn_slots(kind: GoalKind, *, continuation, decision, turn_id: str | None, turn_index: int,
               timezone_name: str, now: datetime | None = None,
               trusted_text: str | None = None) -> dict[str, SlotValue]:
    """Everything this turn says about the goal, with provenance attached.

    The student's own words arrive through `continuation.slot_patch` and are `user_stated`; the model's
    reading of the message arrives through `extracted_entities` and is `model_derived`. Both are written
    and only the first can overwrite the other — `goal_slots.merge_slots` enforces that ordering and it is
    not restated here. What matters at this call site is that the two are never CONFUSED, because
    `guessed_required` is what makes Bruce stop and ask before mailing an address nobody typed.
    """
    values: dict[str, SlotValue] = {}
    for name, value in (entity_slots(kind, getattr(decision, "extracted_entities", ())) or {}).items():
        values[name] = SlotValue(value, Source.model_derived, turn_id=turn_id, turn_index=turn_index)
    # The address the student TYPED, for a recipient the reading did not produce. `user_stated`, because
    # they typed it — so it outranks a model guess rather than losing to one, and Bruce never asks for
    # something that is sitting in the sentence it is answering.
    for name, value in address_from_trusted_text(kind, trusted_text).items():
        if not (values.get(name) is not None and values[name].filled):
            values[name] = SlotValue(value, Source.user_stated, turn_id=turn_id, turn_index=turn_index)
    patch = dict(getattr(continuation, "slot_patch", {}) or {}) if continuation is not None else {}
    for name, value in patch.items():
        values[name] = SlotValue(value, Source.user_stated, turn_id=turn_id, turn_index=turn_index)
    return resolve_temporal(kind, values, timezone_name=timezone_name, now=now)


# --- the canonical pending Decision ------------------------------------------------------------------------
# ONE shape, on the run itself (`agent_runs.active_decision`), for every product. `goal_runtime._view`
# reads it to answer `confirmed`, `continuation._open_decision_id` reads it to bind a "yes", and
# `transitions._guard_awaiting_approval` refuses the state unless it exists. Three consumers, one row —
# rather than a decision that exists only inside a sentence Bruce said, which is all "did that go through?"
# ever had to be answered from.

PENDING = "pending"
APPROVED = "approved"


def decision_block(*, decision_id: str, capability: str, fingerprint: str, question: str,
                   run_id: str, status: str = PENDING, source_message_id: str | None = None) -> dict:
    """`source_message_id` is the turn the proposal was SHOWN on, and it is what makes an inline reply to
    that proposal resolvable. Without it, `goal_selection` could only point a reply at a goal through slot
    provenance — true whenever the proposing turn also stated a value, and an accident when it did."""
    return {"decision_id": decision_id, "status": status, "capability": capability,
            "arguments_fingerprint": fingerprint, "question": question, "run_id": str(run_id),
            "source_message_id": str(source_message_id) if source_message_id else None}


def _fingerprint(provider: str, operation: str, arguments: dict) -> str:
    """The binding between a proposal and the call it will become. Taken over the arguments that would
    actually reach the provider, so a goal edited between the offer and the yes fails the check instead of
    sending the old draft to the old address."""
    from . import authorization_evidence as ae
    return ae.fingerprint(ae.normalize_arguments(provider, operation, arguments))


def idempotency_key(run_id: str, capability: str) -> str:
    """The exactly-once key for one goal's one operation.

    Derived from the RUN, which is durable, rather than from anything held in memory. That is what makes a
    repeated confirmation, a concurrent confirmation, a worker retry and a process restart all resolve to
    the same attempt: the adapter's marker ledger collapses them to the one message already sent, and
    `agent_run_store.create_run` collapses the audit run the same way.
    """
    return f"goal:{run_id}:{capability}"


# --- reading the turn's state (all of it fetched ONCE, by the runtime) -------------------------------------

def _goal_kind_of(run) -> GoalKind | None:
    goal = run.get("goal") if isinstance(run, dict) else None
    kind, _ = goal_slots.from_goal_jsonb(goal if isinstance(goal, dict) else None)
    return kind


def turn_index_for(octx) -> int:
    """A STRICTLY INCREASING position for this turn's slot values, per goal.

    The runtime hands over its position in the conversation, and that number saturates: `load_recent_turns`
    is a bounded window of 8, so from the ninth turn onward every turn would claim the same index. That
    matters because `goal_slots._rank` breaks a same-trust conflict by recency — with a frozen index, "no,
    send it to the other address" on turn 15 would tie with the address stated on turn 12 and be settled by
    an arbitrary (if deterministic) tie-break instead of by the correction. So the goal's own highest index
    is the floor, which is monotone for as long as the goal lives no matter how long the conversation gets.
    """
    goal = (getattr(octx, "open_goal", None) or {}).get("goal")
    _kind, slots = goal_slots.from_goal_jsonb(goal if isinstance(goal, dict) else None)
    highest = max((sv.turn_index for sv in slots.values() if sv is not None), default=0)
    return max(int(getattr(octx, "turn_index", 0) or 0), highest + 1)


def _proposed_capability(decision) -> str:
    """The capability the model named, CANONICALIZED onto a registry id.

    `tool_registry.canonical` is the one place that knows the model says "email" where the registry says
    "gmail". Without it a perfectly good turn — right intent, right entities, right operation — dies at
    `creation_verdict` with `capability_not_in_registry`, and the student gets a promise instead of a
    Decision. Canonicalization cannot invent a capability: an id the registry does not know comes back
    unchanged and is refused exactly as before.
    """
    for c in (getattr(decision, "required_capabilities", None) or ()):
        text = str(c or "").strip()
        if text:
            return tool_registry.canonical(text)
    return ""


def capability_for_turn(decision, open_goal) -> str:
    """The capability THIS turn is about.

    `goal_runtime.resolve_capability` answers the same question against the database; this answers it
    against the run the RUNTIME already fetched, because a handler that went back to the database would be
    building a second view of the turn — the defect itself, one call frame later. Both refusals are kept,
    and they are the reason this is not simply "whatever is open":

      * a REAL operation id is never rewritten, so `gmail.get_message` is not silently promoted into the
        open goal's send;
      * a turn that names nothing continues what is open, which is what a student means by "this".
    """
    proposed = _proposed_capability(decision)
    if proposed and (goal_slots.kind_for_capability(proposed) is not None
                     or tool_registry.get(proposed) is not None):
        return proposed
    kind = _goal_kind_of(open_goal)
    return goal_slots.capability_for(kind) if kind is not None else proposed


# Continuation verdicts that resolved to nothing but are still ABOUT the open goal. Each is a question
# Bruce owes the student: an affirmative with no pending decision (the stale-yes guard), an amendment this
# goal owns no slot for, a pointer with nothing to point at. Handing those to the model is how "send it"
# became a friendly sentence and no work at all.
_ABOUT_THE_GOAL: frozenset[str] = frozenset({
    continuation_mod.AFFIRMATIVE_WITHOUT_DECISION,
    continuation_mod.AMEND_WITHOUT_SLOT,
    continuation_mod.NO_ANCHOR,
})


# --- writing the parts the student did not dictate --------------------------------------------------------
#
# `Fill.generatable` says a slot may be WRITTEN from the objective. This is the thing that writes it. It is
# a seam for the same reason `directive_scope.ScopeReader` is: the composition is a model call, the model
# is wrong some fraction of the time, and every test in this tree has to be able to say exactly what came
# back. What it produces is `model_derived` by construction, so `goal_runtime._needs_confirmation` puts it
# on screen and the student reads it before anything is sent — which is the entire reason writing it is
# safe. A composer that failed silently would be worse than none, so an unavailable one leaves the slots
# empty and the turn falls back to asking.

_COMPOSE_TIMEOUT_S = float(os.environ.get("BRUCE_COMPOSE_TIMEOUT_S", "8.0"))


class DraftComposer:
    """The production composer: one structured call, hard deadline, no retry."""

    name = "model"

    def __init__(self, *, timeout_s: float | None = None) -> None:
        self._timeout = timeout_s or _COMPOSE_TIMEOUT_S
        self._agent = None
        self.calls = 0

    def _build(self):
        from pydantic import BaseModel, Field
        from pydantic_ai import Agent, NativeOutput
        from pydantic_ai.models.openai import OpenAIChatModelSettings

        from . import llm

        class _Draft(BaseModel):
            subject: str = Field("", max_length=200)
            body: str = Field("", max_length=4000)

        self._agent = Agent(
            llm.routing_model(), output_type=NativeOutput(_Draft),
            system_prompt=(
                "You write ONE short email for a student, from their own stated objective.\n"
                "You are not deciding whether to send it and you never mention that you are an AI.\n"
                "Write the way the student would: plain, warm, specific, no corporate filler, no em "
                "dashes. Keep the body under 120 words unless the objective needs more.\n"
                "The SUBJECT is a real subject line, not a restatement of the objective.\n"
                "Honour the stated tone. If a relationship is given (a teacher, a coach), match the "
                "register: professional for an adult in authority, warm for a friend.\n"
                "Invent NO facts. If you do not know a detail, leave it out rather than making it up — "
                "a student will read this before it is sent and a fabricated detail is what they will "
                "have to catch."),
            model_settings=OpenAIChatModelSettings(max_tokens=700))
        return self._agent

    async def compose(self, *, objective: str, recipient: str, tone: str = "",
                      context: str = "") -> dict:
        import asyncio
        agent = self._agent or self._build()
        body = (f"OBJECTIVE: {objective}\nRECIPIENT: {recipient}"
                + (f"\nTONE: {tone}" if tone else "")
                + (f"\nCONTEXT FROM THE CONVERSATION: {context}" if context else ""))
        self.calls += 1
        try:
            res = await asyncio.wait_for(agent.run(body), timeout=self._timeout)
        except Exception:
            log.info("draft_compose_failed")        # NO exception text: it can echo the message
            return {}
        out = res.output
        subject = str(getattr(out, "subject", "") or "").strip()
        text = str(getattr(out, "body", "") or "").strip()
        return {k: v for k, v in (("subject", subject), ("body", text)) if v}


_DEFAULT_COMPOSER: DraftComposer | None = None


def default_composer() -> DraftComposer:
    global _DEFAULT_COMPOSER
    if _DEFAULT_COMPOSER is None:
        _DEFAULT_COMPOSER = DraftComposer()
    return _DEFAULT_COMPOSER


def compose_objective(view, msg_text: str | None) -> str:
    """WHAT the draft is about, from the goal rather than from the raw turn.

    `purpose` is the slot the objective lands in and it is preferred over the message text: by the time a
    draft is composed the objective may have been stated three turns ago, and re-reading only the latest
    message is how "make it shorter" became a brand-new subjectless request in the transcript.
    """
    for name in ("purpose", "subject"):
        sv = view.slots.get(name)
        if sv is not None and sv.filled:
            return str(sv.value)
    from . import decision_resolver
    return decision_resolver.trusted_reply_text(msg_text).strip()


def _recent_context(octx) -> str:
    """A few lines of the conversation, for a draft that has to sound like it belongs in it.

    Bounded and TRUSTED-ONLY. A composer handed a forwarded email would happily quote a stranger's words
    back into a message the student is about to send, and the student would have to notice. The capsule
    is the runtime's already-assembled view; anything it cannot give up cheaply is simply not context.
    """
    capsule = getattr(octx, "capsule", None)
    turns = getattr(capsule, "recent_turns", None) or []
    lines: list[str] = []
    for turn in list(turns)[-4:]:
        role = str(getattr(turn, "role", "") or "")
        text = str(getattr(turn, "text", "") or "").strip()
        if role and text:
            lines.append(f"{role}: {text[:200]}")
    return "\n".join(lines)[:1200]


def _ambiguous(octx) -> bool:
    """Did the runtime fail to work out WHICH open goal this turn is about?

    Read off the selection the runtime already made rather than re-derived here. A handler that decided
    this for itself would be a second answer to "which goal", and two answers to that question is the
    defect the whole selection order exists to remove.
    """
    selection = getattr(octx, "goal_selection", None)
    return bool(selection is not None and getattr(selection, "ambiguous", False))


class GoalHandler:
    """The generic goal outcome: create or continue ONE AgentRun-backed goal, ask for exactly what is
    missing, put the operation on screen, and execute it once the student says yes.

    Priority 90, above every existing handler, because an answer that advances work already in flight
    outranks starting anything new and because a turn naming a real operation id has already said what it
    wants. It claims NARROWLY: only for a capability that has a declared slot set AND a CapabilityExecutor
    behind it, so every calendar turn still belongs to the calendar handlers.

    `evaluate` performs no I/O at all. Every piece of state it reads was fetched once by the runtime and
    handed over on the OutcomeContext, which is the invariant the whole snapshot design exists for.
    """

    name = "goal"
    priority = 90

    def __init__(self, *, adapter=None, composer=None) -> None:
        # Injection seam for the provider, exactly like `GmailSendExecutor(adapter=...)`. Never set in
        # production (`default_handlers()` passes nothing); a test hands in a fake so the send is real
        # Bruce code against a fake Google rather than a mocked Bruce.
        self._adapter = adapter
        # The same seam for the DRAFT. A test says exactly what came back from composition, so a
        # production-surface assertion is about Bruce's behaviour and not about the model's mood.
        self._composer = composer

    # --- phase 1: pure -------------------------------------------------------------------------------------

    def _verdict(self, octx) -> tuple[str, str, goal_runtime.CreationVerdict | None]:
        """(capability, reason, verdict) — shared by evaluate and execute so the two cannot disagree about
        what this turn is."""
        capability = capability_for_turn(octx.decision, getattr(octx, "open_goal", None))
        if not capability:
            return "", goal_runtime.NO_CAPABILITY, None
        if goal_slots.kind_for_capability(capability) is not None and not executable(capability):
            return capability, NO_EXECUTOR, None
        kind = _goal_kind_of(getattr(octx, "open_goal", None))
        continues = kind is not None and kind is goal_slots.kind_for_capability(capability)
        verdict = goal_runtime.creation_verdict(
            decision=octx.decision, capability=capability, continuation=continues)
        return capability, verdict.reason, verdict

    def _advances(self, octx) -> bool:
        """Does this turn actually move the open goal, or is it conversation happening beside it?

        A goal stays open across turns that have nothing to do with it, and claiming every one of those
        would turn "what's 8x7" into an interrogation about a subject line. So a continuation claims only
        when the turn resolves against the goal, is plainly about it, or carries a value for it.
        """
        cont = getattr(octx, "continuation", None)
        if cont is not None and (cont.resolved or cont.evidence in _ABOUT_THE_GOAL):
            return True
        kind = _goal_kind_of(getattr(octx, "open_goal", None))
        return bool(kind is not None
                    and entity_slots(kind, getattr(octx.decision, "extracted_entities", ())))

    async def evaluate(self, octx):
        from . import conversation_outcomes as co

        def decline(reason: str, **telemetry):
            return co.HandlerVerdict(disposition=co.Disposition.decline, priority=self.priority,
                                     reason=reason, telemetry=telemetry)

        if not enabled():
            return decline(OFF)
        if getattr(octx, "mission_lane_ran", False):
            # The background-mission lane already acted on this turn and may already have SENT something.
            # Two lanes proposing the same send is how a student gets the same email twice.
            return decline(MISSION_LANE)
        if _ambiguous(octx):
            # Two goals are open and this turn names neither. CLAIMING rather than declining is the point:
            # the turn is about work in flight, so leaving it to the model would produce a friendly answer
            # about neither goal — the transcript's own failure — and guessing would let a bare "no"
            # cancel the one the student was not talking about. One question, no goal, no write.
            return co.HandlerVerdict(disposition=co.Disposition.claim, priority=self.priority,
                                     reason=AMBIGUOUS_GOAL,
                                     telemetry=getattr(octx, "goal_selection").telemetry)
        capability, reason, verdict = self._verdict(octx)
        if verdict is None or not verdict.create:
            return decline(reason, capability=capability or None)
        if reason == goal_runtime.CONTINUATION and not self._advances(octx):
            return decline(UNTOUCHED, capability=capability)
        cont = getattr(octx, "continuation", None)
        return co.HandlerVerdict(
            disposition=co.Disposition.claim, priority=self.priority, reason=reason,
            telemetry={"capability": capability, "creation_reason": reason,
                       "continuation": None if cont is None else cont.kind.value,
                       "evidence": None if cont is None else cont.evidence})

    # --- phase 2: the only place anything is written --------------------------------------------------------

    async def execute(self, octx):
        from . import conversation_outcomes as co
        try:
            return await self._execute(octx)
        except Exception:
            # A handler fault must never cost the student their reply, and must never be reported as work
            # that happened. Nothing here has claimed a send: the provider call is the last step, and its
            # own result is the only thing a receipt is ever written from.
            log.exception("goal_handler_failed")
            return co.HandlerOutput(
                text="something broke on my end there, so i didn't do anything. mind saying that again?",
                styled=False)

    async def _execute(self, octx):
        from . import calendar_schedule, conversation_outcomes as co, world_state

        selection = getattr(octx, "goal_selection", None)
        if _ambiguous(octx):
            # BEFORE `ensure_goal`, and that ordering is the whole guarantee: nothing below this line runs,
            # so the turn creates no goal, writes no slot, mints no authorization and touches no provider.
            log.info("goal_ambiguous candidates=%d", len(selection.candidates))
            return co.HandlerOutput(text=selection.question, styled=False)

        capability, reason, verdict = self._verdict(octx)
        kind = goal_slots.kind_for_capability(capability)
        if kind is None or verdict is None or not verdict.create:   # defense in depth; evaluate gated it
            return co.HandlerOutput(text=octx.decision.user_visible_response, styled=True)

        cont = getattr(octx, "continuation", None)
        conversation_id = getattr(octx, "conversation_id", "") or None
        turn_index = turn_index_for(octx)
        tz = await world_state.resolve_timezone(octx.user_id, default=calendar_schedule.DEFAULT_TZ)
        slots_in = turn_slots(kind, continuation=cont, decision=octx.decision, turn_id=octx.pmid,
                              turn_index=turn_index, timezone_name=tz,
                              # The student's OWN words. `address_from_trusted_text` re-applies the
                              # trusted separation itself, so a forwarded correspondent can never become
                              # the recipient just because the model missed the real one.
                              trusted_text=getattr(octx.msg, "text", None))

        view = await goal_runtime.ensure_goal(
            octx.user_id, capability=capability, conversation_id=conversation_id, slots_in=slots_in,
            turn_index=turn_index, decision=octx.decision)
        log.info("goal run=%s cap=%s created=%s missing=%d reason=%s", view.run_id, capability,
                 view.created, len(view.missing), reason)

        if cont is not None and cont.kind is continuation_mod.ContinuationKind.reject:
            return await self._cancel(octx, view, capability)

        # A refusal is answered above; from here the run is going to keep going, so it is put in the state
        # that says so. `understanding -> preparing` is the only move needed: every disposition below is a
        # legal edge out of `preparing`, and jumping straight from `understanding` to `awaiting_approval`
        # is refused by the machine on purpose ("approval is approval OF something").
        view = await self._ensure_preparing(octx, view)

        block = await self._decision_block(octx, view)
        if cont is not None and cont.kind is continuation_mod.ContinuationKind.confirm:
            view, block = await self._approve(octx, view, capability, block)

        available, status = await self._availability(octx.user_id, capability)
        step = goal_runtime.next_step(view, availability_ok=available, availability_status=status)
        log.info("goal_step run=%s cap=%s disposition=%s status=%s", view.run_id, capability,
                 step.disposition, status)

        if step.disposition == goal_runtime.BLOCKED_CAPABILITY:
            return await self._blocked(octx, view, capability, status)
        if step.disposition == goal_runtime.ASK_MISSING:
            # A RESOLVABLE gap gets one lookup before it becomes a question. `Fill.resolvable` has always
            # said a recipient may be looked up and never invented; `people` is the thing that looks, and
            # it answers with exactly one person or with a question of its own.
            view, step = await self._resolve_people(octx, view, capability, step,
                                                    turn_index=turn_index, available=available,
                                                    status=status)
            if step.disposition == goal_runtime.ASK_MISSING:
                return co.HandlerOutput(text=_lower_first(step.question), styled=False)
            block = await self._decision_block(octx, view)
        if step.disposition == goal_runtime.COMPOSE:
            view = await self._compose(octx, view, capability, step, turn_index=turn_index, tz=tz)
            # Re-asked, not assumed. A composer that returned nothing leaves the slots empty and this
            # lands on ASK_MISSING, which is the honest outcome — Bruce says what it still needs rather
            # than proposing an email with a blank subject.
            step = goal_runtime.next_step(view, availability_ok=available, availability_status=status)
            log.info("goal_step_after_compose run=%s disposition=%s", view.run_id, step.disposition)
            if step.disposition == goal_runtime.ASK_MISSING:
                return co.HandlerOutput(text=_lower_first(step.question), styled=False)
            block = await self._decision_block(octx, view)
        if step.disposition == goal_runtime.PROPOSE_CONFIRMATION:
            return await self._propose(octx, view, capability, block, tz=tz)
        return await self._run(octx, view, capability, block, availability_status=status, tz=tz)

    # --- dispositions ---------------------------------------------------------------------------------------

    async def _resolve_people(self, octx, view, capability, step, *, turn_index: int,
                              available: bool, status: str):
        """Fill a RESOLVABLE gap from people the student has introduced, or leave the question standing.

        The lookup runs only for slots declared `Fill.resolvable` and only when they are what is blocking
        — a `stated` gap is never resolved, because nothing may look up a moment the student has to give.
        A resolution that finds nobody, or finds two, leaves the gap exactly where it was and Bruce asks;
        the question it asks names PEOPLE rather than addresses, which is the only useful form of it.

        The resolved address is `Source.tool_result`: an observation, not a guess, and not the student
        asserting it again this turn. That ranks it above a model value and below anything they type,
        which is what lets "no, send it to the other one" correct it.
        """
        from . import people as people_mod
        gaps = goal_slots.classify_fill(view.kind, step.missing)
        if "recipient" not in gaps[goal_slots.Fill.resolvable]:
            return view, step
        try:
            res = await people_mod.resolve(octx.user_id, getattr(octx.msg, "text", None),
                                           recent_text=_recent_context(octx))
        except Exception:
            log.info("people_resolve_failed run=%s", view.run_id)
            return view, step
        log.info("goal_recipient_resolution run=%s status=%s", view.run_id, res.status)
        if not res.resolved:
            return view, replace(step, question=people_mod.clarifying_question(res))
        view = await goal_runtime.ensure_goal(
            octx.user_id, capability=capability,
            conversation_id=getattr(octx, "conversation_id", "") or None,
            slots_in={"recipient": SlotValue(res.email, Source.tool_result, turn_id=octx.pmid,
                                             turn_index=turn_index)},
            turn_index=turn_index, decision=octx.decision)
        return view, goal_runtime.next_step(view, availability_ok=available, availability_status=status)

    async def _compose(self, octx, view, capability, step, *, turn_index: int, tz: str):
        """Write the generatable slots from the stated objective, and fold them into the goal.

        Nothing here decides anything. The values land as `model_derived`, which is the truth and which
        has two consequences the design leans on: `goal_slots.merge_slots` will let any later stated
        value correct them, and `goal_runtime._needs_confirmation` is guaranteed to put them on screen
        before a send. A composed draft is a PROPOSAL the student reads, never a decision.

        A composer that returns nothing leaves the slots empty on purpose. The caller re-runs `next_step`,
        lands on ASK_MISSING, and Bruce says what it still needs — which is the honest failure, and a far
        better one than proposing an email with an empty subject line.
        """
        composer = self._composer or default_composer()
        recipient = view.slots.get("recipient")
        tone = view.slots.get("tone")
        try:
            drafted = await composer.compose(
                objective=compose_objective(view, getattr(octx.msg, "text", None)),
                recipient=str(recipient.value) if recipient is not None and recipient.filled else "",
                tone=str(tone.value) if tone is not None and tone.filled else "",
                context=_recent_context(octx))
        except Exception:
            log.info("goal_compose_failed run=%s cap=%s", view.run_id, capability)
            drafted = {}
        wanted = set(step.missing)
        values = {name: SlotValue(value, Source.model_derived, turn_id=octx.pmid, turn_index=turn_index)
                  for name, value in (drafted or {}).items()
                  # ONLY the slots that were actually missing. A composer that returned a recipient would
                  # be inventing an address, and this is where that stops being possible.
                  if name in wanted and value}
        log.info("goal_composed run=%s cap=%s wanted=%d filled=%d", view.run_id, capability,
                 len(wanted), len(values))
        if not values:
            return view
        return await goal_runtime.ensure_goal(
            octx.user_id, capability=capability,
            conversation_id=getattr(octx, "conversation_id", "") or None,
            slots_in=values, turn_index=turn_index, decision=octx.decision)

    async def _cancel(self, octx, view, capability):
        from . import conversation_outcomes as co
        copy = _COPY.get(capability, _FALLBACK_COPY)
        await self._move(octx, view, "cancelled")
        return co.HandlerOutput(text=f"ok, i won't {copy.verb} it.", styled=False)

    async def _blocked(self, octx, view, capability, status):
        """The honest block, said FIRST rather than after four questions.

        `next_step` checks availability before missing slots for exactly this reason: collecting a
        recipient and a subject and a body and only then admitting the account is not connected is the
        exchange that made Bruce feel broken. None of these sentences denies an ability — they name the
        specific thing that is missing, which is a different and repairable statement.
        """
        from . import conversation_outcomes as co
        copy = _COPY.get(capability, _FALLBACK_COPY)
        spec = tool_registry.get(capability)
        provider = (spec.provider if spec else "").replace("_", " ") or "that account"
        if status == "insufficient_scope":
            text = (f"ur {provider} is connected but i was never given permission to {copy.verb} "
                    f"{copy.noun}. reconnect it with that turned on and i'll {copy.verb} this.")
        elif status == "disconnected":
            text = (f"ur {provider} isn't connected here yet. connect it and i'll {copy.verb} this "
                    f"{copy.noun}.")
        elif status == "unsupported":
            text = f"that one isn't live yet, so i'm not going to pretend i did it."
        else:
            text = (f"i couldn't check ur {provider} connection just now, so nothing has been "
                    f"{copy.past}. try me again in a sec?")
        await self._move(octx, view, "blocked", blocked_reason=f"{capability}:{status}"[:200])
        return co.HandlerOutput(text=text, styled=False)

    async def _propose(self, octx, view, capability, block, *, tz: str = ""):
        """Put the EXACT operation on screen and park a canonical Decision beside it.

        The decision carries the fingerprint of the arguments that would reach the provider, so the
        student's yes is anchored to the words they were actually shown. A goal edited afterwards
        produces a different fingerprint and is offered again rather than sent.
        """
        from . import agent_run_store, conversation_outcomes as co
        _arguments, executor = self._bind(view, capability)
        if executor is None:                                    # defense in depth; next_step gated it
            return co.HandlerOutput(
                text=_lower_first(goal_runtime.clarifying_question(view.missing))
                or "i still need a bit more before i can do that.", styled=False)
        copy = _COPY.get(capability, _FALLBACK_COPY)
        question = _render(copy.proposal, self._values(view, capability, tz=tz))
        fingerprint = _fingerprint(executor.gate_provider, executor.gate_operation,
                                   executor.gate_arguments())
        if not (isinstance(block, dict) and block.get("arguments_fingerprint") == fingerprint
                and str(block.get("status") or PENDING) == PENDING):
            block = decision_block(decision_id=str(uuid4()), capability=capability,
                                   fingerprint=fingerprint, question=question, run_id=view.run_id,
                                   source_message_id=octx.pmid)
        await agent_run_store.update_run(
            octx.user_id, UUID(view.run_id), status="awaiting_approval", active_decision=block)
        log.info("goal_proposed run=%s cap=%s decision=%s", view.run_id, capability,
                 block["decision_id"])
        return co.HandlerOutput(text=question, styled=False)

    async def _run(self, octx, view, capability, block, *, availability_status: str, tz: str = ""):
        """Goal -> arguments -> authorization -> MutationGateway -> provider -> read-back -> receipt.

        The ONE execution path. Everything irreversible happens inside `agent_loop.run_direct_action`,
        which persists the freshly minted evidence and then reloads and re-judges it through
        `mutation_gateway` against the world as it is now — a foreground turn held to exactly the standard
        a woken mission is. Nothing here performs a provider call, and nothing here decides that a send
        happened: `tr.verified` comes from the adapter's independent read-back and is copied, never
        re-judged.
        """
        from . import agent_loop, authorization_evidence as ae
        from . import conversation_outcomes as co, execution_gate

        _arguments, executor = self._bind(view, capability)
        if executor is None:
            return co.HandlerOutput(text="i lost the details for that one, mind saying it again?",
                                    styled=False)
        gate_args = executor.gate_arguments()
        fingerprint = _fingerprint(executor.gate_provider, executor.gate_operation, gate_args)
        stored = (block or {}).get("arguments_fingerprint") if isinstance(block, dict) else None
        if stored not in (None, "", fingerprint):
            # The goal changed after the student said yes. Their consent was for the old words, so it is
            # not spent here — the offer is made again against what the goal says now.
            log.info("goal_arguments_changed run=%s cap=%s", view.run_id, capability)
            return await self._propose(octx, view, capability, None, tz=tz)

        authorization = ae.try_grant(
            user_id=octx.user_id, provider=executor.gate_provider, operation=executor.gate_operation,
            arguments=gate_args, authorization_type=ae.AuthorizationType.decision_approval,
            text=getattr(octx.msg, "text", None), trusted_message_id=octx.pmid,
            decision_id=view.decision_id, conversation_id=getattr(octx, "conversation_id", "") or None,
            has_pending_decision=True, explicit_operation_request=False,
            # The runtime's reading of what a negation in this turn governed, computed ONCE for the whole
            # turn. It is forwarded, never interpreted: this handler cannot tell an approval from a
            # refusal and has no business trying — `user_action_boundary` is still the only thing that
            # turns a reading into a verdict, and it discards this outright unless it was derived from
            # these exact words and built for this exact operation.
            scope=getattr(octx, "scope", None))
        verdict = execution_gate.evaluate(
            authorization, user_id=octx.user_id, provider=executor.gate_provider,
            operation=executor.gate_operation, arguments=gate_args,
            conversation_id=getattr(octx, "conversation_id", "") or None)
        if authorization is None or verdict.denied:
            # The decision goes back to PENDING rather than staying approved: consent that could not be
            # used is not consent, and leaving it approved would make every later turn try to execute on
            # the strength of a yes the gate had already refused.
            log.info("goal_not_authorized run=%s cap=%s reason=%s", view.run_id, capability,
                     "no_evidence" if authorization is None else verdict.reason)
            await self._reopen(octx, view, block)
            copy = _COPY.get(capability, _FALLBACK_COPY)
            return co.HandlerOutput(
                text=f"i haven't {copy.past} it. say the word and i will.", styled=False)

        # The one status write this handler PROVES rather than asserts. `_derive_guard_ctx` cannot know
        # whether the broker says the capability is reachable or whether an authorization is open right
        # now — both are live judgements — so the evidence is assembled here and handed to the gate.
        guard = transitions.GuardContext(
            capability=capability, capability_available=True, availability_status=availability_status,
            required_slots=goal_slots.required_slots(view.kind),
            filled_slots=tuple(name for name, sv in view.slots.items() if sv is not None and sv.filled),
            authorization_open=True, decision_id=view.decision_id)
        if not await self._move(octx, view, "executing", guard_ctx=guard,
                                current_action={"capability": capability,
                                                "provider": executor.gate_provider,
                                                "operation": executor.gate_operation}):
            return co.HandlerOutput(
                text="i couldn't start that cleanly, so i haven't done anything. try me again?",
                styled=False)
        view = replace(view, status="executing")

        result = await agent_loop.run_direct_action(
            octx.user_id, executor=executor, idempotency_key=idempotency_key(view.run_id, capability),
            authorization=authorization, conversation_id=getattr(octx, "conversation_id", "") or None,
            # The goal this call is executing ON BEHALF OF. `agent_loop` says a caller that has one must
            # pass it, and this one always has: without it the attempt row records a provider write that
            # names no goal, so "what did this goal actually do" cannot be answered from the database —
            # which is the question the whole spine exists to make answerable.
            parent_run_id=view.run_id)
        await self._settle(octx, view, result)
        log.info("goal_executed run=%s cap=%s status=%s verified=%s", view.run_id, capability,
                 result.status, result.verified)

        copy = _COPY.get(capability, _FALLBACK_COPY)
        if result.verified:
            await self._remember(octx, view, capability, result.tool_result, tz=tz)
            return co.HandlerOutput(text=_render(copy.receipt, self._values(view, capability, tz=tz)),
                                    styled=False)
        if result.status == "blocked":
            spec = tool_registry.get(capability)
            provider = (spec.provider if spec else "").replace("_", " ") or "that account"
            return co.HandlerOutput(
                text=f"{provider} turned that down, so nothing went out. reconnect it and i'll retry.",
                styled=False)
        return co.HandlerOutput(
            text=f"that didn't go through, so nothing was {copy.past}. want me to try again?",
            styled=False)

    # --- the small mechanics ----------------------------------------------------------------------------------

    def _values(self, view, capability, *, tz: str = "") -> dict:
        values = {name: sv.value for name, sv in view.slots.items() if sv is not None and sv.filled}
        values["capability"] = capability
        values["when"] = _when_phrase(view.kind, values, timezone_name=tz)
        return values

    def _bind(self, view, capability):
        """(arguments, executor) for this goal, or (None, None) when it is not runnable yet.

        `tool_arguments()` RAISES while a required slot is empty, which is the behaviour wanted: a
        partially filled send is not a degraded send, it is a wrong one. It is caught here so a goal that
        somehow reached this point half-filled asks a question rather than taking the turn down.
        """
        factory = _EXECUTORS.get(capability)
        if factory is None:
            return None, None
        try:
            arguments = view.tool_arguments()
        except Exception:
            return None, None
        try:
            return arguments, factory(arguments,
                                      idempotency_key=idempotency_key(view.run_id, capability),
                                      adapter=self._adapter)
        except Exception:
            log.info("goal_executor_build_failed cap=%s run=%s", capability, view.run_id)
            return None, None

    async def _decision_block(self, octx, view) -> dict | None:
        """The canonical Decision parked on this run, read back whole.

        `GoalView` carries the decision's ID and whether it was approved, which is what the state machine
        needs — but not the fingerprint, which is what CONSENT needs. So the row is read once, here, and
        passed down rather than fetched again by each step.
        """
        from . import agent_run_store
        try:
            run = await agent_run_store.get_run(octx.user_id, UUID(view.run_id))
        except Exception:
            return None
        block = (run or {}).get("active_decision")
        return block if isinstance(block, dict) else None

    async def _availability(self, user_id, capability) -> tuple[bool, str]:
        from . import tool_broker
        try:
            av = await tool_broker.availability(user_id, capability)
            return bool(av.ok), str(av.status or "")
        except Exception:
            # A failed probe is NOT "you have no tools". It reads as unknown and says so — printing a
            # denial after a failed look is the exact sentence this workstream exists to delete.
            log.info("goal_availability_unreadable cap=%s", capability)
            return False, "unknown"

    async def _ensure_preparing(self, octx, view):
        if view.status != "understanding":
            return view
        return replace(view, status="preparing") if await self._move(octx, view, "preparing") else view

    async def _approve(self, octx, view, capability, block):
        """Record that the student said yes to the EXACT operation that was on screen.

        The fingerprint is rechecked before the approval lands. A decision whose arguments moved since it
        was offered is not the decision that was approved, so it stays pending and gets offered again —
        the difference between "yes" meaning what the student saw and "yes" meaning whatever the goal
        happens to say now.
        """
        from . import agent_run_store
        if not isinstance(block, dict) or str(block.get("status") or PENDING) != PENDING:
            return view, block
        _arguments, executor = self._bind(view, capability)
        if executor is not None:
            current = _fingerprint(executor.gate_provider, executor.gate_operation,
                                   executor.gate_arguments())
            if block.get("arguments_fingerprint") not in (None, "", current):
                log.info("goal_approval_stale run=%s cap=%s", view.run_id, capability)
                return view, block
        approved = {**block, "status": APPROVED}
        try:
            await agent_run_store.update_run(octx.user_id, UUID(view.run_id), active_decision=approved)
        except Exception:
            log.warning("goal_approval_write_failed run=%s", view.run_id)
            return view, block
        log.info("goal_approved run=%s decision=%s", view.run_id, approved.get("decision_id"))
        return replace(view, confirmed=True,
                       decision_id=str(approved.get("decision_id") or "") or None), approved

    async def _reopen(self, octx, view, block) -> None:
        from . import agent_run_store
        if not isinstance(block, dict) or str(block.get("status") or PENDING) == PENDING:
            return
        try:
            await agent_run_store.update_run(octx.user_id, UUID(view.run_id),
                                             active_decision={**block, "status": PENDING})
        except Exception:
            log.info("goal_reopen_failed run=%s", view.run_id)

    async def _settle(self, octx, view, result):
        """Close the goal from the VERIFIED result, mirroring what `agent_loop` wrote on its own run.

        `completed` rather than `succeeded`, and that is not a shortcut: `succeeded` is reachable only
        from `verifying` with an independent read-back, and the direct-action lane's verified terminal is
        `completed` — declared in `agent_run_store._OPERATIONAL` and pinned to `agent_loop._status_for`.
        Reaching `succeeded` from here would mean walking the machine states in this handler, which is a
        second execution path, which is the thing this must not add.
        """
        from . import agent_run_store
        tr = result.tool_result
        fields: dict = {
            "status": result.status,
            "last_tool_result": {"outcome": tr.outcome.value, "capability": tr.capability,
                                 "provider": tr.provider, "operation": tr.operation,
                                 "verified": tr.verified, "provider_entity_id": tr.provider_entity_id},
            "verification_result": {"verified": tr.verified, "outcome": tr.outcome.value},
        }
        if result.status == "blocked":
            fields["blocked_reason"] = (tr.reason or "")[:200]
        try:
            await agent_run_store.update_run(octx.user_id, UUID(view.run_id), **fields)
        except Exception:
            # The provider call already happened and its receipt is real. Losing the bookkeeping is bad;
            # losing it AND telling the student nothing happened would be worse.
            log.warning("goal_settle_failed run=%s status=%s", view.run_id, result.status)

    async def _remember(self, octx, view, capability, tr, *, tz: str = ""):
        """The durable fact, written only after the read-back proved it — `memory_finalize`'s own rule.
        "I emailed my coach on the 29th" is true forever; "i am about to" is a slot, and slots stay in the
        goal where they can be corrected."""
        from . import memory_finalize
        copy = _COPY.get(capability, _FALLBACK_COPY)
        try:
            await memory_finalize.after_verified_outcome(
                octx.user_id, capability=capability,
                summary=_render(copy.memory, self._values(view, capability, tz=tz)),
                trusted_text=getattr(octx.msg, "text", None), stated_span=None,
                source_message_id=octx.pmid, provider_entity_id=tr.provider_entity_id)
        except Exception:
            log.info("goal_memory_failed cap=%s", capability)

    async def _move(self, octx, view, status: str, **fields) -> bool:
        """Every production status change in this file goes through here, and therefore through
        `transitions.propose_transition`. There is no direct assignment anywhere."""
        from . import agent_run_store
        try:
            await agent_run_store.update_run(octx.user_id, UUID(view.run_id), status=status, **fields)
            return True
        except agent_run_store.IllegalStatusWrite as exc:
            # The machine refused. That is information rather than an error to swallow: it names the move
            # and what was missing, and the reply is still generated from the goal rather than from the
            # fact that a write failed.
            log.info("goal_transition_refused run=%s %s->%s reason=%s missing=%s", view.run_id,
                     view.status, status, exc.reason, ",".join(exc.missing_slots))
            return False
        except Exception:
            log.warning("goal_transition_failed run=%s target=%s", view.run_id, status)
            return False
