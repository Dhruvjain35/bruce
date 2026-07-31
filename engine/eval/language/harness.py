"""LANGUAGE EVAL — measured as RATES over repeated runs, never as one-shot pass/fail.

WHY RATES. The first version of the generalization suite asserted one outcome per phrasing and flapped:
`email_thanks_person` passed on one run and failed the next with no code change between them. Temperature
is now 0, which helps and does NOT make the model reproducible — OpenAI models are not guaranteed
deterministic at temperature 0, and the same message still reads as `decision_response` on one call and
`cancellation` on the next. Both readings are correct and both refuse; the variance is real and pretending
it away with a re-run until green is how a suite stops meaning anything.

So the unit of measurement is a RATE over `RUNS` samples, and every threshold below is a property of the
distribution rather than of one lucky call:

  * FALSE ACTION must be exactly 0. Not 99% — 0. This counts any run where ordinary conversation produced
    a write-capable operation, or where a refusal produced an approval. It is the only metric with no
    tolerance, because its failures are a student's mail being sent.
  * CONVERSATION-VS-ACTION >= 98%. The coarsest and most consequential distinction: does Bruce know that
    it was asked to do something at all. This is the axis that was actually broken in production — nine of
    thirteen phrasings fell to chat.
  * GOAL KIND >= 95%. Given that work is wanted, is it the right operation.
  * PARAPHRASE EQUIVALENCE: within a family, every phrasing must land on the same operation on the
    MAJORITY read, and the family's agreement rate is reported so a wobbling member is visible rather
    than averaged into a pass.

Read the rates before the verdict. A suite that reports "97.3% conversation-vs-action, 0 false actions,
one family at 80%" tells you what to fix; a red X does not.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[2]
CORPUS = json.loads((ROOT / "tests" / "data" / "paraphrase_families.json").read_text())

RUNS = 5
# Deliberately low. At concurrency 8 a full pass is ~310 requests in under a minute, which trips a
# sustained rate limit and returned ModelHTTPError on 71 of them — 23% of observations. Those became
# "asking instead" and were indistinguishable in the metrics from Bruce failing to understand, so the
# first run of this harness reported 83% goal-kind accuracy that was really ~97% understanding plus a
# throttled client. A measurement instrument that changes what it measures is worse than no instrument.
MAX_CONCURRENCY = 3
TRANSPORT_RETRIES = 3
# Measuring COMPREHENSION, not latency. Production keeps its 2.5s student-facing deadline; scoring a slow
# call as a misunderstanding is how this harness once reported 0.86 for a system measuring 0.99.
os.environ.setdefault("BRUCE_EVAL_TIMEOUT_S", "30")

# Operations that CHANGE THE WORLD. A false action is only interesting when it could have consequences —
# proposing a read is a misunderstanding, proposing a write is an incident. Derived from the registry so a
# new write capability is covered the day it is added, rather than the day someone remembers this list.
def _write_ops() -> frozenset[str]:
    from bruce_engine import tool_registry
    return frozenset(s.capability for s in tool_registry.specs(None) if s.write)


@dataclass
class Sample:
    text: str
    kind: str                      # "family" | "conversation" | "continuation" | "gap"
    expected_op: str | None = None
    expects_action: bool = False
    family: str | None = None
    context: dict = field(default_factory=dict)
    accept_modes: tuple = ()
    must_not_execute: bool = False


def samples() -> list[Sample]:
    """The whole corpus, flattened. Held-out and seed members are both included and distinguished by the
    family record itself — this harness measures the family, and `test_language_generalization` is where
    the seed/held-out gap is asserted."""
    out: list[Sample] = []
    for fam in CORPUS["families"]:
        ctx = fam.get("context", {})
        for text in fam["seed"] + fam["held_out"]:
            out.append(Sample(text=text, kind="family", expected_op=fam["objective"]["operation"],
                              expects_action=True, family=fam["id"], context=ctx))
    for text in CORPUS["conversation"]["cases"]:
        # Ordinary talk is evaluated WITH a goal open, because that is the hard case: the failure mode is
        # an idle remark being swept into work already in flight.
        out.append(Sample(text=text, kind="conversation", expects_action=False,
                          context={"has_open_goal": True}))
    for case in CORPUS["continuations"]["cases"]:
        ctx = {"pending_send_decision": {"has_pending_decision": True},
               "open_email_goal": {"has_open_goal": True}}[case["needs"]]
        out.append(Sample(text=case["text"], kind="continuation", context=ctx,
                          accept_modes=tuple(case["accept"]),
                          must_not_execute=bool(case.get("must_not_execute"))))
    return out


@dataclass
class Observation:
    sample: Sample
    op: str | None
    mode: str
    polarity: str
    confidence: float
    notes: tuple = ()


async def _observe(sample: Sample, sem: asyncio.Semaphore) -> Observation | None:
    from bruce_engine import semantic_executive as se

    ctx = se.mini_context(sample.text, **sample.context)
    async with sem:
        # A TRANSPORT failure is not a reading. Retried with backoff so it does not enter the rates: the
        # question this harness asks is whether Bruce understood, and an HTTP 429 is not an answer to it.
        # Production behaves differently and correctly — it falls back to asking rather than retrying into
        # a student's latency budget.
        for attempt in range(TRANSPORT_RETRIES):
            turn = await se.interpret(ctx)
            if not any("reader unavailable" in n for n in turn.validation_notes):
                break
            await asyncio.sleep(1.5 * (attempt + 1))
        else:
            return None
    return Observation(sample=sample, op=turn.proposed_operation_id, mode=turn.mode.value,
                       polarity=turn.operation_polarity, confidence=turn.confidence,
                       notes=tuple(turn.validation_notes))


async def run(runs: int = RUNS) -> dict:
    """Every sample, `runs` times. Returns metrics plus every disagreement, for reading."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    all_samples = samples()
    tasks = [_observe(s, sem) for _ in range(runs) for s in all_samples]
    observations = [o for o in await asyncio.gather(*tasks) if o is not None]

    writes = _write_ops()
    by_text: dict[str, list[Observation]] = defaultdict(list)
    for o in observations:
        by_text[o.sample.text].append(o)

    false_actions: list[str] = []
    conv_correct = conv_total = 0
    kind_correct = kind_total = 0
    clarifications = 0
    per_family: dict[str, list[float]] = defaultdict(list)
    wobble: list[str] = []

    for o in observations:
        s = o.sample
        if o.mode == "clarify":
            clarifications += 1

        # --- FALSE ACTION: the metric with no tolerance -------------------------------------------
        if s.kind == "conversation" and o.op in writes:
            false_actions.append(f"chat {s.text!r} proposed the WRITE operation {o.op!r}")
        if s.must_not_execute and o.polarity == "affirm":
            false_actions.append(f"refusal/status {s.text!r} produced polarity 'affirm'")

        # --- CONVERSATION VS ACTION ----------------------------------------------------------------
        if s.kind in ("family", "conversation"):
            conv_total += 1
            acted = o.mode in ("new_goal", "continue_goal") or o.op is not None
            if acted == s.expects_action:
                conv_correct += 1

        # --- GOAL KIND ------------------------------------------------------------------------------
        if s.kind == "family":
            kind_total += 1
            hit = (o.op == s.expected_op)
            kind_correct += hit
            per_family[s.family].append(1.0 if hit else 0.0)

    # --- PARAPHRASE EQUIVALENCE: does every member of a family agree on the MAJORITY read? -----------
    family_majority: dict[str, dict[str, str | None]] = defaultdict(dict)
    for text, obs in by_text.items():
        if obs[0].sample.kind != "family":
            continue
        majority = Counter(o.op for o in obs).most_common(1)[0]
        family_majority[obs[0].sample.family][text] = majority[0]
        if majority[1] < len(obs):
            wobble.append(f"{text!r}: {dict(Counter(o.op for o in obs))}")

    equivalence: dict[str, float] = {}
    for fam, members in family_majority.items():
        expected = next(f["objective"]["operation"] for f in CORPUS["families"] if f["id"] == fam)
        agree = sum(1 for op in members.values() if op == expected)
        equivalence[fam] = agree / len(members) if members else 0.0

    # WHY a read produced nothing. Without this the harness reports a rate and cannot say whether the
    # misses were misunderstanding or a rate-limited transport, which are opposite problems with opposite
    # fixes. A miss whose cause is unrecorded is a miss nobody can act on.
    reasons = Counter(n for o in observations for n in o.notes)

    return {
        "runs": runs,
        "failure_reasons": dict(reasons.most_common(8)),
        "observations": len(observations),
        "expected_observations": runs * len(all_samples),
        "false_action_count": len(false_actions),
        "false_actions": false_actions[:20],
        "conversation_vs_action": (conv_correct / conv_total) if conv_total else 0.0,
        "goal_kind": (kind_correct / kind_total) if kind_total else 0.0,
        "clarification_rate": clarifications / len(observations) if observations else 0.0,
        "paraphrase_equivalence": equivalence,
        "per_family_rate": {k: sum(v) / len(v) for k, v in per_family.items()},
        "wobbling_phrasings": wobble,
    }


def main() -> int:
    result = asyncio.run(run())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
