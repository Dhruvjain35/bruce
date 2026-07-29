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

ONE SEND, TWO ROWS — and why the second one is now a CHILD. This loop opens its OWN `agent_runs` row for
the provider call, and since `goal_handler` started leading the outcome handlers there is also a CANONICAL
goal run: the row that holds the typed slots, the Decision, and the student's consent. Both rows are
correct to exist — one is a GOAL, one is an ATTEMPT — but nothing joined them, so a single thank-you email
left two unrelated rows and "what is this student working on" answered TWO. That is the mirror image of
the transcript's failure (twenty-two turns, ZERO agent_runs), and it is the regression a fix for it
produces: the run whose absence was the bug becomes a run that is counted twice.

Worse than the count, the ATTEMPT was created LAST and could outlive the turn. `goal_runtime.open_goal`
answers "the newest run that is not closed", and a blocked attempt is not closed — so the next turn's
continuation would land on the audit row for a send instead of on the goal that owns the send, and the
audit row has no slots to answer into. See `_execution_goal` and `_persisted_status` for the two rules
that make that structurally impossible.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from . import agent_run_store, execution_gate, mutation_gateway, tool_registry
from .runtime_contracts import ActionType, ExecutionClass, NextAction, ToolOutcome, ToolResult

log = logging.getLogger("bruce.agent_loop")   # content-free: run ids / statuses only, never user text


class CapabilityExecutor(Protocol):
    """What a domain plugs in so the general loop can drive it. `execute` MUST return a ToolResult whose
    `verified` is set only by an independent read-back — the loop trusts it verbatim and never re-judges."""
    domain: str
    capability: str
    # How the execution gate names this executor's provider operation, and the exact arguments an
    # authorization must be bound to. Kept separate from `build_action()` because the run record and the
    # authorization describe different things: the run says what Bruce set out to do, the gate says what
    # is about to reach the provider. Where those two disagree, the gate's version is the one that matters.
    gate_provider: str
    gate_operation: str

    def goal(self) -> dict: ...
    def build_action(self) -> NextAction: ...
    def gate_arguments(self) -> dict: ...
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


# --- an attempt is not a goal ---------------------------------------------------------------------------
# The two keys every run this loop creates carries in its OWN goal blob. A blob key rather than a column
# because it needs no migration and Postgres indexes and filters JSONB perfectly well — the link is a
# first-class query, not a comment:
#
#     SELECT id FROM agent_runs WHERE user_id = :u AND goal->>'parent_run_id' = :canonical_run_id;
#     SELECT id FROM agent_runs WHERE user_id = :u AND NOT (goal @> '{"execution_run": true}');
#
# (A column with a self-FK would also work and would be cheaper to index at scale; it is a strictly
# additive migration on top of this, because the key stays the thing callers read.)

EXECUTION_RUN_KEY = "execution_run"    # true => this row RECORDS a provider call; it is never a goal
PARENT_RUN_KEY = "parent_run_id"       # the canonical goal run this attempt was made on behalf of


def is_execution_run(run: Mapping | None) -> bool:
    """Whether a run row is an execution ATTEMPT rather than a goal.

    Exists so goal selection can exclude attempts BY CONSTRUCTION. Today the exclusion is accidental —
    `conversation_runtime._open_goal_row` skips a run whose goal blob has no typed slot block, and an
    executor's `goal()` happens not to write one. That holds only for as long as no executor ever
    summarizes itself in a shape `goal_slots` can parse, which is not a property anyone is checking.
    """
    goal = run.get("goal") if isinstance(run, Mapping) else None
    return isinstance(goal, Mapping) and goal.get(EXECUTION_RUN_KEY) is True


def parent_run_id_of(run: Mapping | None) -> str | None:
    """The canonical goal run an attempt belongs to, or None for an attempt made outside a goal
    (`calendar_mutation`, `mission_planner` — both call this loop with no goal row of their own)."""
    goal = run.get("goal") if isinstance(run, Mapping) else None
    parent = goal.get(PARENT_RUN_KEY) if isinstance(goal, Mapping) else None
    return str(parent) if parent else None


def _execution_goal(executor: CapabilityExecutor, *, parent_run_id: str | None,
                    conversation_id: str | None) -> dict:
    """The attempt row's goal blob: the executor's own summary, marked as an attempt and STRIPPED of slots.

    Dropping `goal_slots.SLOT_KEY` is the "no competing slot stores" rule made mechanical rather than
    hoped for. The canonical goal is the one place a recipient lives, because that is the place a
    correction lands; a second row holding its own copy of `to` is how an amendment gets applied to one
    of them and sent from the other.

    The conversation is written into the blob rather than the column on purpose: `agent_runs.conversation_id`
    is a uuid column and several conversation ids in this codebase are channel handles, so
    `agent_run_store._conversation_uuid` RAISES on them — inside `_create_run`'s best-effort try that would
    cost the whole audit row. `goal_runtime._conversation_of` reads this key as its declared fallback. It
    also NARROWS visibility: an unattributed run is visible to every conversation, so before this an
    attempt made in one thread was an open row in all of them.
    """
    from . import goal_runtime, goal_slots
    base = executor.goal()
    goal = dict(base) if isinstance(base, dict) else {}
    goal.pop(goal_slots.SLOT_KEY, None)
    goal[EXECUTION_RUN_KEY] = True
    if parent_run_id:
        goal[PARENT_RUN_KEY] = parent_run_id
    if conversation_id and not goal.get(goal_runtime.CONVERSATION_KEY):
        goal[goal_runtime.CONVERSATION_KEY] = str(conversation_id)
    return goal


def _persisted_status(status: str, *, linked: bool) -> str:
    """The word written to the attempt ROW, which is not always the word returned to the CALLER.

    They differ in exactly one case, and only for a LINKED attempt: `blocked`. `blocked` means resumable,
    and for a standalone attempt that is right — `calendar_mutation` has no other row to carry the "your
    calendar isn't connected" state, so its run stays open and `test_agent_loop` pins that.

    For a child it is wrong in the specific way this module exists to prevent. `blocked` is not in
    `goal_runtime._CLOSED_STATUSES`, so the attempt row stays OPEN forever — nothing resumes it (the
    background claim only takes leased `queued`/`running` rows), and being the newest open row it becomes
    the answer to `open_goal`. The resumable thing is the GOAL: `goal_handler._settle` puts the canonical
    run in `blocked`, which is where the student's reconnect acts. The attempt itself is over, and `failed`
    is what an attempt that did not happen is called.

    Nothing is lost by the rename. `blocked_reason` and the ToolResult's own `unauthorized` outcome are
    written to the same row, so "the provider refused us" stays distinguishable from "the call went wrong"
    without the status column having to carry it.

    A retry under the same idempotency key still resolves to THIS row (that is what the key is for) and
    still cannot advance it — `update_run` refuses `blocked -> completed` and `failed -> completed` alike
    and aborts the whole write, so the row keeps attempt 1's result either way. That is measured, not
    assumed (`test_a_retry_after_a_block_does_not_fork_the_attempt`), and it is unchanged by this
    function: the pre-existing gap is that a retried attempt has no legal edge to its own new outcome, and
    closing it means giving `update_run` an evidence-bearing seam this loop can honestly fill. What the
    rename does change is that the stuck row is CLOSED rather than posing as the student's open goal.
    """
    if linked and status == "blocked":
        return "failed"
    return status


async def _create_run(user_id: UUID, executor: CapabilityExecutor, idempotency_key: str | None,
                      *, parent_run_id: str | None, conversation_id: str | None) -> UUID | None:
    try:
        goal = _execution_goal(executor, parent_run_id=parent_run_id, conversation_id=conversation_id)
        run = await agent_run_store.create_run(user_id, domain=executor.domain, goal=goal,
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
                            idempotency_key: str | None = None,
                            authorization=None, authorization_id: str | None = None,
                            conversation_id: str | None = None,
                            parent_run_id: str | UUID | None = None) -> AgentRunResult:
    """Tier-0 execution: run the executor's VERIFIED tool once and commit the run's terminal state from the
    ToolResult. Never claims done unless the read-back verified it. The AgentRun record is AUDIT/durability
    and strictly BEST-EFFORT — a run-store hiccup must never break, delay, or precede the actual provider
    operation or its honest reply (same discipline as calendar_schedule's best-effort entity record).

    Consent arrives one of two ways, and neither has a default meaning "trusted". A foreground turn
    passes ``authorization`` — evidence just minted from the student's own trusted words, which the
    gateway persists and then reloads. A woken background run passes ``authorization_id`` and nothing
    else: it may not carry the verdict, only the pointer, because the whole point of the wait is that the
    world may have changed inside it. Neither is optional; None for both is a denial and the executor's
    provider call is never reached.

    ``parent_run_id`` is the canonical goal run this call is executing ON BEHALF OF, and a caller that has
    one MUST pass it. The audit run is kept rather than folded into the goal — it holds the idempotency key
    that collapses two simultaneous confirmations onto one attempt, and its terminal word (`completed`, the
    direct-action lane's) is not the goal's (`succeeded`, which requires a read-back the goal never
    performs itself) — so the two rows stay two rows and are JOINED instead. A linked run is an attempt: it
    is marked as one, it carries no slots, and it is CLOSED by the time this returns, whatever happened.
    """
    parent = str(parent_run_id).strip() if parent_run_id else ""
    ruid = await _create_run(user_id, executor, idempotency_key,
                             parent_run_id=parent or None, conversation_id=conversation_id)
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

    # THE EXECUTION GATE. Ahead of the "executing" status, because a denied action never executed and a
    # run record that says it did is a lie told to the next person who reads the audit trail.
    # An executor that does not declare how the gate should name its operation cannot be authorized —
    # there is nothing to bind consent to. Fail shut rather than falling back to `build_action()`, whose
    # arguments describe the RUN and have already been seen to differ from what reaches the provider.
    gate_provider = getattr(executor, "gate_provider", None)
    gate_operation = getattr(executor, "gate_operation", None)
    gate_args = executor.gate_arguments() if hasattr(executor, "gate_arguments") else None

    async def _perform():
        # Faults are turned into a ToolResult HERE rather than escaping into the gateway. An exception
        # crossing the gateway would leave it unable to tell "the provider failed" from "the boundary
        # stopped it", and those two need different replies and different audit rows.
        try:
            return await executor.execute(user_id)
        except execution_gate.UnauthorizedExecution as exc:
            return ToolResult(ToolOutcome.forbidden, executor.capability, action.provider or "",
                              action.operation or "", reason=f"backstop:{exc}"[:200])
        except Exception:
            return ToolResult(ToolOutcome.provider_error, executor.capability, action.provider or "",
                              action.operation or "", reason="executor_raised")

    if execution_gate.unchecked():                       # provider-semantics test; see execution_gate
        result = mutation_gateway.GatewayResult(True, "allow", value=await _perform())
    elif not gate_provider or not gate_operation or gate_args is None:
        result = mutation_gateway.GatewayResult(False, "executor_declares_no_gate_binding")
    else:
        # The gateway is the ONE door. This loop no longer authorizes anything — it reloads by id (or
        # persists freshly minted evidence and then reloads), rechecks the ten conditions against the
        # world as it is now, and only then performs the call. A foreground turn and a woken mission take
        # exactly the same path, so the durable one is not a special case only background code exercises.
        common = dict(provider=gate_provider, operation=gate_operation, arguments=gate_args,
                      perform=_perform, conversation_id=conversation_id, attempt_key=idempotency_key)
        if authorization_id:
            result = await mutation_gateway.execute(user_id, authorization_id=authorization_id, **common)
        else:
            result = await mutation_gateway.execute_with_evidence(user_id, authorization, **common)

    if result.denied:
        tr = ToolResult(ToolOutcome.forbidden, executor.capability, action.provider or "",
                        action.operation or "", reason=f"authorization:{result.reason}")
        await _persist(user_id, ruid, status="failed", current_action=_action_dict(action),
                       last_tool_result=_tool_result_dict(tr),
                       verification_result={"verified": False, "reason": tr.reason},
                       completed_at=datetime.now(timezone.utc))
        log.warning("direct_action_forbidden cap=%s reason=%s", executor.capability, result.reason)
        return AgentRunResult(str(ruid) if ruid else "", "failed", tr, False)

    await _persist(user_id, ruid, status="executing", current_action=_action_dict(action))
    tr = result.value
    if not isinstance(tr, ToolResult):
        tr = ToolResult(ToolOutcome.provider_error, executor.capability, action.provider or "",
                        action.operation or "", reason="executor_returned_no_tool_result")

    status = _status_for(tr)                                  # the ATTEMPT's outcome, as the caller sees it
    persisted = _persisted_status(status, linked=bool(parent))
    fields: dict = {
        "status": persisted,
        "last_tool_result": _tool_result_dict(tr),
        "verification_result": {"verified": tr.verified, "outcome": tr.outcome.value, "reason": tr.reason},
    }
    if status == "blocked":
        # WHY the outcome is recorded even when the status word changed: a reader of this row has to be
        # able to tell "the provider refused us" from "the call went wrong", and that distinction lives
        # here and in `last_tool_result.outcome`, not in the status column.
        fields["blocked_reason"] = (tr.reason or "")[:200]   # blocked_reason column is String(200)
    if persisted != "blocked":
        # `blocked` is the one non-terminal outcome — it needs a reconnect, so a STANDALONE attempt stays
        # active and resumable and gets no completed_at. A linked attempt never lands there (see
        # `_persisted_status`), which is what makes "the child is closed when this returns" total.
        fields["completed_at"] = datetime.now(timezone.utc)
    await _persist(user_id, ruid, **fields)
    return AgentRunResult(str(ruid) if ruid else "", status, tr, tr.verified)
