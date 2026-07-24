"""FastRouter quality harness (G0.1 §3) — execution-class accuracy + false-mission / missed-execution
rates + confusion matrix over a generated dataset (288 real student texts, fixture-free). Deterministic
Stage 0 is exercised with the world state each example ASSUMES (entity_exists / pending_decision / none)
stubbed. Blocks regressions in the high-risk directions: never invent a mission, never miss an action."""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections import Counter, defaultdict
from unittest.mock import patch
from uuid import uuid4

from bruce_engine import entity_resolution, fast_router, mission_kernel
from bruce_engine.entity_resolution import ResolutionResult

DATA = json.load(open(pathlib.Path(__file__).parent / "data" / "router_eval.json"))


def _run(c):
    return asyncio.run(c)


def _predict(ex) -> str:
    st = ex["state"]

    async def _resolve(uid, t):
        return ResolutionResult("resolved", entity={"id": "1", "title": "x"}) if st == "entity_exists" else ResolutionResult("not_found")

    async def _recent(uid):
        return ResolutionResult("resolved", entity={"id": "1"}) if st == "entity_exists" else ResolutionResult("not_found")

    async def _pending(uid):
        return {"mission_id": "m1"} if st == "pending_decision" else None

    with patch.object(entity_resolution, "resolve", _resolve), \
         patch.object(entity_resolution, "resolve_most_recent", _recent), \
         patch.object(mission_kernel, "latest_pending_calendar_mission", _pending):
        d, _t = _run(fast_router.route(uuid4(), ex["text"], has_attachments=False))
    return d.execution_class.value


def test_router_execution_class_quality():
    conf = defaultdict(Counter)
    correct = 0
    misses = []
    for ex in DATA:
        pred = _predict(ex)
        exp = ex["expected_class"]
        conf[exp][pred] += 1
        if pred == exp:
            correct += 1
        else:
            misses.append((ex["text"], exp, pred))
    n = len(DATA)
    acc = correct / n
    fm = sum(conf[c]["background_mission"] for c in conf if c != "background_mission")
    non_bg = sum(sum(conf[c].values()) for c in conf if c != "background_mission")
    false_mission_rate = fm / non_bg if non_bg else 0.0
    missed = conf["direct_action"]["fast_conversation"]
    da = sum(conf["direct_action"].values())
    missed_execution_rate = missed / da if da else 0.0

    print(f"\nROUTER n={n} accuracy={acc:.3f} false_mission_rate={false_mission_rate:.3f} "
          f"missed_execution_rate={missed_execution_rate:.3f}")
    print("CONFUSION", {k: dict(v) for k, v in conf.items()})
    if misses:
        print("SAMPLE_MISSES", misses[:12])

    assert acc >= 0.88, f"router accuracy {acc:.3f} regressed"
    assert false_mission_rate <= 0.02, f"false_mission_rate {false_mission_rate:.3f} too high (inventing missions)"
    assert missed_execution_rate <= 0.08, f"missed_execution_rate {missed_execution_rate:.3f} too high (dropping actions)"
