"""THE INSTRUMENT'S OWN GATE — can the language evaluation tell "Bruce misunderstood" from "the
provider was down"?

WHY THIS SUITE EXISTS. On 2026-08-10 the language gate reported:

    conversation-vs-action 0.1739 < 0.98
    goal-kind              0.0000 < 0.95

and that was read as a comprehension regression in the semantic executive. It was not. Every one of the
310 model calls behind those numbers had been rejected by OpenAI with

    429 insufficient_quota / credit_balance_exhausted

The account had no credits. The harness could not tell the difference, so a BILLING event was reported as
"Bruce understands 17% of turns" — a number that is not merely wrong but actively misleading, because it
points an investigation at the prompt and the model while the actual fix is a credit card.

The mechanism, precisely: `harness._observe` retried only on the substring "reader unavailable", but the
note the executive emits for a triage failure is `f"triage failed: {reason} ({ms}ms)"`. The predicate
missed, the retry never ran, and the fallback ExecutiveTurn — mode `clarify`, `proposed_operation_id`
None — was scored as a genuine reading of the student's sentence. `observations` therefore equalled
`expected_observations` at a 100% transport failure rate, so the guard in
`test_language_generalization.py` that exists to catch exactly this could never fire.

SO THE PROPERTY UNDER TEST IS NOT A RATE. It is: an evaluation run must know whether it measured
anything. Five outcomes have to stay distinguishable, because they have five different owners:

    valid             a real reading of the sentence          -> score it
    provider_failure  429/5xx/network, or 4xx auth/config     -> billing or configuration; NOT comprehension
    timeout           the deadline was spent                  -> latency; NOT comprehension
    malformed         the model returned an unusable shape    -> prompt/schema; NOT comprehension
    degraded          a partial read below the floor          -> weak signal; NOT a full reading

Only `valid` may enter a rate. Anything else must INVALIDATE the run rather than depress a percentage —
a gate that reports 17% when it measured nothing is worse than a gate that refuses to report.

These tests run OFFLINE against injected providers. That is deliberate and is not the "fake reader"
this repo bans in `test_language_generalization`: that suite measures UNDERSTANDING, which a stub cannot
prove. This suite measures the INSTRUMENT, and the only way to prove an instrument detects an outage is
to cause one. No API budget is spent here.
"""

from __future__ import annotations

import asyncio

import pytest

from bruce_engine.semantic_contracts import (Actionability, DecisionPolarity, Family, GoalCount,
                                             OperationFamily, SemanticTurn, TurnRole)
from eval.language import harness as H


# --- providers that fail in each of the ways production actually fails --------------------------------

def _healthy_turn(confidence: float = 0.95) -> SemanticTurn:
    """A well-formed read. Deliberately a SEND: it is the one family whose objective the corpus asserts."""
    return SemanticTurn(
        turn_role=TurnRole.new_goal,
        actionability=Actionability.executable,
        decision_polarity=DecisionPolarity.none,
        goal_count=GoalCount.one,
        domain_candidates=(Family.communication,),
        operation_family=OperationFamily.send,
        confidence=confidence,
    )


class _Exhausted(Exception):
    """The real DEFECT-2 shape: HTTP 429 with an exhausted balance. `classify_failure` maps a 429 to
    `transport`, which IS retried — so this also proves the retry path terminates instead of looping."""
    status_code = 429


class _AuthRejected(Exception):
    """A 4xx that is NOT retried: `classify_failure` calls it `provider_rejected`. A revoked or wrong key
    lands here, and it must not be scored as a misunderstanding either."""
    status_code = 401


class QuotaExhaustedProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def read(self, body: str) -> SemanticTurn:
        self.calls += 1
        raise _Exhausted("You have no credits remaining.")


class AuthRejectedProvider:
    async def read(self, body: str) -> SemanticTurn:
        raise _AuthRejected("invalid api key")


class HangingProvider:
    """Never answers inside the deadline."""

    async def read(self, body: str) -> SemanticTurn:
        await asyncio.sleep(30)
        raise AssertionError("unreachable — the deadline must fire first")


class MalformedProvider:
    """Returns an unusable shape. `classify_failure`'s default branch calls this `invalid_schema`."""

    async def read(self, body: str) -> SemanticTurn:
        raise ValueError("expected SemanticTurn, model returned prose")


class HealthyProvider:
    async def read(self, body: str) -> SemanticTurn:
        return _healthy_turn()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """The retry backoff is real time and this suite exercises the exhausted-retry path on every sample.
    Zero it so the integrity gate stays a fast offline test — the NUMBER of attempts is what is under
    test, never the wall time between them."""
    monkeypatch.setattr(H, "TRANSPORT_BACKOFF_S", 0.0)


def _measure(provider, *, timeout_s: float = 5.0) -> dict:
    return asyncio.run(H.run(1, provider=provider, timeout_s=timeout_s))


# --- the gate ------------------------------------------------------------------------------------------

def test_total_provider_outage_invalidates_the_run_instead_of_reporting_a_rate():
    """THE 2026-08-10 REGRESSION, as a test.

    Every call 429s. The run must come back INVALID and must NOT carry a comprehension percentage. This
    is the assertion that would have saved a day of investigating a prompt that was never broken.
    """
    provider = QuotaExhaustedProvider()
    r = _measure(provider)

    assert r["valid"] is False, (
        "a 100% provider outage produced a VALID evaluation run — this is exactly the defect: "
        f"conversation_vs_action={r.get('conversation_vs_action')!r}, goal_kind={r.get('goal_kind')!r}")
    assert r["conversation_vs_action"] is None and r["goal_kind"] is None, (
        "an invalidated run must not expose a comprehension rate at all. A number that exists will be "
        f"read and quoted: got {r['conversation_vs_action']!r} / {r['goal_kind']!r}")
    assert r["read_outcomes"]["provider_failure"] == r["expected_observations"]
    assert r["read_outcomes"]["valid"] == 0
    assert "provider_failure" in r["invalidated_by"]


def test_the_outage_is_retried_a_bounded_number_of_times_and_then_recorded():
    """A transport failure earns retries; it does not earn silence, and it does not loop.

    The old code dropped a retry-exhausted sample by returning None, which made `observations` shrink —
    the one signal the generalization guard reads. Recording it as an explicit provider_failure is
    strictly better: the guard fires AND the reason survives.
    """
    provider = QuotaExhaustedProvider()
    r = _measure(provider)

    # THE BOUND COMPOUNDS ACROSS TWO LAYERS, and this test pins the product rather than either factor.
    # `semantic_triage.triage` reserves its own single retry for transport (semantic_triage.py:343,353),
    # and the harness retries on top of that — so one sample against a 429 costs TRANSPORT_RETRIES x 2
    # provider calls, not TRANSPORT_RETRIES. Worth stating out loud: against a dead account a full
    # evaluation issues ~6x its sample count in rejected requests. They are 429s and therefore unbilled,
    # but the wall time is real, and anyone who later lowers one factor should see the other here.
    expected_calls_per_sample = H.TRANSPORT_RETRIES * 2
    per_sample = provider.calls / r["expected_observations"]
    assert per_sample == pytest.approx(expected_calls_per_sample, abs=0.01), (
        f"expected {expected_calls_per_sample} provider calls per sample (harness "
        f"{H.TRANSPORT_RETRIES} x triage 2), saw {per_sample:.2f} — an unbounded retry against an "
        f"account that is already refusing to serve is the wrong direction")
    assert r["observations"] == r["expected_observations"], (
        "every sample must still be ACCOUNTED for after its retries are exhausted, not dropped")


def test_auth_rejection_is_a_provider_failure_not_a_misunderstanding():
    """A 4xx is configuration, not comprehension. `classify_failure` refuses to retry it, and the harness
    must still refuse to score it."""
    r = _measure(AuthRejectedProvider())

    assert r["valid"] is False
    assert r["read_outcomes"]["provider_failure"] == r["expected_observations"]
    assert r["read_outcomes"]["malformed"] == 0, (
        "a 401 was classified as a malformed model response — that points the fix at the prompt "
        "instead of at the key")


def test_timeout_is_distinguished_from_misunderstanding():
    """A deadline that fired is a LATENCY fact. Scoring it as a wrong reading is how this harness once
    reported 0.86 for a system measuring 0.99 — the exact reasoning behind EVAL_TIMEOUT_S existing."""
    r = _measure(HangingProvider(), timeout_s=0.05)

    assert r["valid"] is False
    assert r["read_outcomes"]["timeout"] == r["expected_observations"]
    assert r["read_outcomes"]["provider_failure"] == 0, (
        "a timeout was folded into provider_failure — they have different owners: one is latency, the "
        "other is billing or configuration")


def test_malformed_model_output_is_its_own_class():
    """An unusable shape is a prompt/schema defect. It is not the student being misunderstood, and it is
    not the provider being down."""
    r = _measure(MalformedProvider())

    assert r["valid"] is False
    assert r["read_outcomes"]["malformed"] == r["expected_observations"]
    assert r["read_outcomes"]["timeout"] == 0


def test_a_healthy_provider_still_produces_real_rates():
    """The guard must not be a blanket refusal. With every read valid, the run is valid and the rates are
    numbers again — otherwise this fix would simply have broken the gate in the other direction."""
    r = _measure(HealthyProvider())

    assert r["valid"] is True, f"a fully healthy run was invalidated: {r.get('invalidated_by')!r}"
    assert r["invalidated_by"] is None
    assert r["read_outcomes"]["valid"] == r["expected_observations"]
    assert isinstance(r["conversation_vs_action"], float)
    assert isinstance(r["goal_kind"], float)


def test_one_machinery_failure_invalidates_even_though_it_is_a_single_read():
    """ZERO TOLERANCE, but only for the machinery. A spent deadline is a LATENCY fact and a 429 is a
    BILLING fact; neither is evidence about comprehension, so there is no honest fraction of them to
    accept. One is enough to refuse."""

    class OneHang:
        def __init__(self) -> None:
            self.n = 0

        async def read(self, body: str) -> SemanticTurn:
            self.n += 1
            if self.n == 3:
                await asyncio.sleep(30)
            return _healthy_turn()

    r = _measure(OneHang(), timeout_s=0.2)

    assert r["valid"] is False, "a machinery failure was averaged into an otherwise green run"
    assert r["read_outcomes"]["timeout"] == 1
    assert r["machinery_failures"] == 1
    assert r["conversation_vs_action"] is None
    assert "MACHINERY" in r["invalidated_by"]


def test_a_rare_malformed_read_is_reported_but_does_not_throw_away_the_run():
    """THE OTHER RULE, learned from the first honest baseline.

    On 2026-08-11 a real 310-read run came back 309 valid, 0 provider failures, 0 false actions — and was
    discarded over ONE unparseable response, 0.32%. That is not a measurement failure, it is a measurable
    property of the model, and a gate nobody can afford to run is a gate that stops being run.

    So model-quality failures are BOUNDED rather than forbidden: excluded from the comprehension rates
    they would distort, reported as their own number, and fatal only above the bound.
    """

    class OneBadShape:
        def __init__(self) -> None:
            self.n = 0

        async def read(self, body: str) -> SemanticTurn:
            self.n += 1
            if self.n == 3:
                raise ValueError("model returned prose")
            return _healthy_turn()

    r = _measure(OneBadShape())

    assert r["model_quality_fraction"] < H.MAX_MODEL_QUALITY_FRACTION
    assert r["valid"] is True, f"one malformed read discarded the whole run: {r['invalidated_by']!r}"
    assert r["read_outcomes"]["malformed"] == 1
    assert r["model_quality_failures"] == 1
    # The rate exists, and it was computed WITHOUT the bad read rather than scoring it as a wrong answer.
    assert isinstance(r["conversation_vs_action"], float)
    assert r["valid_observations"] == r["expected_observations"] - 1


def test_malformed_output_above_the_bound_still_invalidates():
    """The bound is what stops the tolerance from becoming a hiding place: a model returning garbage half
    the time must not post an excellent score on the half that happened to parse."""

    class HalfGarbage:
        def __init__(self) -> None:
            self.n = 0

        async def read(self, body: str) -> SemanticTurn:
            self.n += 1
            if self.n % 2 == 0:
                raise ValueError("model returned prose")
            return _healthy_turn()

    r = _measure(HalfGarbage())

    assert r["model_quality_fraction"] > H.MAX_MODEL_QUALITY_FRACTION
    assert r["valid"] is False
    assert r["conversation_vs_action"] is None
    assert "bound" in r["invalidated_by"]


def test_a_transient_blip_that_recovers_on_retry_does_not_invalidate():
    """THE OTHER HALF. Strictness must not mean brittleness: the retry exists precisely so that a lost
    packet does not cost a whole evaluation. A blip that recovers produced a real reading, and a real
    reading counts.

    Without this test the previous one could be satisfied by refusing every run that ever saw a 429,
    which would make the instrument unusable on a normal day.
    """

    class OneBlipThenFine:
        def __init__(self) -> None:
            self.n = 0

        async def read(self, body: str) -> SemanticTurn:
            self.n += 1
            if self.n == 3:
                raise _Exhausted("one lost packet")
            return _healthy_turn()

    r = _measure(OneBlipThenFine())

    assert r["valid"] is True, (
        f"a single recoverable blip invalidated an otherwise healthy run: {r['invalidated_by']!r}")
    assert r["read_outcomes"]["valid"] == r["expected_observations"]
    assert isinstance(r["conversation_vs_action"], float)


def test_failure_reasons_do_not_fragment_on_interpolated_latency():
    """THE BACKUP DIAGNOSTIC, which was also broken.

    `failure_reasons` keyed on the prose note, and the note interpolates elapsed ms — so a real outage
    produced eight distinct keys with two to four counts each instead of one key with sixty-two. The
    counter must key on the CLOSED VOCABULARY code, which carries no latency and no student text.
    """
    r = _measure(QuotaExhaustedProvider())

    assert r["failure_reasons"], "an all-failing run recorded no failure reasons at all"
    assert len(r["failure_reasons"]) == 1, (
        f"one failure mode fragmented into {len(r['failure_reasons'])} keys: "
        f"{list(r['failure_reasons'])} — a diagnostic that fragments cannot be read")
    key = next(iter(r["failure_reasons"]))
    assert not any(ch.isdigit() for ch in key), (
        f"failure reason key {key!r} carries interpolated digits; it will never aggregate across runs")


def test_every_outcome_class_is_reported_even_when_zero():
    """A missing key and a zero are different facts, and `dict.get(...)` on a metrics blob silently turns
    the first into the second. Every class is always present."""
    r = _measure(HealthyProvider())

    assert set(r["read_outcomes"]) == {o.value for o in H.ReadOutcome}
