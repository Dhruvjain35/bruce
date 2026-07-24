"""General AgentRun execution loop (G0.4) — one domain-agnostic understand→act→verify→persist cycle that
finally makes the R1/R2 contracts live. Bruce's direct actions stop being bespoke per-capability branches:
the loop drives a CapabilityExecutor (calendar today, email/drive/canvas next) over the frozen NextAction /
ToolResult vocabulary and records every step to a durable AgentRun.

Tier 0 (this lane): a single known operation on a known entity — deterministic, NO model call. Build the
NextAction, validate the capability is live, run the executor's VERIFIED tool, and commit the run's outcome
straight from the ToolResult. Verification is NEVER fabricated: `verified` comes only from the executor's
independent read-back (calendar_tools does the readback + _matches). Higher tiers (a model planner for
ambiguous/multi-step foreground work) are a declared seam — see planning_tier() — not built here, because
with only the calendar hands live there is nothing multi-step to plan yet.

Crucially this REUSES the verified provider I/O (calendar_tools) rather than reimplementing it, so the
live-verified calendar behavior is unchanged; the loop only adds the general orchestration + durable run
around it. The executor owns the provider call; the loop owns the state machine, validation, and audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from . import agent_run_store, tool_registry
from .runtime_contracts import ActionType, ExecutionClass, NextAction, ToolOutcome, ToolResult

log = logging.getLogger("bruce.agent_loop")   # content-free: run ids / statuses only, never user text


class CapabilityExecutor(Protocol):
    """What a domain plugs in so the general loop can drive it. `execute` MUST return a ToolResult whose
    `verified` is set only by an independent read-back — the loop trusts it verbatim and never re-judges."""
    domain: str
    capability: str

    def goal(self) -> dict: ...
    def build_action(self) -> NextAction: ...
    async def execute(self, user_id: UUID) -> ToolResult: ...


@dataclass(frozen=True)
class AgentRunResult:
    run_id: str
    status: str                    # "completed" (verified) | "blocked" (needs reconnect) | "failed"
    tool_result: ToolResult
    verified: bool


def planning_tier(execution_class: ExecutionClass, *, needs_deeper_planning: bool = False) -> int:
    """Which planning tier a request needs. 0 = deterministic single op (direct_action) — no model. 1 = a
    compact planner for a foreground multi-step task the user is waiting on. 2 = deep planning when a
    decision explicitly flags it. Tiers 1/2 are seams until foreground/non-calendar work exists to plan."""
    if needs_deeper_planning:
        return 2
    if execution_class == ExecutionClass.direct_action:
        return 0
    if execution_class in (ExecutionClass.foreground_agent, ExecutionClass.background_mission):
        return 1
    return 0


def _status_for(tr: ToolResult) -> str:
    if tr.verified:
        return "completed"
    if tr.outcome in (ToolOutcome.unauthorized, ToolOutcome.insufficient_scope):
        return "blocked"                                    # reconnect needed — not a failure of the plan
    return "failed"


def _action_dict(a: NextAction) -> dict:
    return {"type": a.type.value, "capability": a.capability, "provider": a.provider,
            "operation": a.operation, "target_entity_id": a.target_entity_id,
            "arguments": a.arguments, "risk": a.risk.value, "reversible": a.reversible}


def _tool_result_dict(tr: ToolResult) -> dict:
    return {"outcome": tr.outcome.value, "capability": tr.capability, "provider": tr.provider,
            "operation": tr.operation, "verified": tr.verified,
            "provider_entity_id": tr.provider_entity_id, "reason": tr.reason}


async def _create_run(user_id: UUID, executor: CapabilityExecutor, idempotency_key: str | None) -> UUID | None:
    try:
        run = await agent_run_store.create_run(user_id, domain=executor.domain, goal=executor.goal(),
                                               idempotency_key=idempotency_key)
        return run["id"] if isinstance(run["id"], UUID) else UUID(str(run["id"]))
    except Exception:
        log.info("agent_run_create_failed domain=%s", executor.domain)   # audit is best-effort
        return None


async def _persist(user_id: UUID, ruid: UUID | None, **fields) -> None:
    if ruid is None:
        return
    try:
        await agent_run_store.update_run(user_id, ruid, **fields)
    except Exception:
        log.info("agent_run_persist_failed run=%s", ruid)   # never break or precede the verified action


async def run_direct_action(user_id: UUID, *, executor: CapabilityExecutor,
                            idempotency_key: str | None = None) -> AgentRunResult:
    """Tier-0 execution: run the executor's VERIFIED tool once and commit the run's terminal state from the
    ToolResult. Never claims done unless the read-back verified it. The AgentRun record is AUDIT/durability
    and strictly BEST-EFFORT — a run-store hiccup must never break, delay, or precede the actual provider
    operation or its honest reply (same discipline as calendar_schedule's best-effort entity record)."""
    ruid = await _create_run(user_id, executor, idempotency_key)
    action = executor.build_action()

    # Tier-0 validation: never dispatch a capability the registry doesn't declare live. (Availability — the
    # user's live connection — is enforced inside the executor's verified tool, which returns unauthorized.)
    if not tool_registry.is_live(executor.capability):
        tr = ToolResult(ToolOutcome.provider_error, executor.capability, action.provider or "",
                        action.operation or "", reason="capability_not_live")
        await _persist(user_id, ruid, status="failed", current_action=_action_dict(action),
                       last_tool_result=_tool_result_dict(tr),
                       verification_result={"verified": False, "reason": tr.reason},
                       completed_at=datetime.now(timezone.utc))
        return AgentRunResult(str(ruid) if ruid else "", "failed", tr, False)

    await _persist(user_id, ruid, status="executing", current_action=_action_dict(action))

    # THE ACTUAL WORK — outside any audit try/except so its real ToolResult always flows back to the reply.
    try:
        tr = await executor.execute(user_id)
    except Exception:
        tr = ToolResult(ToolOutcome.provider_error, executor.capability, action.provider or "",
                        action.operation or "", reason="executor_raised")

    status = _status_for(tr)
    fields: dict = {
        "status": status,
        "last_tool_result": _tool_result_dict(tr),
        "verification_result": {"verified": tr.verified, "outcome": tr.outcome.value, "reason": tr.reason},
    }
    if status == "blocked":
        # not terminal — needs reconnect, so the run stays active/resumable (no completed_at).
        fields["blocked_reason"] = (tr.reason or "")[:200]   # blocked_reason column is String(200)
    else:
        fields["completed_at"] = datetime.now(timezone.utc)
    await _persist(user_id, ruid, **fields)
    return AgentRunResult(str(ruid) if ruid else "", status, tr, tr.verified)
