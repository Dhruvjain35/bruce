"""Run the open-set semantic evaluation and score every layer separately.

Scored independently, because collapsing them is what produced a 0.385 figure for a model that
understood almost everything:

  turn role · actionability · executable-goal recall · domain-family recall · operation family ·
  correction · continuation · reference · deterministic orchestration · false actions ·
  unnecessary clarification · mechanical fallback · invalid schema · cold and warm latency

Latency is measured SERIALLY. Running 300 calls concurrently would produce a p95 that reflects the
harness's own queueing rather than what a student would wait, and the gate is about the student.

Cold and warm are reported separately and never mixed: the first call of a process pays a TLS
handshake that has already been measured at ~2.8s, and burying it inside a warm distribution would
hide the single largest latency fact about this path.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import Counter

from bruce_engine import execution_derivation as ed
from bruce_engine import semantic_triage
from bruce_engine.semantic_contracts import (Actionability, Family, OperationFamily, TriageFailure,
                                             TurnContext)

from .scenarios import (CTX_ACTIVE, CTX_DISCONNECTED, CTX_NONE, CTX_PENDING, CTX_SCOPE, SCENARIOS,
                        composition)

_ALL_CAPS = frozenset({"calendar.create_event", "calendar.update_event", "calendar.delete_event",
                       "gmail.send_message", "gmail.find_reply"})
_CAL_ONLY = frozenset({"calendar.create_event", "calendar.update_event", "calendar.delete_event"})
_BOTH = frozenset({Family.calendar, Family.communication})

# Context condition -> the deterministic runtime state, and what the model is told is connected.
CONTEXTS = {
    CTX_NONE: (TurnContext(live_families=_BOTH, live_capabilities=_ALL_CAPS),
               ("calendar", "communication"), None, False),
    CTX_PENDING: (TurnContext(live_families=_BOTH, live_capabilities=_ALL_CAPS,
                              pending_decision_id="dec-1", active_run_domain="calendar"),
                  ("calendar", "communication"), "calendar — add chem test friday at 2", True),
    CTX_ACTIVE: (TurnContext(live_families=_BOTH, live_capabilities=_ALL_CAPS,
                             active_run_id="run-1", active_run_domain="gmail"),
                 ("calendar", "communication"), "communication — emailed coach, waiting on a reply", False),
    CTX_DISCONNECTED: (TurnContext(), (), None, False),
    CTX_SCOPE: (TurnContext(live_families=frozenset({Family.calendar}), live_capabilities=_CAL_ONLY),
                ("calendar",), None, False),
}

_ACTION_CLASSES = {"direct_action", "background_mission"}
_EXECUTABLE_EXPECT = {"direct_action", "background_mission"}


def effective_class(d) -> str:
    """A clarifying question is its own outcome. Counting it as chat hides a blocked action; counting
    it as foreground work pretends a question is a task graph."""
    return "clarify" if d.needs_clarification else d.execution_class


async def run_one(scn, provider) -> dict:
    ctx, fams, open_task, awaiting = CONTEXTS[scn.context]
    body = semantic_triage.render_request(scn.text, live_families=fams, open_task=open_task,
                                          awaiting_decision=awaiting)
    t0 = time.perf_counter()
    turn = await semantic_triage.triage(body, provider=provider)
    ms = (time.perf_counter() - t0) * 1000.0

    row = {"sid": scn.sid, "split": scn.split, "context": scn.context, "forms": list(scn.forms),
           "expect": scn.expect, "ms": round(ms, 1)}
    if isinstance(turn, TriageFailure):
        row["fallback"] = turn.reason
        return row

    d = ed.derive(turn, ctx)
    got_fams = {f.value for f in turn.domain_candidates}
    row.update({
        "role": turn.turn_role.value, "role_ok": turn.turn_role.value in scn.turn_roles,
        "act": turn.actionability.value, "act_ok": turn.actionability.value in scn.actionability,
        "fams": sorted(got_fams),
        "fam_ok": bool(got_fams & scn.families) if scn.families else not got_fams,
        "op": turn.operation_family.value, "op_ok": turn.operation_family.value in scn.operations,
        "conf": turn.confidence, "frontier": turn.needs_frontier,
        "class": d.execution_class, "eff": effective_class(d), "rule": d.rule,
        "class_ok": effective_class(d) == scn.expect,
    })
    return row


def _pct(v, p):
    return round(sorted(v)[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 1) if v else None


def score(rows: list[dict]) -> dict:
    graded = [r for r in rows if "fallback" not in r]

    def rate(rs, k):
        return round(sum(1 for r in rs if r.get(k)) / len(rs), 3) if rs else None

    def subset(pred):
        return [r for r in graded if pred(r)]

    # A goal whose correct outcome is a real action must be READ as work. Dropping one to chat is the
    # failure that made Bruce feel dumb, so it is measured on its own.
    should_act = subset(lambda r: r["expect"] in _EXECUTABLE_EXPECT)
    recalled = [r for r in should_act if r["act"] in ("executable", "durable_monitoring")]

    # A control is anything whose correct outcome is NOT an action. It may be answered or questioned;
    # it may never be executed.
    controls = subset(lambda r: r["expect"] not in _EXECUTABLE_EXPECT)
    false_actions = [r for r in controls if r["class"] in _ACTION_CLASSES]

    # Asking when the runtime already had enough to act. The cost of over-asking is a Bruce that
    # interrogates instead of helping, so it is a release metric, not a footnote.
    should_not_ask = subset(lambda r: r["expect"] != "clarify")
    unnecessary = [r for r in should_not_ask if r["eff"] == "clarify"]

    by_role = lambda name: subset(lambda r: r["sid"].startswith(name))  # noqa: E731
    lat = [r["ms"] for r in rows]

    return {
        "n": len(rows), "graded": len(graded),
        "turn_role_accuracy": rate(graded, "role_ok"),
        "actionability_accuracy": rate(graded, "act_ok"),
        "executable_goal_recall": round(len(recalled) / len(should_act), 3) if should_act else None,
        "domain_family_recall": rate(subset(lambda r: r["expect"] in _EXECUTABLE_EXPECT), "fam_ok"),
        "operation_family_accuracy": rate(subset(lambda r: r["expect"] in _EXECUTABLE_EXPECT), "op_ok"),
        "correction_accuracy": rate(by_role("cal_move") + by_role("correct_pending"), "role_ok"),
        "continuation_accuracy": rate(by_role("status_") + by_role("reference_open"), "role_ok"),
        "reference_accuracy": rate(by_role("reference"), "role_ok"),
        "orchestration_accuracy": rate(graded, "class_ok"),
        "false_action_rate": round(len(false_actions) / len(controls), 3) if controls else None,
        "false_actions": [r["sid"] for r in false_actions],
        "unnecessary_clarification_rate": (round(len(unnecessary) / len(should_not_ask), 3)
                                           if should_not_ask else None),
        "mechanical_fallback_rate": round(sum(1 for r in rows if "fallback" in r) / len(rows), 3),
        "fallback_reasons": dict(Counter(r["fallback"] for r in rows if "fallback" in r)),
        "invalid_schema_rate": round(
            sum(1 for r in rows if r.get("fallback") == "invalid_schema") / len(rows), 3),
        "latency_ms": {"p50": _pct(lat, 50), "p95": _pct(lat, 95), "max": _pct(lat, 100),
                       "mean": round(statistics.mean(lat), 1) if lat else None},
        # NOT emitted by the Stage-1 schema. Reported as absent rather than approximated: target
        # extraction and missing-information are full-SemanticTurn fields that only a Stage-2 reasoner
        # produces, and Stage 2 is not built.
        "target_extraction_accuracy": "NOT MEASURED — Stage 1 does not emit targets",
        "missing_information_accuracy": "NOT MEASURED — Stage 1 does not emit missing_information",
    }


async def measure_cold(provider) -> float:
    """The first call of the process, before anything is warm. Measured on its own because it is the
    single largest latency fact about this path and averaging it away would be dishonest."""
    t0 = time.perf_counter()
    try:
        await provider.read(semantic_triage.render_request("hi"))
    except Exception:
        pass
    return round((time.perf_counter() - t0) * 1000.0, 1)


async def run_set(scenarios, provider) -> list[dict]:
    return [await run_one(s, provider) for s in scenarios]      # serial: see module docstring


async def main(benchmark: bool = True):
    print("COMPOSITION_JSON", json.dumps(composition(), indent=1))

    provider = semantic_triage.SemanticTriage()
    cold_ms = await measure_cold(provider)
    print("COLD_START_MS", cold_ms)

    rows = await run_set(SCENARIOS, provider)
    overall = score(rows)
    by_split = {sp: score([r for r in rows if r["split"] == sp])
                for sp in ("development", "hidden", "adversarial")}
    by_context = {c: score([r for r in rows if r["context"] == c])
                  for c in sorted({r["context"] for r in rows})}
    by_form = {}
    for form in sorted({f for r in rows for f in r["forms"]}):
        sub = [r for r in rows if form in r["forms"]]
        s = score(sub)
        by_form[form] = {"n": s["n"], "actionability": s["actionability_accuracy"],
                         "orchestration": s["orchestration_accuracy"],
                         "fallback": s["mechanical_fallback_rate"]}

    print("RESULTS_JSON", json.dumps({
        "cold_start_ms": cold_ms, "warm_overall": overall,
        "by_split": by_split, "by_context": by_context, "by_language_form": by_form,
        "misses": [{"sid": r["sid"], "text_forms": r["forms"], "expect": r["expect"],
                    "got": r.get("eff"), "role": r.get("role"), "act": r.get("act"),
                    "fams": r.get("fams"), "op": r.get("op"), "conf": r.get("conf"),
                    "rule": r.get("rule"), "fallback": r.get("fallback")}
                   for r in rows if not r.get("class_ok")],
    }, indent=1, default=str))

    if not benchmark:
        return

    # ---- model benchmark: the SAME hidden set, one config at a time, serial, cold measured per config
    hidden = [s for s in SCENARIOS if s.split == "hidden"]
    configs = [("gpt-5.4-mini", "none"), ("gpt-5.4-nano", "none"), ("gpt-4.1-mini", None)]
    bench = {}
    for model_id, effort in configs:
        try:
            p = _provider_for(model_id, effort)
            cold = await measure_cold(p)
            rs = await run_set(hidden, p)
            s = score(rs)
            bench[f"{model_id}/{effort or 'n-a'}"] = {
                "cold_ms": cold,
                "actionability": s["actionability_accuracy"],
                "executable_recall": s["executable_goal_recall"],
                "domain_recall": s["domain_family_recall"],
                "operation": s["operation_family_accuracy"],
                "orchestration": s["orchestration_accuracy"],
                "false_action_rate": s["false_action_rate"],
                "fallback": s["mechanical_fallback_rate"],
                "invalid_schema": s["invalid_schema_rate"],
                "latency": s["latency_ms"],
            }
        except Exception as exc:
            bench[f"{model_id}/{effort or 'n-a'}"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    print("BENCHMARK_JSON", json.dumps(bench, indent=1, default=str))


def _provider_for(model_id: str, effort: str | None):
    """A triage provider pinned to one model. Non-reasoning models reject reasoning_effort outright, so
    the setting is omitted rather than guessed — the last time a reasoning value was assumed it 400'd on
    every call."""
    from pydantic_ai import Agent, NativeOutput
    from pydantic_ai.models.openai import OpenAIChatModelSettings

    from bruce_engine import llm

    class Pinned(semantic_triage.SemanticTriage):
        def _agent(self):
            if self._cached is not None:
                return self._cached
            kw = {"max_tokens": semantic_triage.TRIAGE_MAX_TOKENS,
                  "openai_prompt_cache_key": "bruce-semantic-triage-v1"}
            if effort:
                kw["openai_reasoning_effort"] = effort
            self._cached = Agent(llm.openai_model(model_id),
                                 output_type=NativeOutput(semantic_triage._TriageOut),
                                 system_prompt=semantic_triage._SYSTEM,
                                 model_settings=OpenAIChatModelSettings(**kw))
            return self._cached

    return Pinned()


if __name__ == "__main__":
    asyncio.run(main())
