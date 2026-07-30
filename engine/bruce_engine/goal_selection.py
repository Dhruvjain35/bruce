"""WHICH open goal is this turn about — an ordered, deterministic answer, or an honest question.

THE BUG THIS EXISTS FOR, measured against the real transcript's own shape. A student can be booking a
rehearsal and writing to a teacher in the same thread. `goal_runtime.open_goal_of_kind` already keeps the
two sets of slots from ever touching, so nothing gets contaminated — and yet with both open the student's
turns landed nowhere at all:

    the model drafts a subject and a body   ->  read as a calendar turn, declined for want of an executor
    "YES WRITE IT AND SEND IT"              ->  the email's pending Decision was never even looked at
    an inline reply to the email proposal   ->  answered by the model

One cause. `conversation_runtime._open_goal_row` returned THE NEWEST typed run, and every downstream
question — which pending Decision, which draft, which capability — was read off that one row. Recency is
not a reading of the turn; it is a reading of the clock, and the clock knows nothing about what the
student just typed. `goal_runtime.resolve_capability` already refuses to guess when two kinds are open and
says so in its own docstring. What was missing is anything that then decides.

THE ORDER, and it is the whole module. Each rule is strictly better evidence than the one below it:

    1. INLINE REPLY TARGET      the student pointed at a message, and that message belongs to one goal.
                                The only unambiguous statement of intent a turn can carry.
    2. THE PENDING DECISION     the turn answers yes or no, and exactly one open goal is holding a
                                question. An answer belongs to the question that was asked.
    3. AN EXPLICIT REFERENCE    the turn names a real operation, or says the noun out loud ("the email").
    4. UNIQUE COMPATIBILITY     the turn carries values only one open goal has slots for. A drafted
                                subject fits an email and nothing about a rehearsal.
    5. RECENCY                  and ONLY when one candidate is left, which is to say: never as a
                                tie-break. This is what the defect was.
    6. OTHERWISE                one question, no goal, no write.

WHY 6 IS NOT "PICK THE LIKELIER ONE". Guessing between an email and a calendar goal is how a recipient
becomes an event title, and worse, how a bare "no" cancels the goal the student was not talking about. The
cost of asking is one turn. The cost of being wrong is work the student never asked for, or work they
asked for silently discarded — which is the transcript's own failure mode, and it is the quieter of the
two.

AMBIGUITY IS NOT THE SAME AS IRRELEVANCE, and conflating them would make Bruce insufferable. "whats 8
times 7" typed beside two open goals is not a turn that fails to name one — it is a turn that is not about
them, and it must stay conversation. So rule 6 fires only when the turn SPEAKS to work in flight: it
answers a question, points at something, or carries a value some open goal could hold. A turn that does
none of those selects nothing and is not ambiguous either.

Nothing here performs I/O and nothing here writes. The caller fetches the candidates once and hands them
over, for the same reason `continuation.resolve` is pure: a selector that fetched its own rows would be a
second view of the turn, and three views of one turn is the defect this whole spine removes.

CONTENT-FREE LOGGING. This module returns a human-readable label for a goal (`describe`) because the
clarifying question has to name the two things it is asking between. That string goes to the STUDENT. It
never goes to a log.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import decision_resolver, goal_slots
from .goal_slots import GoalKind

log = logging.getLogger("bruce.goal_selection")   # CONTENT-FREE: rules, counts, run ids

# Which rule decided, or why nothing did. Typed and closed, like `transitions.REASONS` and
# `goal_handler.DECLINE_REASONS`: "how often did the runtime have to ask which goal" has to be countable
# without grepping a log line.
BY_REPLY = "inline_reply_target"
BY_DECISION = "answers_the_one_pending_decision"
BY_REFERENCE = "explicit_operation_or_goal_reference"
BY_COMPATIBILITY = "uniquely_compatible_open_goal"
BY_RECENCY = "the_only_open_goal"
NOTHING_OPEN = "no_open_goal"
NEW_GOAL = "names_a_capability_no_open_goal_serves"
NOT_ABOUT_A_GOAL = "turn_does_not_speak_to_work_in_flight"
AMBIGUOUS = "several_open_goals_and_nothing_names_one"

RULES: tuple[str, ...] = (BY_REPLY, BY_DECISION, BY_REFERENCE, BY_COMPATIBILITY, BY_RECENCY)
REASONS: frozenset[str] = frozenset(RULES) | {NOTHING_OPEN, NEW_GOAL, NOT_ABOUT_A_GOAL, AMBIGUOUS}


# What a student calls each kind of goal out loud. ROLE words for the ARTIFACT, not product phrases and
# not content: "rehearsal" is what one particular event is called and belongs in a slot, while "event" is
# what every one of them is. A kind whose noun set matched content would select on the coincidence that a
# student said the same word twice.
_KIND_NOUNS: dict[GoalKind, tuple[str, ...]] = {
    GoalKind.send_email: ("email", "e-mail", "mail", "message", "note", "reply", "draft"),
    GoalKind.schedule_event: ("calendar", "event", "meeting", "appointment", "invite", "schedule"),
}

# A turn that is nothing but a pointer. It names no goal and is still plainly about one of them, which is
# exactly the shape rule 6 must ask about rather than drop. Kept in step with `continuation._DEICTIC`.
_POINTER = re.compile(r"\A(?:this|that|it|these|those|the one)(?:\s+one)?\Z")


@dataclass(frozen=True)
class Selection:
    """Which open goal this turn is about, which rule said so, and whether Bruce owes a question.

    `run` and `ambiguous` are deliberately NOT the same axis. Three outcomes exist and each needs a
    different thing from the caller: a selected goal (continue it), an ambiguity (ask, and do nothing
    else), and neither (today's behaviour — the turn is not about work in flight, or there is none).
    """

    run: dict | None
    rule: str
    ambiguous: bool = False
    candidates: tuple[dict, ...] = field(default_factory=tuple)   # what was on the table, newest first

    @property
    def run_id(self) -> str | None:
        return str(self.run["id"]) if isinstance(self.run, Mapping) and self.run.get("id") else None

    @property
    def considered(self) -> tuple[str, ...]:
        return tuple(str(r.get("id")) for r in self.candidates)

    @property
    def telemetry(self) -> dict:
        """CONTENT-FREE. Ids, a rule name and a count — never a label, because `describe` renders a
        subject line and a recipient and those are the student's words."""
        return {"rule": self.rule, "candidates": len(self.candidates),
                "selected": self.run_id, "ambiguous": self.ambiguous}

    @property
    def question(self) -> str:
        """The ONE thing rule 6 owes the student. Empty unless this selection is the ambiguous one."""
        return clarifying_question(self.candidates) if self.ambiguous else ""


# --- reading a candidate ---------------------------------------------------------------------------------


def _kind_of(run: Mapping) -> GoalKind | None:
    goal = run.get("goal")
    kind, _ = goal_slots.from_goal_jsonb(dict(goal) if isinstance(goal, Mapping) else None)
    return kind


def _slots_of(run: Mapping) -> dict:
    goal = run.get("goal")
    _kind, slots = goal_slots.from_goal_jsonb(dict(goal) if isinstance(goal, Mapping) else None)
    return slots


def _pending_decision_id(run: Mapping) -> str | None:
    """The id of a Decision this goal is STILL waiting on. An approved or closed one is not a question the
    student can be answering, so it must not attract their yes."""
    block = run.get("active_decision")
    if not isinstance(block, Mapping):
        return None
    if str(block.get("status") or "pending").strip().lower() != "pending":
        return None
    raw = block.get("decision_id") or block.get("id")
    return str(raw).strip() or None if raw else None


def message_ids_of(run: Mapping) -> frozenset[str]:
    """Every message id this goal can be POINTED AT — the turns whose words are in it.

    Read off slot provenance rather than from a list somebody has to remember to append to. A slot's
    `turn_id` is the message the value was stated in, so the set is exactly "the messages this goal is
    made of", and it cannot drift from the goal because it IS the goal. The pending Decision contributes
    the turn its proposal was shown on, which is the message a student is most likely to reply to and the
    one no slot necessarily records.
    """
    ids = {str(sv.turn_id) for sv in _slots_of(run).values()
           if sv is not None and getattr(sv, "turn_id", None)}
    block = run.get("active_decision")
    if isinstance(block, Mapping) and block.get("source_message_id"):
        ids.add(str(block["source_message_id"]))
    return frozenset(ids)


def describe(run: Mapping) -> str:
    """A short label for ONE goal, for the clarifying question. Student-facing; never logged.

    The goal's own `desired_outcome` first — it is the model's plain-language proposal, the one thing it is
    reliably good at, and it is what `turn_context.OpenRun.what` already renders. Failing that, the kind's
    noun plus whichever slot identifies the thing, so the question reads as "the email to X" rather than
    as a uuid.
    """
    goal = run.get("goal") if isinstance(run.get("goal"), Mapping) else {}
    outcome = str(goal.get("desired_outcome") or "").strip()
    if outcome:
        return outcome[:80]
    kind = _kind_of(run)
    slots = _slots_of(run)
    for name, template in (("recipient", "the email to {}"), ("subject", "the email about {}"),
                           ("title", "{}")):
        sv = slots.get(name)
        if sv is not None and sv.filled:
            return template.format(str(sv.value))[:80]
    return {GoalKind.send_email: "the email", GoalKind.schedule_event: "the calendar thing"}.get(
        kind, "the other one")


# --- the rules -------------------------------------------------------------------------------------------


def _only(hits: list[dict]) -> dict | None:
    """One candidate or nothing. A rule that matched two goals has not disambiguated anything, and taking
    the first would be recency wearing a better rule's name."""
    return hits[0] if len(hits) == 1 else None


def _by_reply(runs: list[dict], reply_to_message_id: str | None) -> dict | None:
    target = str(reply_to_message_id or "").strip()
    if not target:
        return None
    return _only([r for r in runs if target in message_ids_of(r)])


def _answers_a_question(text: str | None) -> bool:
    """Is this turn a yes or a no at all? Judged with `decision_resolver`, the same vocabulary the
    execution boundary is made of, so "the turn answers a Decision" means one thing in this tree.

    Inline quotations are removed first — the same asymmetry the boundary applies to its affirmative path.
    A student pasting `coach said "yes send it"` is reporting a conversation, and reading it as an answer
    would point this turn at the goal holding a question it is not answering.
    """
    return decision_resolver.resolve_approval(decision_resolver.strip_inline_quotes(text)) in (
        decision_resolver.Resolution.approved, decision_resolver.Resolution.rejected)


def _asks_for_a_change(runs, text: str | None) -> bool:
    """Does the turn ask for an EDIT rather than answer a question?

    "move it to friday" and "send it to coach instead" both read as approvals to a yes/no resolver and
    neither is an approval of what is on screen. `continuation.resolve` already puts amendment ahead of
    approval for exactly that reason; rule 2 has to honour the same precedence or a change request would
    be pulled onto whichever goal happens to be holding a question — an answer to a question nobody asked.
    """
    from . import continuation as continuation_mod
    kinds = {k for k in (_kind_of(r) for r in runs) if k is not None}
    return any(continuation_mod.patches_a_slot(text, kind) for kind in kinds)


def _by_decision(runs, text: str | None) -> dict | None:
    if not _answers_a_question(text) or _asks_for_a_change(runs, text):
        return None
    return _only([r for r in runs if _pending_decision_id(r)])


def _named_capability(decision) -> str:
    """A REAL operation id the turn named, or "". The model's free text ("sending messages") joined to
    nothing in the transcript and must join to nothing here."""
    from . import tool_registry
    for raw in (getattr(decision, "required_capabilities", None) or ()):
        cap = str(raw or "").strip()
        if cap and (goal_slots.kind_for_capability(cap) is not None or tool_registry.get(cap) is not None):
            return cap
    return ""


def _serves(run: Mapping, capability: str) -> bool:
    """Would this open goal be the one that capability acts on?

    By KIND when the capability declares one. Otherwise by DOMAIN, and ONLY for a write: "move it to
    friday" is `calendar.update_event`, which declares no goal kind because an update and a create are
    different operations on the same piece of work, and without the fallback that turn lands nowhere.

    A READ is excluded on purpose. "what did coach say" is `gmail.get_message`, same domain as an open
    send goal and about something else entirely; letting it select the goal would answer a question about
    one email by continuing the drafting of another.
    """
    kind = goal_slots.kind_for_capability(capability)
    if kind is not None:
        return _kind_of(run) is kind
    from . import tool_registry
    spec = tool_registry.get(capability)
    if spec is None or not getattr(spec, "write", False):
        return False
    domain = capability.split(".", 1)[0].strip().lower()
    return bool(domain) and str(run.get("domain") or "").strip().lower() == domain


def _named_by_noun(runs, text: str | None) -> list[dict]:
    """Every open goal whose KIND the student said out loud. One hit selects; two hits name neither, and
    both facts are read off this one list so the two callers cannot disagree about it."""
    t = decision_resolver.normalize(text)
    if not t:
        return []
    return [r for r in runs
            if any(re.search(r"\b" + re.escape(n) + r"\b", t)
                   for n in _KIND_NOUNS.get(_kind_of(r), ()))]


def _compatible(run: Mapping, *, decision, text: str | None) -> bool:
    """Could this turn's values live in THIS goal?

    Asked through the two functions that actually write slots — `goal_handler.entity_slots` for the
    model's reading and `continuation.patches_a_slot` for the student's own words — rather than through a
    third opinion invented here. A rule that guessed compatibility differently from the code that STORES
    the value would select a goal the turn then silently fails to update, which is worse than asking.
    """
    kind = _kind_of(run)
    if kind is None:
        return False
    from . import continuation as continuation_mod
    from . import goal_handler
    if goal_handler.entity_slots(kind, getattr(decision, "extracted_entities", ()) or ()):
        return True
    return continuation_mod.patches_a_slot(text, kind)


def _by_compatibility(runs, *, decision, text: str | None) -> dict | None:
    return _only([r for r in runs if _compatible(r, decision=decision, text=text)])


def _speaks_to_work_in_flight(runs, *, decision, text: str | None) -> bool:
    """Is this turn about the open goals AT ALL? The precondition for asking rather than shrugging.

    Four ways in, and each is a thing a student does when they mean the work rather than the weather:
    they answer the question Bruce asked, they point at something, they say a value one of the goals could
    hold, or they name the kind of thing. Everything else is conversation happening beside a goal, and
    claiming it would turn "whats 8 times 7" into an interrogation about a subject line.
    """
    if _answers_a_question(text) and any(_pending_decision_id(r) for r in runs):
        return True
    pointer = decision_resolver.normalize(text)
    if pointer and _POINTER.fullmatch(pointer):
        return True
    if any(_compatible(r, decision=decision, text=text) for r in runs):
        return True
    return bool(_named_by_noun(runs, text))


# --- the selector ----------------------------------------------------------------------------------------


def select(runs, *, text: str | None = None, reply_to_message_id: str | None = None,
           decision=None) -> Selection:
    """The one open goal this turn is about. Pure: no I/O, no writes, no model.

    `runs` is every open run the caller fetched, NEWEST FIRST — untyped rows and other conversations
    already excluded by the fetch. The order matters only for rule 5 and for the candidate list in
    telemetry; no rule below it may use it, which is the property the defect violated.
    """
    candidates = tuple(r for r in (runs or []) if isinstance(r, Mapping) and _kind_of(r) is not None)

    if not candidates:
        return Selection(None, NOTHING_OPEN)
    if len(candidates) == 1:
        # RULE 5, and the only place recency is allowed to speak — where there is nothing to be recent
        # ABOUT. Every single-goal turn in this runtime resolves here, byte-for-byte as it did before.
        return Selection(candidates[0], BY_RECENCY, candidates=candidates)

    for rule, hit in ((BY_REPLY, _by_reply(candidates, reply_to_message_id)),
                      (BY_DECISION, _by_decision(candidates, text)),):
        if hit is not None:
            return Selection(hit, rule, candidates=candidates)

    capability = _named_capability(decision)
    if capability:
        served = [r for r in candidates if _serves(r, capability)]
        if len(served) == 1:
            return Selection(served[0], BY_REFERENCE, candidates=candidates)
        if not served:
            # The turn names a real operation and no open goal performs it. That is a NEW goal, not an
            # ambiguity — asking "which of these two do you mean" about a third thing is nonsense.
            return Selection(None, NEW_GOAL, candidates=candidates)

    by_noun = _only(_named_by_noun(candidates, text))
    if by_noun is not None:
        return Selection(by_noun, BY_REFERENCE, candidates=candidates)

    by_slots = _by_compatibility(candidates, decision=decision, text=text)
    if by_slots is not None:
        return Selection(by_slots, BY_COMPATIBILITY, candidates=candidates)

    if _speaks_to_work_in_flight(candidates, decision=decision, text=text):
        # RULE 6. Nothing in the turn names one of them and the turn is plainly about one of them.
        log.info("goal_selection_ambiguous candidates=%d", len(candidates))
        return Selection(None, AMBIGUOUS, ambiguous=True, candidates=candidates)

    return Selection(None, NOT_ABOUT_A_GOAL, candidates=candidates)


def refine(selection: Selection, runs, *, text: str | None = None,
           reply_to_message_id: str | None = None, decision=None) -> Selection:
    """A second pass once the model's reading of the turn exists. It may only break a tie, never move one.

    The evidence arrives in two instalments and there is nothing to be done about that: an inline reply
    and a pending Decision are known before anything reasons, while a drafted subject and a named
    operation are the reasoner's output and cannot be. So the selector runs on what is known, the
    continuation is resolved against THAT, and this adds the rest.

    THE ONE RULE. A selection that already named a goal is final. `continuation.resolve` has by then bound
    this turn's yes, its amendment or its pointer to that specific run, and re-selecting underneath it
    would leave a confirmation of one goal's Decision sitting on another's. Only "I could not tell" is
    allowed to become "now I can".
    """
    if selection is not None and selection.run is not None:
        return selection
    return select(runs, text=text, reply_to_message_id=reply_to_message_id, decision=decision)


def clarifying_question(runs) -> str:
    """The ONE question rule 6 owes the student. Names both things and asks nothing else.

    A question that listed the goals without asking anything, or asked without naming them, would both
    make the student do the disambiguating twice. Bruce writes lowercase.
    """
    labels = [describe(r) for r in (runs or []) if isinstance(r, Mapping)][:3]
    if len(labels) < 2:
        return "i've got a couple of things going rn. which one do u mean?"
    # A goal's label is a VERB PHRASE ("email ms alvarez a thank-you note"), so it has to sit after the
    # question rather than inside a sentence about what Bruce has going — otherwise the reply reads as
    # word salad, and a clarifying question nobody can parse costs the student a turn for nothing.
    return "which one do u mean — " + ", or ".join(labels) + "?"
