"""Planner tiers (Phase D) — the tiered planning the FastRouter's execution_class selects into, so Bruce
runs the CHEAPEST plan that fits, and only escalates when it must:

  Tier 0  deterministic single verified call        (direct_action)              -> agent_loop.run_direct_action
  Tier 1  ONE model-generated NextAction             (foreground, single action)  -> plan_tier1 + the loop
  Tier 2  a bounded multi-step TaskGraph             (foreground, multi-step)     -> execute_task_graph
  Tier 3  a durable background mission               (background_mission)         -> enqueue (Phase E runner)

Invariants:
  * the planner receives tools ONLY from the ToolBroker — never the registry, never a hand-coded capability;
  * every proposed action is validated against the broker's availability + compact arg schema — an invalid,
    unknown, unavailable, or over-broad plan FAILS CLOSED (nothing executes);
  * a step's ToolResult.verified gates progression, and we REPLAN only on a real trigger (tool failure /
    verification failure / correction / dependency change) — NEVER after a successful step;
  * idempotency + verification are the executor's (agent_loop / calendar_tools) — the planner threads them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from . import tool_broker
from .runtime_contracts import ActionType, ExecutionClass, NextAction, Risk, ToolResult


def planner_model_enabled() -> bool:
    """Whether Tier-1/2 use the real model planner (else a caller must inject one / stay Tier 0)."""
    return os.environ.get("BRUCE_PLANNER_MODEL", "").strip().lower() in {"1", "true", "yes", "on"}

TIER_DETERMINISTIC, TIER_ONE_ACTION, TIER_TASKGRAPH, TIER_BACKGROUND = 0, 1, 2, 3
MAX_GRAPH_NODES = 8      # a bounded TaskGraph — Tier 2 is "small multi-step", not open-ended orchestration


def select_tier(execution_class: ExecutionClass, *, needs_deeper_planning: bool = False) -> int:
    """The cheapest tier for a routed request. direct_action -> deterministic (Tier 0) unless it explicitly
    needs deeper planning; foreground -> one action (Tier 1) or a graph (Tier 2); background -> Tier 3."""
    if execution_class == ExecutionClass.background_mission:
        return TIER_BACKGROUND
    if execution_class == ExecutionClass.foreground_agent:
        return TIER_TASKGRAPH if needs_deeper_planning else TIER_ONE_ACTION
    if execution_class == ExecutionClass.direct_action:
        return TIER_TASKGRAPH if needs_deeper_planning else TIER_DETERMINISTIC
    return TIER_DETERMINISTIC


# --- fail-closed validation: every action must match a broker candidate + its compact schema -----

def validate_action(action: NextAction, shortlist: tool_broker.ToolShortlist) -> tuple[bool, str]:
    """An action is valid ONLY if its capability is in the broker's shortlist AND actionable AND its args
    satisfy the tool's compact schema (all required present, no unknown keys). Anything else fails closed."""
    if action is None or not action.capability:
        return False, "no capability"
    cand = next((c for c in shortlist.candidates if c.capability == action.capability), None)
    if cand is None:
        return False, "capability not offered by broker"        # never let the planner invent a tool
    if not cand.available:
        return False, f"not actionable ({cand.status})"
    schema = cand.arg_schema or {}
    args = action.arguments or {}
    for name, typ in schema.items():
        if not str(typ).endswith("?") and name not in args:
            return False, f"missing required arg '{name}'"
    unknown = set(args) - set(schema)
    if unknown:
        return False, f"unknown args {sorted(unknown)}"
    return True, "ok"


# --- Tier 1: one model-generated NextAction ------------------------------------------------------

class PlannerModel(Protocol):
    """Produces ONE NextAction for a goal, choosing ONLY from the tools handed to it (broker candidates)."""
    async def plan_action(self, *, goal: dict, tools: list[dict]) -> NextAction: ...


@dataclass(frozen=True)
class PlanResult:
    action: NextAction | None
    status: str            # ok | unsupported | disconnected | insufficient_scope | no_tool | invalid
    reason: str = ""


async def plan_tier1(user_id, *, domain, action, goal: dict, planner_model: PlannerModel,
                     candidate_capabilities: tuple[str, ...] = ()) -> PlanResult:
    """Tier 1: shortlist tools via the broker, ask the model for ONE action, validate it fail-closed. If no
    tool is actionable, return the broker's honest status (no plan) instead of guessing."""
    sl = await tool_broker.shortlist(user_id, domain=domain, action=action,
                                     candidate_capabilities=candidate_capabilities)
    actionable = sl.actionable()
    if not actionable:
        status = sl.candidates[0].status if sl.candidates else "no_tool"
        return PlanResult(None, status, reason="no actionable tool")
    tools = [{"capability": c.capability, "operation": c.operation, "arg_schema": c.arg_schema}
             for c in actionable]
    try:
        proposed = await planner_model.plan_action(goal=goal, tools=tools)
    except Exception:
        return PlanResult(None, "invalid", reason="planner error")
    ok, why = validate_action(proposed, sl)
    if not ok:
        return PlanResult(None, "invalid", reason=why)          # fail closed
    return PlanResult(proposed, "ok")


# --- Tier 2: a bounded multi-step TaskGraph ------------------------------------------------------

class CompactPlannerModel:
    """The production Tier-1 planner (PlannerModel): a fast structured call that chooses ONE capability from
    the offered tools and fills its args from that tool's compact schema — the model never sees the registry
    or invents a capability. Output is validated fail-closed by plan_tier1 regardless."""
    provider = "openai"

    async def plan_action(self, *, goal: dict, tools: list[dict]) -> NextAction:
        from pydantic import BaseModel, Field
        from pydantic_ai import Agent, PromptedOutput
        from . import llm

        offered = {t["capability"]: t for t in tools}

        class _Plan(BaseModel):
            capability: str
            arguments: dict = Field(default_factory=dict)

        tool_lines = "\n".join(f"- {t['capability']}: args {t['arg_schema']}" for t in tools)
        system = ("You plan ONE tool call for Bruce. Choose exactly one capability from the offered tools and "
                  "fill its arguments from that tool's schema (a '?' suffix marks an optional arg). Use ONLY "
                  "an offered capability; never invent one. Offered tools:\n" + tool_lines)
        agent = Agent(llm.routing_model(), output_type=PromptedOutput(_Plan), system_prompt=system)
        result = await agent.run(f"GOAL: {goal}")
        out = result.output
        spec = offered.get(out.capability)
        op = spec["operation"] if spec else out.capability.split(".")[-1]
        return NextAction(type=ActionType.call_tool, capability=out.capability, provider="google_calendar",
                          operation=op, arguments=dict(out.arguments or {}), risk=Risk.medium)


@dataclass(frozen=True)
class TaskNode:
    id: str
    action: NextAction
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskGraph:
    nodes: tuple[TaskNode, ...]


class StepExecutor(Protocol):
    async def execute(self, action: NextAction) -> ToolResult: ...


@dataclass
class GraphResult:
    completed: list[str] = field(default_factory=list)
    failed: str | None = None
    results: dict = field(default_factory=dict)     # node_id -> ToolResult
    status: str = "ok"                              # ok | invalid | failed
    reason: str = ""


def _topo_order(graph: TaskGraph) -> list[TaskNode] | None:
    """Kahn's algorithm — dependency order, or None on a cycle / dangling dependency (fail closed)."""
    by_id = {n.id: n for n in graph.nodes}
    if len(by_id) != len(graph.nodes):
        return None                                             # duplicate ids
    indeg = {n.id: 0 for n in graph.nodes}
    for n in graph.nodes:
        for d in n.depends_on:
            if d not in by_id:
                return None                                     # dangling dependency
            indeg[n.id] += 1
    ready = [n for n in graph.nodes if indeg[n.id] == 0]
    order: list[TaskNode] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in graph.nodes:
            if n.id in m.depends_on:
                indeg[m.id] -= 1
                if indeg[m.id] == 0:
                    ready.append(m)
    return order if len(order) == len(graph.nodes) else None    # leftover -> cycle


async def execute_task_graph(user_id, graph: TaskGraph, *, executor: StepExecutor,
                             shortlist: tool_broker.ToolShortlist, replan=None) -> GraphResult:
    """Tier 2: validate the WHOLE graph fail-closed (bound, schema, acyclic) BEFORE executing anything, then
    run it in dependency order — each step must VERIFY before its dependents run. On an unverified step,
    call `replan` once if provided (a real trigger); a verified step NEVER triggers a replan."""
    res = GraphResult()
    if not graph.nodes:
        return res
    if len(graph.nodes) > MAX_GRAPH_NODES:
        res.status, res.reason = "invalid", f"graph exceeds {MAX_GRAPH_NODES} nodes"
        return res
    for n in graph.nodes:                                       # fail closed: no execution on any bad action
        ok, why = validate_action(n.action, shortlist)
        if not ok:
            res.status, res.failed, res.reason = "invalid", n.id, why
            return res
    order = _topo_order(graph)
    if order is None:
        res.status, res.reason = "invalid", "cyclic or dangling dependency"
        return res

    done: set[str] = set()
    replanned = False
    for node in order:
        if not all(d in done for d in node.depends_on):         # an upstream step failed -> stop
            res.status, res.failed, res.reason = "failed", node.id, "unmet dependency"
            return res
        tr = await executor.execute(node.action)
        res.results[node.id] = tr
        if tr.verified:
            done.add(node.id)
            res.completed.append(node.id)                       # success -> do NOT replan, continue
        else:
            if replan is not None and not replanned:            # real trigger: a step failed verification
                replanned = True
                new_graph = await replan(node, tr)
                if new_graph is not None:
                    return await execute_task_graph(user_id, new_graph, executor=executor,
                                                    shortlist=shortlist, replan=None)   # one replan only
            res.status, res.failed, res.reason = "failed", node.id, tr.reason or "verification failed"
            return res
    return res
