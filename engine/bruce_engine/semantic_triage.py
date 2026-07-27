"""Stage 1 — fast semantic triage (M2). One small, schema-constrained call that says what the student
MEANS. It never says how the work runs; `execution_derivation` decides that.

WHAT CHANGED AND WHY IT IS NOT COSMETIC.

  Field order. Structured output is generated left to right, so the old schema's leading required
  `execution_class` forced the orchestration token out before the domain and operation tokens that would
  justify it. Here understanding comes first — role, actionability, family, operation — and no execution
  class exists at all.

  Vocabulary. The old prompt asked for `domain (calendar/... or null)`; the model answered "communication"
  for email, at 0.96, and was graded wrong. It was not wrong. Families are semantic and closed, and the
  provider mapping lives in the orchestrator.

  Latency. The measured p95 was 6798ms against a 500ms budget. Three causes, all mechanical, all fixed
  here: gpt-5.x defaults to medium reasoning effort and was burning reasoning tokens on a classification
  (`minimal`); the output schema was prompted rather than provider-native, so a whole JSON object had to
  be produced conversationally and re-parsed (`NativeOutput`); and the per-user capability list sat in the
  system prompt, moving the cacheable prefix on every user (invariant text is now the stable prefix, with
  a fixed cache key).

  Failure honesty. Every failure returns an explicit reason. The old path converted timeout, transport
  error, invalid schema and low confidence alike into fast_conversation, which is precisely why a missed
  executable goal was indistinguishable from a correct chat classification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, NativeOutput

from . import llm
from .semantic_contracts import (Actionability, Family, OperationFamily, SemanticTurn, TriageFailure,
                                 TurnRole)

log = logging.getLogger("bruce.semantic_triage")   # CONTENT-FREE: labels/latency only, never message text

_TRUTHY = {"1", "true", "yes", "on"}

# A hard, short deadline. Routing must be cheap; on breach the caller falls back deterministically.
TRIAGE_TIMEOUT_S = float(os.environ.get("BRUCE_TRIAGE_TIMEOUT_S", "3.0"))
# Reasoning effort for a classification. `minimal` is the single biggest latency lever measured.
TRIAGE_REASONING = os.environ.get("BRUCE_TRIAGE_REASONING", "minimal")
# The output is ~40 tokens. Capping it stops a rambling generation from eating the deadline.
TRIAGE_MAX_TOKENS = int(os.environ.get("BRUCE_TRIAGE_MAX_TOKENS", "200"))


def enabled() -> bool:
    return os.environ.get("BRUCE_ROUTER_SEMANTIC", "").strip().lower() in _TRUTHY


# INVARIANT PREFIX — identical for every user and every turn, so it is a stable prompt-cache prefix.
# Per-user capability families deliberately do NOT appear here; they go in the request body.
_SYSTEM = """You read ONE message from a student to Bruce, their assistant, and describe what it MEANS.
You do not decide how the work runs, you do not answer the message, and you never name a product or provider.

turn_role — what the message IS:
  conversation: chat, greeting, thanks, tutoring, a question about the world
  new_goal: the student wants something done that is not already underway
  continuation: advances or asks about something already underway
  correction: fixes something said or already done
  decision_response: answers a yes/no or a choice Bruce asked for
  cancellation: calls off something pending
  reference_only: points at something ("that one", "the other") with no content of its own

actionability — whether work is implied:
  no_action: nothing to do
  information_only: answerable by thinking; no change to the world
  executable: a concrete change to the world is wanted, once
  durable_monitoring: something must be watched over time and reported back later
  ambiguous: plausibly work, but too underspecified to name

domain_candidates — capability FAMILIES, one normally, two only when genuinely torn:
  calendar: schedules, events, appointments, deadlines, times
  communication: reaching a person — email, message, note, telling someone something
  coursework: assignments, classes, grades
  files: documents, storage
  memory: a durable fact about the student themselves (their timezone, a preference)
  knowledge: answerable by thinking alone
  unknown: does not resolve

operation_family — the verb, provider-neutral:
  create, update, cancel, send, find, monitor, remember, answer, none

confidence — your real belief, 0..1. Report low confidence rather than guessing.
needs_frontier — true only when several dependent goals, a hard referent, or a genuine domain conflict
means a slower, deeper read would decide differently.

Asking someone something and wanting to be told when they answer is communication + send +
durable_monitoring. Putting something on a schedule is calendar + create."""


class _TriageOut(BaseModel):
    """Small on purpose: ~40 output tokens. Understanding first; there is no execution class in here."""
    turn_role: Literal["conversation", "new_goal", "continuation", "correction", "decision_response",
                       "cancellation", "reference_only"]
    actionability: Literal["no_action", "information_only", "executable", "durable_monitoring", "ambiguous"]
    domain_candidates: list[Literal["calendar", "communication", "coursework", "files", "memory",
                                    "knowledge", "unknown"]] = Field(default_factory=list, max_length=2)
    operation_family: Literal["create", "update", "cancel", "send", "find", "monitor", "remember",
                              "answer", "none"] = "none"
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    needs_frontier: bool = False


def _coerce_enum(enum_cls, value, default):
    """Never discard a good read because ONE field is unrecognised. An unknown operation_family must not
    throw away a correct turn_role and actionability — that is the rule the old strict mapper broke."""
    try:
        return enum_cls(value)
    except Exception:
        return default


def to_semantic_turn(out: _TriageOut) -> SemanticTurn:
    families = tuple(_coerce_enum(Family, d, Family.unknown) for d in (out.domain_candidates or []))
    return SemanticTurn(
        turn_role=_coerce_enum(TurnRole, out.turn_role, TurnRole.conversation),
        actionability=_coerce_enum(Actionability, out.actionability, Actionability.ambiguous),
        domain_candidates=families,
        operation_family=_coerce_enum(OperationFamily, out.operation_family, OperationFamily.none),
        confidence=float(out.confidence), needs_frontier=bool(out.needs_frontier))


def render_request(text: str, *, live_families: tuple[str, ...] = (), reply_summary: str | None = None,
                   open_task: str | None = None, awaiting_decision: bool = False) -> str:
    """The variable half of the prompt. Content-bounded: the message, plus one line each of runtime state.
    No raw history, no tool schemas, no entity contents."""
    lines = [f"MESSAGE: {text}"]
    if reply_summary:
        lines.append(f"REPLYING TO: {reply_summary}")
    if open_task:
        lines.append(f"ALREADY UNDERWAY: {open_task}")
    if awaiting_decision:
        lines.append("Bruce has asked the student a yes/no question that is still unanswered.")
    if live_families:
        # Stated as context, not as a constraint on meaning: a student can ask for something Bruce cannot
        # do, and the honest handling of that is the orchestrator's job, not a distortion of the reading.
        lines.append("Families connected for this student: " + ", ".join(live_families)
                     + ". Describe what was meant even if it is not connected.")
    return "\n".join(lines)


class SemanticTriage:
    """One fast structured call. No vision, no tools, no retry on timeout."""

    provider = "openai"
    model = llm.MODEL_ROUTING

    def __init__(self, *, reasoning: str | None = None, max_tokens: int | None = None):
        self._reasoning = reasoning or TRIAGE_REASONING
        self._max_tokens = max_tokens or TRIAGE_MAX_TOKENS

    def _agent(self) -> Agent:
        from pydantic_ai.models.openai import OpenAIChatModelSettings
        settings = OpenAIChatModelSettings(
            openai_reasoning_effort=self._reasoning,   # a classification does not need reasoning tokens
            max_tokens=self._max_tokens,
            openai_prompt_cache_key="bruce-semantic-triage-v1",   # stable: the prefix never varies
        )
        return Agent(llm.routing_model(), output_type=NativeOutput(_TriageOut),
                     system_prompt=_SYSTEM, model_settings=settings)

    async def read(self, body: str) -> SemanticTurn:
        result = await self._agent().run(body)
        return to_semantic_turn(result.output)


async def triage(body: str, *, provider: SemanticTriage | None = None, timeout_s: float | None = None,
                 min_confidence: float = 0.0) -> SemanticTurn | TriageFailure:
    """ONE Stage-1 read under a hard deadline, with a single retry reserved for transport failure.

    Retry policy, deliberately narrow: a transport error is a lost packet and retrying is nearly free; a
    timeout means the budget is already spent, so retrying it would double the very latency being
    protected; a validation failure would return the same answer twice."""
    provider = provider or SemanticTriage()
    deadline = timeout_s if timeout_s is not None else TRIAGE_TIMEOUT_S
    t0 = time.perf_counter()

    for attempt in (1, 2):
        try:
            turn = await asyncio.wait_for(provider.read(body), timeout=deadline)
        except asyncio.TimeoutError:
            ms = (time.perf_counter() - t0) * 1000.0
            log.info("triage failed=timeout attempt=%d ms=%.0f", attempt, ms)
            return TriageFailure("timeout", ms)
        except Exception as exc:
            ms = (time.perf_counter() - t0) * 1000.0
            transport = isinstance(exc, (ConnectionError, OSError)) or "connection" in type(exc).__name__.lower()
            if transport and attempt == 1:
                log.info("triage retry=transport ms=%.0f", ms)      # NO exception text: it can echo the message
                continue
            log.info("triage failed=%s ms=%.0f", "transport" if transport else "invalid_schema", ms)
            return TriageFailure("transport" if transport else "invalid_schema", ms)

        ms = (time.perf_counter() - t0) * 1000.0
        if turn.confidence < min_confidence:
            log.info("triage failed=low_confidence conf=%.2f ms=%.0f", turn.confidence, ms)
            return TriageFailure("low_confidence", ms, partial=turn)
        log.info("triage ok role=%s act=%s fam=%s op=%s conf=%.2f ms=%.0f", turn.turn_role.value,
                 turn.actionability.value, [f.value for f in turn.domain_candidates],
                 turn.operation_family.value, turn.confidence, ms)
        return turn

    return TriageFailure("transport", (time.perf_counter() - t0) * 1000.0)   # unreachable; total by construction
