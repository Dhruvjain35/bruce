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
from enum import Enum

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
# Seconds, multiplied by the attempt number. A module constant rather than a literal so the integrity
# suite can zero it: what that suite asserts is the NUMBER of attempts, never the wall time between them.
TRANSPORT_BACKOFF_S = 1.5
# Measuring COMPREHENSION, not latency. Production keeps its 2.5s student-facing deadline; scoring a slow
# call as a misunderstanding is how this harness once reported 0.86 for a system measuring 0.99. Passed
# explicitly to `interpret` — an `os.environ.setdefault` at import time lived here briefly and was exactly
# the kind of process-global this harness now exists to avoid depending on.
EVAL_TIMEOUT_S = 30.0

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


class ReadOutcome(str, Enum):
    """WHAT AN OBSERVATION ACTUALLY IS — and therefore whether it may enter a rate.

    Only `valid` is a reading of the student's sentence. The other four are facts about the machinery,
    and each has a different owner: folding any of them into a comprehension percentage attributes a
    billing, latency or schema problem to the model's understanding. That is not a hypothetical — see
    the module header of tests/test_language_eval_integrity.py for the day it cost.
    """

    valid = "valid"                          # a real reading; score it
    provider_failure = "provider_failure"    # 429/5xx/network, or a 4xx auth/config rejection
    timeout = "timeout"                      # the deadline was spent before an answer arrived
    malformed = "malformed"                  # the model returned a shape that could not be parsed
    degraded = "degraded"                    # a partial or unusable read; a weak signal, not a reading


# Closed-vocabulary codes (semantic_executive) -> what the observation IS. Anything not listed here is a
# validation note about a read that DID happen (a dropped operation id, a polarity veto), which is a
# genuine reading and stays `valid`.
_OUTCOME_BY_CODE = {
    "reader_unavailable": ReadOutcome.provider_failure,
    "triage_failed_transport": ReadOutcome.provider_failure,
    "triage_failed_provider_rejected": ReadOutcome.provider_failure,
    "triage_failed_timeout": ReadOutcome.timeout,
    "triage_failed_invalid_schema": ReadOutcome.malformed,
    "triage_failed_low_confidence": ReadOutcome.degraded,
    "triage_failed": ReadOutcome.degraded,   # generic fallback: a failure whose reason we cannot name
    "no_usable_read": ReadOutcome.degraded,
}

# Retrying a spent deadline just spends it again, and a 4xx returns the same answer — the same reasoning
# semantic_triage.triage applies one layer down. Only a genuine transport blip earns another attempt.
_RETRYABLE = {ReadOutcome.provider_failure}

# TWO KINDS OF FAILURE, TWO DIFFERENT RULES — because they answer different questions.
#
# MACHINERY failures say nothing whatsoever about comprehension: a 429 is a billing fact and a spent
# deadline is a latency fact. There is no honest way to fold either into a percentage about understanding,
# so a SINGLE one invalidates the run. Zero tolerance is right here precisely because these are not
# properties of the model at all.
_MACHINERY = {ReadOutcome.provider_failure, ReadOutcome.timeout}

# MODEL-QUALITY failures are the opposite: an unparseable response IS the model's behaviour, and it is a
# real thing to measure. Invalidating a whole run over one of them is how a measurement stops being
# affordable enough to take — the first honest baseline (2026-08-11, 310 reads) was thrown away over
# exactly one malformed response, 0.32%, while `false_action` was 0 and `provider_failure` was 0.
#
# So they are BOUNDED rather than forbidden: reported as their own rate, excluded from the comprehension
# rates they would otherwise distort, and above the bound they invalidate the run. Without a bound this
# would be the hiding place the strict rule existed to close — a model returning garbage half the time
# would post an excellent score on the half that parsed.
_MODEL_QUALITY = {ReadOutcome.malformed, ReadOutcome.degraded}
MAX_MODEL_QUALITY_FRACTION = 0.02


def classify_read(codes) -> ReadOutcome:
    """The SAFEST class wins. A turn carrying both a transport code and a schema code failed at the
    transport first; reporting the later one would point the fix at the wrong owner."""
    seen = [_OUTCOME_BY_CODE[c] for c in codes if c in _OUTCOME_BY_CODE]
    for rank in (ReadOutcome.provider_failure, ReadOutcome.timeout, ReadOutcome.malformed,
                 ReadOutcome.degraded):
        if rank in seen:
            return rank
    return ReadOutcome.valid


@dataclass
class Observation:
    sample: Sample
    op: str | None
    mode: str
    polarity: str
    confidence: float
    notes: tuple = ()
    codes: tuple = ()
    outcome: ReadOutcome = ReadOutcome.valid


async def _observe(sample: Sample, sem: asyncio.Semaphore, *, provider=None,
                   timeout_s: float = EVAL_TIMEOUT_S) -> Observation:
    from bruce_engine import semantic_executive as se

    ctx = se.mini_context(sample.text, **sample.context)
    async with sem:
        # A TRANSPORT failure is not a reading. Retried with backoff so it does not enter the rates: the
        # question this harness asks is whether Bruce understood, and an HTTP 429 is not an answer to it.
        # Production behaves differently and correctly — it falls back to asking rather than retrying into
        # a student's latency budget.
        #
        # The predicate is the CODE, not the note. It used to be `"reader unavailable" in note`, but the
        # note the executive emits for a triage failure is `f"triage failed: {reason} ({ms}ms)"` — so the
        # substring never matched, the retry never ran, and a 100% outage was scored as 100% real reads.
        # That single mismatch is the whole of DEFECT-17.
        for attempt in range(TRANSPORT_RETRIES):
            turn = await se.interpret(ctx, triage=provider, timeout_s=timeout_s)
            outcome = classify_read(turn.validation_codes)
            if outcome not in _RETRYABLE:
                break
            if attempt < TRANSPORT_RETRIES - 1:
                await asyncio.sleep(TRANSPORT_BACKOFF_S * (attempt + 1))
    # A retry-exhausted sample is RECORDED, never dropped. Returning None shrank `observations`, which
    # hid the reason and left the caller to infer an outage from a missing count.
    return Observation(sample=sample, op=turn.proposed_operation_id, mode=turn.mode.value,
                       polarity=turn.operation_polarity, confidence=turn.confidence,
                       notes=tuple(turn.validation_notes), codes=tuple(turn.validation_codes),
                       outcome=outcome)


async def run(runs: int = RUNS, *, provider=None, timeout_s: float = EVAL_TIMEOUT_S) -> dict:
    """Every sample, `runs` times. Returns metrics plus every disagreement, for reading."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    all_samples = samples()
    tasks = [_observe(s, sem, provider=provider, timeout_s=timeout_s)
             for _ in range(runs) for s in all_samples]
    recorded = [o for o in await asyncio.gather(*tasks) if o is not None]
    expected_total = runs * len(all_samples)

    # EVERY class present, always. A missing key and a zero are different facts, and `.get(k, 0)` on a
    # metrics blob silently turns the first into the second.
    outcome_counts = {o.value: 0 for o in ReadOutcome}
    for o in recorded:
        outcome_counts[o.outcome.value] += 1

    # ONLY a real reading may enter a rate. Everything below this line scores `observations`, so a failed
    # read cannot depress a comprehension percentage by being counted as a wrong answer.
    observations = [o for o in recorded if o.outcome is ReadOutcome.valid]

    # THE REFUSAL. A run that did not measure what it claims to measure does not get to report a number.
    machinery = sum(outcome_counts[o.value] for o in _MACHINERY)
    model_quality = sum(outcome_counts[o.value] for o in _MODEL_QUALITY)
    quality_fraction = (model_quality / expected_total) if expected_total else 0.0
    missing = expected_total - len(recorded)

    reasons_invalid = []
    if machinery:
        detail = ", ".join(f"{o.value}={outcome_counts[o.value]}" for o in _MACHINERY
                           if outcome_counts[o.value])
        reasons_invalid.append(
            f"{machinery} of {expected_total} reads failed in the MACHINERY ({detail}) — a provider "
            f"outage or a spent deadline says nothing about comprehension, so no rate is reported")
    if quality_fraction > MAX_MODEL_QUALITY_FRACTION:
        detail = ", ".join(f"{o.value}={outcome_counts[o.value]}" for o in _MODEL_QUALITY
                           if outcome_counts[o.value])
        reasons_invalid.append(
            f"{model_quality} of {expected_total} reads ({quality_fraction:.2%}) were unusable model "
            f"output ({detail}), over the {MAX_MODEL_QUALITY_FRACTION:.0%} bound — too much of the "
            f"corpus went unread for the remainder to stand for it")
    if missing:
        reasons_invalid.append(f"{missing} reads were never recorded at all")

    valid = not reasons_invalid
    invalidated_by = "; ".join(reasons_invalid) if reasons_invalid else None

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
    #
    # Keyed on the CLOSED VOCABULARY CODE, not the prose note. The note interpolates elapsed ms, so one
    # outage fragmented into a key per latency value — 8 keys of 2-4 counts each instead of one key of
    # 62 — and a diagnostic that fragments cannot be read. Counted over every RECORDED observation,
    # because the failures are precisely what was just excluded from `observations`.
    reasons = Counter(c for o in recorded for c in o.codes)

    return {
        "runs": runs,
        # Did this run measure anything? Read this BEFORE any rate below it.
        "valid": valid,
        "invalidated_by": invalidated_by,
        "read_outcomes": outcome_counts,
        "failure_reasons": dict(reasons.most_common(8)),
        # Every sample accounted for, including the ones that failed — a dropped sample hides its reason.
        "observations": len(recorded),
        "expected_observations": expected_total,
        "valid_observations": len(observations),
        # Reported ALWAYS, including on a valid run. These are the reads the comprehension rates below
        # were computed WITHOUT, so a rate quoted without them beside it is missing its own denominator.
        "machinery_failures": machinery,
        "model_quality_failures": model_quality,
        "model_quality_fraction": quality_fraction,
        "false_action_count": len(false_actions),
        "false_actions": false_actions[:20],
        # None, not 0.0, on an invalidated run. A zero is a number, and a number gets quoted; this is the
        # difference between "Bruce understands 17% of turns" and "the account had no credits".
        "conversation_vs_action": ((conv_correct / conv_total) if conv_total else 0.0) if valid else None,
        "goal_kind": ((kind_correct / kind_total) if kind_total else 0.0) if valid else None,
        "clarification_rate": (clarifications / len(observations) if observations else 0.0) if valid else None,
        "paraphrase_equivalence": equivalence if valid else {},
        "per_family_rate": {k: sum(v) / len(v) for k, v in per_family.items()} if valid else {},
        "wobbling_phrasings": wobble,
    }


def main() -> int:
    result = asyncio.run(run())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


class LanguageEvalHarness:
    """The evaluation, with its dependencies handed to it rather than discovered from the process.

    WHY THIS EXISTS AS AN OBJECT. The module-level `run()` reached for `semantic_triage.default_provider()`
    — a process-wide singleton holding a cached Agent, and therefore a cached API key. A test elsewhere in
    the suite assigned a fake key to `os.environ` without restoring it, and this evaluation inherited it:
    every model call 401'd, the executive fell back to asking, and the gate read 0.86 for a system that
    measures 0.99. Nothing near the leaking test failed; the damage surfaced minutes later in the only
    suite that makes real calls.

    So the harness now OWNS its provider. Construct it, run it, dispose it:

        h = LanguageEvalHarness(provider=SemanticTriage(), timeout_s=30, samples_per_case=5)
        try:
            rates = await h.measure()
        finally:
            h.close()

    A caller that passes no provider gets a FRESH one, never the singleton — so two evaluations in one
    process cannot contaminate each other, and neither can anything that ran before them.
    """

    def __init__(self, *, provider=None, timeout_s: float = EVAL_TIMEOUT_S, seed: int | None = None,
                 samples_per_case: int = RUNS):
        from bruce_engine.semantic_triage import SemanticTriage

        # A FRESH provider by default. `default_provider()` would be the singleton, which is the whole
        # problem. `temperature=...` keeps the configured default (0) rather than sampling.
        self._owns_provider = provider is None
        self.provider = provider if provider is not None else SemanticTriage()
        self.timeout_s = timeout_s
        self.samples_per_case = samples_per_case
        # Recorded rather than applied: this harness has no stochastic branching of its own, and the model
        # is the only source of variance. Storing it keeps a run self-describing and makes it obvious that
        # reproducibility here comes from temperature 0 plus repetition, not from a seed.
        self.seed = seed

    async def measure(self) -> dict:
        return await run(self.samples_per_case, provider=self.provider, timeout_s=self.timeout_s)

    def close(self) -> None:
        """Drop the provider this harness created. Idempotent; safe on an injected provider (left alone)."""
        if self._owns_provider:
            self.provider = None
