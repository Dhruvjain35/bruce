"""SHADOW MODE — run the semantic executive beside the live router and record the difference.

WHY SHADOW BEFORE AUTHORITY. The synthetic corpus says the executive reads held-out phrasings at 97-99%.
That is a claim about a corpus someone wrote, and the thing it cannot tell you is how the executive behaves
on real founder traffic: how often it would refuse something that currently works, how often it would deny
a capability that is live, and how often it disagrees with the router that is answering students today.
Turning on authority from a synthetic score is the "unproven flag" this whole change exists to avoid.

WHAT SHADOW IS ALLOWED TO DO. Read, compute, and write ONE row. Nothing else, and the constraint is
structural rather than disciplinary — `observe` takes no session that could transition a run, holds no
gateway, and returns None. It cannot:

  * create or continue a goal          (it never calls agent_run_store)
  * create or resolve a Decision       (it never calls transitions or approvals)
  * authorize anything                 (it never calls authorization_evidence)
  * call a provider                    (it never touches an adapter or MutationGateway)
  * send an alternate reply            (it returns nothing the response layer reads)

The live turn is completely unaffected: `observe` is called for its side effect on a telemetry table, and
every failure inside it is swallowed. A shadow that can break a student's turn is worse than no shadow —
observability must never be the thing that takes Bruce down.

WHAT IT RECORDS. The executive's reading, the live router's decision, and whether they AGREE — plus the
four numbers that decide whether authority is safe:

  false action            the executive proposed a WRITE the router did not, on a turn that is not a request
  false refusal           the executive refused where the router proceeded
  false capability denial the executive found nothing reachable for a turn whose family IS connected
  disagreement            any divergence, so the rate is visible rather than inferred from the other three

Content-free logging: labels, ids and latencies only, never message text. The shadow table stores the
student's text because comparing readings without it is useless, and it is RLS-scoped like everything else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

log = logging.getLogger("bruce.semantic_shadow")   # CONTENT-FREE: labels and latency only

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """Shadow recording, off by default.

    Separate from `BRUCE_ROUTER_SEMANTIC` on purpose: that flag is about AUTHORITY, this one is about
    OBSERVATION, and conflating them would mean the only way to measure the executive is to let it decide.
    """
    return os.environ.get("BRUCE_SEMANTIC_SHADOW", "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class ShadowRecord:
    """One turn, read both ways. Pure data — nothing here can act."""

    user_id: str
    message_id: str | None
    text: str

    # what the executive understood
    exec_mode: str
    exec_operation: str | None
    exec_polarity: str
    exec_confidence: float
    exec_goal_id: str | None
    exec_missing: tuple[str, ...]
    exec_notes: tuple[str, ...]
    exec_latency_ms: int

    # what production actually did
    router_class: str | None
    router_action: str | None
    router_domain: str | None
    router_capabilities: tuple[str, ...]
    router_source: str | None

    # the comparison
    agrees: bool
    divergence: str | None


def _router_missed(router_source: str | None) -> bool:
    """Did the live router fall through to the silent default?

    `router_default` is the source Stage 1 stamps when it had no provider — which in deployed staging is
    EVERY turn Stage 0's regexes did not match. This is the population the executive exists to rescue, so
    it is counted separately from ordinary disagreement.
    """
    return router_source == "router_default"


def compare(turn: Any, decision: Any, *, reachable: frozenset[str]) -> tuple[bool, str | None]:
    """Do the executive and the live router agree about this turn? Returns (agrees, divergence-label).

    "Agree" is deliberately coarse: whether both concluded that WORK IS WANTED, and if so whether they
    named the same capability. Comparing finer than that would report disagreement on distinctions the
    student never made — the router has no notion of `answer_question`, and calling that a divergence
    would bury the divergences that matter.
    """
    exec_wants_work = turn.mode.value in ("new_goal", "continue_goal") or turn.proposed_operation_id
    router_caps = tuple(getattr(decision, "candidate_capabilities", ()) or ())
    router_wants_work = getattr(decision, "execution_class", None) in (
        "direct_action", "foreground_agent", "background_mission") or bool(router_caps)

    if not exec_wants_work and not router_wants_work:
        return True, None
    if exec_wants_work and not router_wants_work:
        # THE POPULATION THIS CHANGE EXISTS FOR: the router fell to chat, the executive read a request.
        return False, ("executive_found_work_router_missed" if _router_missed(
            getattr(decision, "source", None)) else "executive_found_work_router_did_not")
    if router_wants_work and not exec_wants_work:
        return False, "router_found_work_executive_did_not"
    if turn.proposed_operation_id and router_caps and turn.proposed_operation_id not in router_caps:
        return False, "different_capability"
    return True, None


async def observe(user_id: UUID, text: str, *, message_id: str | None, context: Any,
                  decision: Any) -> ShadowRecord | None:
    """Read one turn with the executive and record it beside what production did. NEVER acts.

    Returns the record for callers that want it (the offline evaluator); the live runtime ignores the
    return value entirely. Every exception is swallowed — see the module docstring.
    """
    import time

    from . import semantic_executive

    if not enabled():
        return None
    try:
        t0 = time.perf_counter()
        turn = await semantic_executive.interpret(context)
        elapsed = int((time.perf_counter() - t0) * 1000)
        reachable = semantic_executive.reachable_operations(context)
        agrees, divergence = compare(turn, decision, reachable=reachable)

        record = ShadowRecord(
            user_id=str(user_id), message_id=message_id, text=text,
            exec_mode=turn.mode.value, exec_operation=turn.proposed_operation_id,
            exec_polarity=turn.operation_polarity, exec_confidence=turn.confidence,
            exec_goal_id=turn.target_goal_id, exec_missing=tuple(turn.missing_information),
            exec_notes=tuple(turn.validation_notes), exec_latency_ms=elapsed,
            router_class=getattr(getattr(decision, "execution_class", None), "value",
                                 getattr(decision, "execution_class", None)),
            router_action=getattr(getattr(decision, "action", None), "value",
                                  getattr(decision, "action", None)),
            router_domain=getattr(decision, "domain", None),
            router_capabilities=tuple(getattr(decision, "candidate_capabilities", ()) or ()),
            router_source=getattr(decision, "source", None),
            agrees=agrees, divergence=divergence)

        # CONTENT-FREE. The record carries the text (that is its purpose, and it is RLS-scoped); the LOG
        # carries only labels, because logs travel further than tables and are read by more people.
        log.info("shadow mode=%s op=%s agrees=%s divergence=%s ms=%d",
                 record.exec_mode, record.exec_operation, agrees, divergence, elapsed)
        return record
    except Exception:
        log.info("shadow_error")        # never the exception text: it can echo the student's message
        return None
