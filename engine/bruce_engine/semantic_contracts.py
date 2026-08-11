"""Semantic contracts (M2) — the vocabulary the language model uses to express MEANING, with no
execution class anywhere in it.

WHY THIS EXISTS. `_RouterOut` (router_model.py) put `execution_class` first and made it required, so the
model had to commit to orchestration — direct_action vs foreground_agent vs background_mission — before it
had written down what the student even wanted. Structured output is generated field-by-field in schema
order, so this is not a stylistic complaint: the orchestration token is literally emitted before the
domain and operation tokens that would justify it. The re-grade measured the consequence — actionability
0.95, orchestration 0.615. The model understood; it was being asked the wrong question first.

The split: this layer answers "what does the student mean". `execution_derivation` answers "how does that
run". Only the second one needs to know what a background mission is.

DOMAIN IS A CAPABILITY FAMILY, NOT A PROVIDER. The old prompt said `domain (calendar/... or null)` and the
model answered "communication" for email at confidence 0.96 — graded wrong by a rubric demanding "gmail".
The model was right and the rubric was provider-shaped. Families here are semantic and provider-free;
mapping a family onto Gmail or Google Calendar is the orchestrator's job, and stays the orchestrator's job
when Drive, Canvas, and Docs arrive. The semantic layer must never name a provider.

Dependency-light on purpose (enums + frozen dataclasses, no model, no DB) so every layer can import it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TurnRole(str, Enum):
    """What this message IS in the conversation — independent of whether it causes work."""
    conversation = "conversation"          # chat, thanks, greeting, tutoring
    new_goal = "new_goal"                  # a fresh thing the student wants done
    continuation = "continuation"          # advances or asks about work already in flight
    correction = "correction"              # fixes something already said or already done
    decision_response = "decision_response"  # answers a question Bruce asked (yes/no/choice)
    cancellation = "cancellation"          # calls off something pending or in flight
    reference_only = "reference_only"      # deixis with no standalone content ("that one")


class Actionability(str, Enum):
    """Whether the turn implies work, and of what shape — still NOT an execution class."""
    no_action = "no_action"                    # nothing to do
    information_only = "information_only"      # answerable from knowledge/state, no tool
    executable = "executable"                  # a concrete operation on the world is wanted
    durable_monitoring = "durable_monitoring"  # wants something watched over time
    ambiguous = "ambiguous"                    # plausibly work, but underspecified


class DecisionPolarity(str, Enum):
    """Which way a decision_response points. Its ABSENCE was a safety hole: the derivation treated every
    decision_response under a pending Decision as consent, so "no", "nah dont", and a forwarded "yes add
    it" from someone else all executed the proposal. Polarity is meaning, so the model reads it — but it
    can only ever authorize when it says `approve`, and everything else declines or asks."""
    approve = "approve"
    decline = "decline"
    unclear = "unclear"
    none = "none"                  # not a response to anything Bruce asked


class GoalCount(str, Enum):
    """How many distinct things the turn asks for. Measured: on "add practice friday and email coach",
    the model returned ONE family and one operation, and the runtime happily executed half the request.
    Doing half of what was asked, silently, is worse than asking."""
    none = "none"
    one = "one"
    several = "several"


class Family(str, Enum):
    """Capability FAMILIES — semantic, provider-free, closed. The model picks from these; the
    orchestrator maps them onto live capabilities. Adding Drive/Canvas later adds a family here, never a
    provider name."""
    calendar = "calendar"            # a schedule: events, appointments, deadlines, times
    communication = "communication"  # reaching a person: email, message, note
    coursework = "coursework"        # school work: assignments, classes, grades
    files = "files"                  # documents and storage
    memory = "memory"                # a durable fact about the student (timezone, preference)
    knowledge = "knowledge"          # answerable by thinking, no tool at all
    unknown = "unknown"              # meaning did not resolve to any family


class OperationFamily(str, Enum):
    """Provider-neutral verbs. 'send' is send-to-a-person whether the hand is Gmail or something later."""
    create = "create"
    update = "update"
    cancel = "cancel"
    send = "send"
    find = "find"
    monitor = "monitor"
    remember = "remember"
    answer = "answer"
    none = "none"


@dataclass(frozen=True)
class SemanticTurn:
    """What the model understood. Deliberately contains no execution class, no capability id, no provider,
    and no idempotency concern — none of those are things a language model should be deciding."""
    turn_role: TurnRole
    actionability: Actionability
    decision_polarity: DecisionPolarity = DecisionPolarity.none
    goal_count: GoalCount = GoalCount.none
    domain_candidates: tuple[Family, ...] = ()
    operation_family: OperationFamily = OperationFamily.none
    desired_outcome: str | None = None
    target_entities: tuple[str, ...] = ()      # raw referent text, unresolved ("my coach", "chem test")
    references: tuple[str, ...] = ()           # deictic spans that need a referent ("that one")
    correction_target: str | None = None
    continuation_target: str | None = None
    temporal_intent: str | None = None         # raw time language, unresolved ("thursday at 3")
    constraints: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()
    confidence: float = 0.0
    uncertainty: tuple[str, ...] = ()
    needs_frontier: bool = False               # Stage 1 asking for Stage 2, not a quality signal
    # Resolving which family this turn is about lives in `execution_derivation.resolve_family`, not here:
    # it needs the capability tables to break a tie, and two answers to that question would drift apart.


@dataclass(frozen=True)
class TurnContext:
    """The deterministic facts the orchestrator needs. Every one comes from the runtime, never the model —
    the model must not be the source of truth for what is connected or what is in flight."""
    live_families: frozenset[Family] = frozenset()
    live_capabilities: frozenset[str] = frozenset()
    pending_decision_id: str | None = None
    active_run_id: str | None = None
    active_run_domain: str | None = None
    has_reply_ref: bool = False
    has_attachments: bool = False
    # The deterministic refusal/cancellation verdict over TRUSTED user text, resolved before any model
    # call. Typed loosely to keep this module dependency-light; see `user_action_boundary`.
    action_boundary: object | None = None


@dataclass(frozen=True)
class TriageFailure:
    """A Stage-1 call that produced nothing usable. `reason` is always explicit — the old path converted
    every failure silently into fast_conversation, which is why the true miss rate was unmeasurable."""
    reason: str                    # timeout | transport | invalid_schema | low_confidence | disabled
    elapsed_ms: float = 0.0
    partial: SemanticTurn | None = None   # a semantically usable read whose optional fields failed


@dataclass(frozen=True)
class Derivation:
    """The orchestrator's output: how the understood goal actually runs."""
    execution_class: str
    action: str | None = None
    domain: str | None = None                  # provider-facing domain ("calendar", "gmail")
    capabilities: tuple[str, ...] = ()
    needs_clarification: bool = False
    clarification_reason: str | None = None
    continuation_run_id: str | None = None
    correction_of_run_id: str | None = None
    decision_id: str | None = None
    confidence: float = 0.0
    rule: str = ""                             # which derivation rule fired — for telemetry and tests
    ambiguity: tuple[str, ...] = field(default_factory=tuple)


# =====================================================================================================
# THE EXECUTIVE CONTRACT
# =====================================================================================================
# `SemanticTurn` above answers "what does this message mean" in provider-neutral terms, and `Derivation`
# answers "how would that run". Neither carries the DETAIL a student supplied — the slots, the people they
# named, the edits they asked for, how they want to be shown things. Today that detail is re-derived from
# the raw string by ~28 separate regex/keyword matchers scattered across continuation.py, directive_scope.py
# and goal_handler.py, each with its own closed vocabulary. That is why "make it sound less like a robot"
# does nothing: "robot" is not one of the eleven adjectives in the tone list.
#
# `ExecutiveTurn` is the whole reading of one turn — meaning, detail and grounding together — assembled by
# `semantic_executive` from the model's read plus deterministic derivation. It is what goal selection,
# continuation and slot filling consume INSTEAD of re-reading the string.
#
# IT REMAINS A PROPOSAL. There is no field here meaning authorized, executed, completed, verified or sent,
# and `test_interpretation_can_never_authorize_execute_or_claim_completion` asserts that on the class
# itself so a future field cannot quietly gain that power. Bruce reads forwarded mail and OCR'd
# screenshots; if understanding could authorize, a stranger's sentence would be an instruction.


class Mode(str, Enum):
    """What the student is DOING. A view of `TurnRole` widened with the two states the runtime needs to
    distinguish and the role vocabulary does not: asking ABOUT work, and being unreadable."""
    conversation = "conversation"
    new_goal = "new_goal"
    continue_goal = "continue_goal"
    answer_question = "answer_question"
    clarify = "clarify"
    approve = "approve"
    reject = "reject"
    cancel = "cancel"


class Fill(str, Enum):
    """Where a proposed value came from. Provenance travels with the value or the value is worthless."""
    stated = "stated"        # the student wrote it, this turn, in trusted text
    carried = "carried"      # already on the goal
    resolved = "resolved"    # looked up (a known person, an existing event) — never invented
    generated = "generated"  # composed by Bruce because composing it is safe


@dataclass(frozen=True)
class SlotPatch:
    """A PROPOSED change to one typed slot. `goal_handler` merges it under its own provenance rules, and
    a patch always loses to something the student typed. Proposing `recipient` does not set it."""
    name: str
    value: str | None = None
    fill: Fill = Fill.stated
    source_span: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True)
class ReferencedPerson:
    """Someone the student referred to, AS they referred to them — and carrying no address.

    Resolution is `people.resolve`'s job, deliberately not the model's: a model asked for an address
    produces a plausible one, and a plausible address is a stranger receiving a student's mail.
    """
    mention: str                       # "my teacher", "her", "bobby"
    relationship: str | None = None
    is_pronoun: bool = False
    source_span: str | None = None


@dataclass(frozen=True)
class RequestedEdit:
    """An amendment to something already drafted. `instruction` is free text on purpose — the closed tone
    list this replaces could not read "less like a robot", and the fix is not a twelfth adjective."""
    target: str                        # "draft" | "subject" | "body" | "event_title" | ...
    instruction: str
    source_span: str | None = None


class Presentation(str, Enum):
    """How the student wants to be SHOWN things. A preference, never a safety decision.

    `hidden_draft` suppresses the draft BODY. It never suppresses the confirmation: what is about to
    happen, and to whom, stays visible always. Not wanting to read prose is not waiving the right to know
    an email is about to reach a person.
    """
    default = "default"
    hidden_draft = "hidden_draft"
    brief = "brief"
    detailed = "detailed"


@dataclass
class ExecutiveTurn:
    """One turn, fully understood. Every field is a proposal; none is a permission."""
    mode: Mode = Mode.conversation
    reading: SemanticTurn | None = None        # the raw model read this was assembled from
    derivation: Derivation | None = None       # how the runtime would run it

    target_goal_id: str | None = None
    target_decision_id: str | None = None

    proposed_goal_kind: str | None = None
    proposed_operation_id: str | None = None   # EXACT registry id, or None. Never a description.

    slot_patches: tuple[SlotPatch, ...] = field(default_factory=tuple)
    referenced_people: tuple[ReferencedPerson, ...] = field(default_factory=tuple)
    referenced_artifacts: tuple[str, ...] = field(default_factory=tuple)
    requested_edits: tuple[RequestedEdit, ...] = field(default_factory=tuple)

    operation_polarity: str = "neutral"        # neutral | affirm | reject | ambiguous
    presentation: Presentation = Presentation.default

    # ADVISORY. `goal_slots` computes the real gap from the Fill taxonomy, because a model asked what it
    # needs will ask for things it could safely have written — which is how Bruce came to ask a student
    # for a "subject" and a "body".
    missing_information: tuple[str, ...] = field(default_factory=tuple)
    response_intent: str | None = None         # what the reply should ACCOMPLISH, never the prose itself

    confidence: float = 0.0
    supporting_spans: tuple[str, ...] = field(default_factory=tuple)

    # Set by the EXECUTIVE, never by the model: what validation rejected, so a shadow comparison can say
    # why a reading was downgraded instead of silently reporting a clarification.
    validation_notes: tuple[str, ...] = field(default_factory=tuple)

    # THE SAME FACTS, AS CLOSED VOCABULARY. `validation_notes` are prose written for a human reading an
    # eval, and they interpolate whatever the model proposed — an operation id can be a whole sentence
    # about a named teacher, because the model is free to emit prose where an id was asked for. That is
    # fine in an offline eval and NOT fine in a telemetry row, which is why the durable shadow record
    # persists these codes instead. A parallel field rather than a parsed one on purpose: deriving codes
    # by matching note prefixes would go silently wrong the day someone rewords a note, and "silently
    # wrong" is how a privacy rule stops holding.
    validation_codes: tuple[str, ...] = field(default_factory=tuple)

    def is_actionable(self) -> bool:
        """Does this turn want work that needs durable state?

        Deliberately NOT "may we execute" — that belongs to the execution boundary and the authorization
        evidence, neither of which consults this object.
        """
        return self.mode in (Mode.new_goal, Mode.continue_goal)
