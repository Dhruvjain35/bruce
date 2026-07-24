"""Planner tiers (Phase D) — the tier selection, fail-closed validation (the planner may only use tools the
broker offered, with args matching the compact schema), Tier-1 single-action planning, and Tier-2 TaskGraph
execution that VERIFIES each step, replans ONLY on a failure, and never after a success. Fakes stand in for
the model + executor so this measures the planning machinery, not the DB/model."""

from __future__ import annotations

import asyncio

from bruce_engine import planner, tool_broker
from bruce_engine.runtime_contracts import ActionType, ExecutionClass, NextAction, Risk, ToolOutcome, ToolResult


def _run(c):
    return asyncio.run(c)


def _cand(capability="calendar.update_event", *, available=True, status="ok",
          arg_schema=None):
    return tool_broker.ToolCandidate(
        capability=capability, provider="google_calendar", operation=capability.split(".")[1],
        write=True, live=True, available=available, status=status, reversible=True, score=1.0,
        required_scopes=(), arg_schema=(arg_schema if arg_schema is not None
                                        else {"target_entity_id": "str", "new_start": "datetime",
                                              "new_end": "datetime?"}),
        reason="test")


def _shortlist(*cands, action=None):
    cs = cands or (_cand(),)
    return tool_broker.ToolShortlist(candidates=tuple(cs), domain="calendar", action=action,
                                     has_actionable=any(c.available for c in cs))


def _action(capability="calendar.update_event", **args):
    return NextAction(type=ActionType.call_tool, capability=capability, provider="google_calendar",
                      operation=capability.split(".")[1], arguments=args, risk=Risk.medium)


def _ok(op="update_event"):
    return ToolResult(ToolOutcome.ok, "calendar.update_event", "google_calendar", op, verified=True)


def _fail(op="update_event"):
    return ToolResult(ToolOutcome.verification_failed, "calendar.update_event", "google_calendar", op,
                      verified=False, reason="read-back mismatch")


# --- tier selection -------------------------------------------------------------------------------

def test_select_tier():
    assert planner.select_tier(ExecutionClass.direct_action) == planner.TIER_DETERMINISTIC
    assert planner.select_tier(ExecutionClass.foreground_agent) == planner.TIER_ONE_ACTION
    assert planner.select_tier(ExecutionClass.foreground_agent, needs_deeper_planning=True) == planner.TIER_TASKGRAPH
    assert planner.select_tier(ExecutionClass.background_mission) == planner.TIER_BACKGROUND


# --- fail-closed validation -----------------------------------------------------------------------

def test_validate_action_accepts_valid():
    ok, _ = planner.validate_action(_action(target_entity_id="e1", new_start="2026-07-25T21:00:00"), _shortlist())
    assert ok


def test_validate_action_fails_closed():
    sl = _shortlist()
    # capability the broker never offered
    ok, why = planner.validate_action(_action("calendar.telepathy"), sl)
    assert not ok and "not offered" in why
    # not actionable (disconnected)
    ok, why = planner.validate_action(_action(target_entity_id="e1", new_start="x"),
                                      _shortlist(_cand(available=False, status="disconnected")))
    assert not ok and "not actionable" in why
    # missing required arg
    ok, why = planner.validate_action(_action(target_entity_id="e1"), sl)   # missing new_start
    assert not ok and "missing required arg" in why
    # unknown arg
    ok, why = planner.validate_action(_action(target_entity_id="e1", new_start="x", nonsense=1), sl)
    assert not ok and "unknown args" in why


# --- Tier 1: one model action ---------------------------------------------------------------------

class FakePlanner:
    def __init__(self, action):
        self._action = action
    async def plan_action(self, *, goal, tools):
        assert tools and all("arg_schema" in t for t in tools)   # broker handed schemas, not the registry
        return self._action


def _patch_shortlist(sl):
    async def _sl(user_id, *, domain=None, action=None, candidate_capabilities=(), limit=4):
        return sl
    return _sl


def test_plan_tier1_valid(monkeypatch):
    monkeypatch.setattr(tool_broker, "shortlist", _patch_shortlist(_shortlist()))
    fp = FakePlanner(_action(target_entity_id="e1", new_start="2026-07-25T21:00:00"))
    r = _run(planner.plan_tier1("u", domain="calendar", action=None, goal={"x": 1}, planner_model=fp))
    assert r.status == "ok" and r.action is not None


def test_plan_tier1_no_actionable_is_honest(monkeypatch):
    monkeypatch.setattr(tool_broker, "shortlist",
                        _patch_shortlist(_shortlist(_cand(available=False, status="disconnected"))))
    r = _run(planner.plan_tier1("u", domain="calendar", action=None, goal={}, planner_model=FakePlanner(_action())))
    assert r.action is None and r.status == "disconnected"     # no plan; honest status, not a guess


def test_plan_tier1_invalid_action_fails_closed(monkeypatch):
    monkeypatch.setattr(tool_broker, "shortlist", _patch_shortlist(_shortlist()))
    fp = FakePlanner(_action("calendar.telepathy"))            # a tool the broker didn't offer
    r = _run(planner.plan_tier1("u", domain="calendar", action=None, goal={}, planner_model=fp))
    assert r.action is None and r.status == "invalid"


# --- Tier 2: bounded TaskGraph --------------------------------------------------------------------

class SeqExecutor:
    """Runs each action, returning scripted ToolResults; records call order (proves REAL execution)."""
    def __init__(self, results):
        self._results = list(results)
        self.calls = []
    async def execute(self, action):
        self.calls.append(action.arguments.get("id"))
        return self._results.pop(0)


def _node(nid, *, deps=(), aid=None):
    return planner.TaskNode(nid, _action(target_entity_id="e1", new_start="x", **({"id": aid} if aid else {})),
                            depends_on=deps)


def test_task_graph_runs_in_dependency_order_and_verifies():
    g = planner.TaskGraph((_node("a", aid="a"), _node("b", deps=("a",), aid="b")))
    ex = SeqExecutor([_ok(), _ok()])
    sl = _shortlist(_cand(arg_schema={"target_entity_id": "str", "new_start": "datetime", "id": "str?"}))
    r = _run(planner.execute_task_graph("u", g, executor=ex, shortlist=sl))
    assert r.status == "ok" and r.completed == ["a", "b"] and ex.calls == ["a", "b"]   # ordered, both verified


def test_task_graph_stops_on_unverified_step_no_replan_after_success():
    g = planner.TaskGraph((_node("a", aid="a"), _node("b", deps=("a",), aid="b")))
    ex = SeqExecutor([_ok(), _fail()])                        # a verifies, b fails
    sl = _shortlist(_cand(arg_schema={"target_entity_id": "str", "new_start": "datetime", "id": "str?"}))
    r = _run(planner.execute_task_graph("u", g, executor=ex, shortlist=sl))
    assert r.status == "failed" and r.failed == "b" and r.completed == ["a"]


def test_task_graph_replans_once_on_failure():
    g = planner.TaskGraph((_node("a", aid="a"),))
    ex = SeqExecutor([_fail(), _ok()])
    sl = _shortlist(_cand(arg_schema={"target_entity_id": "str", "new_start": "datetime", "id": "str?"}))
    calls = {"n": 0}

    async def _replan(node, tr):
        calls["n"] += 1
        return planner.TaskGraph((_node("a2", aid="a2"),))     # a corrected single-step plan

    r = _run(planner.execute_task_graph("u", g, executor=ex, shortlist=sl, replan=_replan))
    assert calls["n"] == 1 and r.status == "ok" and r.completed == ["a2"]   # replanned once, then succeeded


def test_task_graph_fails_closed_on_cycle_and_oversize():
    sl = _shortlist()
    cyclic = planner.TaskGraph((_node("a", deps=("b",)), _node("b", deps=("a",))))
    assert _run(planner.execute_task_graph("u", cyclic, executor=SeqExecutor([]), shortlist=sl)).status == "invalid"
    big = planner.TaskGraph(tuple(_node(str(i)) for i in range(planner.MAX_GRAPH_NODES + 1)))
    assert _run(planner.execute_task_graph("u", big, executor=SeqExecutor([]), shortlist=sl)).status == "invalid"


def test_task_graph_fails_closed_on_invalid_action_before_executing():
    bad = planner.TaskGraph((planner.TaskNode("x", _action("calendar.telepathy")),))
    ex = SeqExecutor([_ok()])
    r = _run(planner.execute_task_graph("u", bad, executor=ex, shortlist=_shortlist()))
    assert r.status == "invalid" and ex.calls == []           # nothing executed
