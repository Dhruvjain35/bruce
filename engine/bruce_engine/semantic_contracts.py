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
