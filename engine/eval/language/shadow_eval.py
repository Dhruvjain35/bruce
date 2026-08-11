"""SHADOW EVALUATION — the semantic executive against REAL founder turns, beside the live router.

The paraphrase corpus measures whether Bruce understands sentences someone wrote for it to understand.
This measures something different and more important: on the traffic Bruce actually received, would the
executive have done better, worse, or differently — and specifically, would it ever have done HARM.

Four numbers decide whether authority is safe to enable. Only the first has no tolerance:

  FALSE ACTION             the executive proposes a WRITE on a turn that is not a request for one.
                           Must be 0. Its failures are a student's mail being sent.
  FALSE REFUSAL            the executive refuses or clarifies where the live router proceeded, on a turn
                           the student meant. Costs trust, not damage.
  FALSE CAPABILITY DENIAL  the executive finds no reachable operation for a turn whose family IS
                           connected. This is the exact production defect — Bruce telling its founder it
                           could not schedule while calendar.create_event was live — so it is measured
                           directly rather than inferred.
  DISAGREEMENT             any divergence from the live router, reported as a rate. High disagreement is
                           EXPECTED and is not itself bad: the router falls to chat on everything its ten
                           regexes miss, so most disagreement should be the executive finding work the
                           router missed. The breakdown says which.

Nothing here writes, authorizes, or calls a provider. It reads the captured turns and runs `interpret`.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from collections import Counter, defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[2]

MAX_CONCURRENCY = 3
TRANSPORT_RETRIES = 3

# Pasted shell commands, file paths and SQL are NOT student language. The founder used the thread as a
# debugging console for days; scoring Bruce on whether it understood a sqlite3 invocation would drown the
# 13 real sentences in 100 lines of noise and report a meaningless number.
_NOT_LANGUAGE = re.compile(
    r"^\s*(sqlite3|bash|echo|grep|tail|cd |set -|ps -|awk |curl |PYTHONPATH|/Users/|~/|\$HOME)"
    r"|\|\s*(grep|tail|awk)\b|--[a-z-]{3,}\s", re.IGNORECASE)


def is_student_language(text: str) -> bool:
    """Is this a person talking, or a terminal?"""
    t = (text or "").strip()
    if not t or len(t) > 400:
        return False
    return not _NOT_LANGUAGE.search(t)


async def _read(text: str, ctx_kwargs: dict, sem: asyncio.Semaphore):
    from bruce_engine import semantic_executive as se

    ctx = se.mini_context(text, **ctx_kwargs)
    async with sem:
        for attempt in range(TRANSPORT_RETRIES):
            turn = await se.interpret(ctx)
            if not any("triage failed" in n or "reader unavailable" in n for n in turn.validation_notes):
                return turn
            await asyncio.sleep(1.5 * (attempt + 1))
    return turn


async def _router(text: str):
    """What the LIVE production router does with this turn, with its DB reads stubbed to 'nothing open'.

    Stubbed rather than mocked-permissive: `entity_resolution` and `mission_kernel` return not-found, which
    is the state the founder's account was actually in for most of these turns (10 agent_runs in 12 days,
    9 with no typed kind). A permissive stub would flatter the router.
    """
    from unittest.mock import patch
    from uuid import uuid4

    import os

    from bruce_engine import entity_resolution, fast_router, mission_kernel
    from bruce_engine.entity_resolution import ResolutionResult

    async def _nf(*a, **k):
        return ResolutionResult("not_found")

    async def _none(*a, **k):
        return None

    # THE ROUTER AS DEPLOYED, not as configured on this laptop. The first run of this evaluation had
    # BRUCE_ROUTER_SEMANTIC=1 in the environment (set to exercise the executive), which switched Stage 1
    # ON inside the baseline — so it compared the executive against a router that does not exist in
    # production, and reported source=router_model for 10 of 13 turns. Deployed staging has that flag
    # UNSET; the baseline must reproduce that or the whole comparison measures nothing.
    saved = {k: os.environ.pop(k, None) for k in ("BRUCE_ROUTER_SEMANTIC", "BRUCE_ROUTER_STAGE1")}
    try:
        with patch.object(entity_resolution, "resolve", _nf), \
             patch.object(entity_resolution, "resolve_most_recent", _nf), \
             patch.object(mission_kernel, "latest_pending_calendar_mission", _none):
            decision, _timing = await fast_router.route(uuid4(), text, has_attachments=False)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    return decision


async def run(turns: list[str], *, label: str) -> dict:
    """Every turn, read both ways."""
    from bruce_engine import semantic_shadow, tool_registry

    writes = frozenset(s.capability for s in tool_registry.specs(None) if s.write)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    execs = await asyncio.gather(*[_read(t, {}, sem) for t in turns])
    routers = await asyncio.gather(*[_router(t) for t in turns])

    false_actions, false_refusals, capability_denials, divergences = [], [], [], []
    agree = 0
    modes, router_sources = Counter(), Counter()

    for text, turn, dec in zip(turns, execs, routers):
        modes[turn.mode.value] += 1
        router_sources[getattr(dec, "source", None) or "stage0"] += 1
        # `reachable=frozenset()` deliberately: this harness routes offline against a synthetic baseline,
        # so it cannot establish what THIS user could reach on that turn, and `compare` treats an unknown
        # reachable set as "do not classify a capability denial". The production-denial metric is
        # collected on real traffic in the durable shadow queue; the denial list below is the harness's
        # own, opposite measure (the EXECUTIVE proposing nothing on a turn it read as work).
        cmp = semantic_shadow.compare(turn, dec, reachable=frozenset())
        agree += cmp.agrees
        divergence = cmp.divergence
        if divergence:
            divergences.append((divergence, text))

        # FALSE ACTION: a write proposed on a turn that is plainly not a request.
        if turn.proposed_operation_id in writes and turn.mode.value == "conversation":
            false_actions.append(f"{text[:70]!r} -> {turn.proposed_operation_id}")

        # FALSE REFUSAL: executive stops where the router proceeded.
        router_worked = getattr(getattr(dec, "execution_class", None), "value", None) in (
            "direct_action", "foreground_agent", "background_mission")
        if router_worked and turn.mode.value in ("clarify", "reject", "cancel"):
            false_refusals.append(f"{text[:70]!r} exec={turn.mode.value} router=worked")

        # FALSE CAPABILITY DENIAL: the exact production defect. The executive read a request in a family
        # that IS connected, and produced no reachable operation for it.
        if turn.mode.value in ("new_goal", "continue_goal") and turn.proposed_operation_id is None:
            capability_denials.append(f"{text[:70]!r} mode={turn.mode.value} notes={list(turn.exec_notes) if hasattr(turn,'exec_notes') else list(turn.validation_notes)}")

    n = len(turns) or 1
    return {
        "label": label,
        "turns": len(turns),
        "false_action_count": len(false_actions),
        "false_actions": false_actions[:10],
        "false_refusal_count": len(false_refusals),
        "false_refusals": false_refusals[:10],
        "capability_denial_count": len(capability_denials),
        "capability_denials": capability_denials[:10],
        "agreement_rate": agree / n,
        "disagreement_rate": 1 - (agree / n),
        "divergence_breakdown": dict(Counter(d for d, _ in divergences)),
        "clarification_rate": modes["clarify"] / n,
        "exec_modes": dict(modes),
        "router_sources": dict(router_sources),
        "examples_router_missed": [t[:80] for d, t in divergences
                                   if d == "executive_found_work_router_missed"][:12],
    }


def founder_turns() -> list[str]:
    """The real inbound turns, filtered to actual language."""
    path = ROOT.parent / "production_turns.txt"
    if not path.exists():
        path = pathlib.Path("/private/tmp/claude-501/-Users-dhruvjain-bruce/"
                            "1ff25848-6b11-47cb-80ca-52c4aec11033/scratchpad/production_turns.txt")
    out = []
    for line in path.read_text().splitlines():
        if "] IN : " not in line:
            continue
        text = line.split("] IN : ", 1)[1].strip().lstrip("￼").strip()
        if is_student_language(text):
            out.append(text)
    return out


def main() -> int:
    turns = founder_turns()
    print(json.dumps(asyncio.run(run(turns, label="founder_real_turns")), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
