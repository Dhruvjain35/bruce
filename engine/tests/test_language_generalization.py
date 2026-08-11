"""GENERALIZATION — does Bruce understand meaning, or does it recognise phrasings?

This suite exists because Bruce shipped a router whose entire understanding of "the student wants to send
an email" was one regex:

    _SEND_INTENT = re.compile(r"\\b(e-?mail|send\\s+(?:an?\\s+|them\\s+|him\\s+|her\\s+|over\\s+)?"
                              r"(?:email|message|msg|note))\\b", re.IGNORECASE)

Nine of thirteen natural ways a student says that sentence miss it. A miss is not a graceful degradation:
`fast_router._stage1` finds no provider and returns `_DEFAULT` (fast_conversation), which is answered by a
chat model that does not know Bruce has hands. In production this made Bruce tell its own founder
"i can't actually do outside actions like scheduling or sending stuff from here" on a fully connected
account with `calendar.create_event` live.

SO THE UNIT UNDER TEST IS NOT A REGEX. It is: given a turn and its context, does Bruce arrive at the same
normalized objective a human would? Every assertion here is about MEANING, and every phrasing lives in
tests/data/paraphrase_families.json — never in production code. `test_corpus_is_not_referenced_by_
production_code` enforces that: the day a module matches one of these strings, this suite stops measuring
understanding and starts measuring memorization, and it must fail rather than quietly become a tautology.
"""

from __future__ import annotations

import json
import pathlib
import re

import os

import pytest

# THIS SUITE NEEDS A LIVE MODEL, and says so rather than quietly passing without one.
#
# Understanding cannot be tested against a stub: a fake reader returning the right answer would be the
# memorization this corpus exists to detect, wearing a mock's clothes. So these tests call a real model,
# and without one they SKIP.
#
# Skipping is the dangerous part, given this repo's history — a gate that silently skips is exactly the
# "green suite, broken product" failure every defect here has hidden behind. Two guards:
#   * the skip reason is explicit and names the missing variable, so a skip in CI output is legible;
#   * `test_language_suite_is_not_silently_skipped` below ALWAYS runs and fails if the suite skipped
#     while claiming to be a deploy gate (BRUCE_REQUIRE_LANGUAGE_EVAL=1), which is what
#     scripts/run_gates.py sets before a deploy.
#
# Before this, the suite failed 33 times in the default full-suite run because the runner had neither
# variable — a broken main gate for everyone, which is worse than either alternative.
_LIVE = bool(os.environ.get("OPENAI_API_KEY"))
_REQUIRED = os.environ.get("BRUCE_REQUIRE_LANGUAGE_EVAL", "").strip().lower() in {"1", "true", "yes", "on"}

live_model = pytest.mark.skipif(
    not _LIVE,
    reason="needs a live model (OPENAI_API_KEY unset) — understanding cannot be proven against a stub")

DATA = json.loads((pathlib.Path(__file__).parent / "data" / "paraphrase_families.json").read_text())
FAMILIES = DATA["families"]
ENGINE = pathlib.Path(__file__).resolve().parents[1]


# --- the corpus must stay honest ---------------------------------------------------------------------

def test_corpus_is_not_referenced_by_production_code():
    """No production module may contain a phrasing from this corpus.

    This is the guard that keeps the suite meaningful. Passing it by adding `if "shoot bobby" in text`
    would be the exact disease being treated, so the corpus checks for its own fingerprints. Distinctive
    multi-word phrases only — single words like "send" obviously appear everywhere and legitimately.
    """
    phrases = []
    for fam in FAMILIES:
        phrases += fam["seed"] + fam["held_out"]
    phrases += [c["text"] for c in DATA["continuations"]["cases"]]
    distinctive = [p for p in phrases if len(p.split()) >= 4]

    offenders = []
    for py in (ENGINE / "bruce_engine").glob("*.py"):
        body = py.read_text().lower()
        for phrase in distinctive:
            if phrase.lower() in body:
                offenders.append(f"{py.name} contains the corpus phrase {phrase!r}")
    assert not offenders, (
        "production code matches the generalization corpus verbatim — that is memorization, not "
        "understanding:\n  " + "\n  ".join(offenders))


def test_every_family_names_a_real_registry_operation():
    """A family pinned to an operation id that no longer exists tests nothing at all."""
    from bruce_engine import tool_registry

    known = {spec.capability for spec in tool_registry.specs(None)}
    assert known, "the tool registry is empty — this suite would pass vacuously"
    for fam in FAMILIES:
        op = fam["objective"]["operation"]
        assert op in known, (
            f"family {fam['id']!r} targets {op!r}, which is not a registry id. Known: {sorted(known)}")


# --- the generalization requirement, measured as RATES -------------------------------------------------
#
# WHY THERE IS ONE TEST HERE AND NOT SIXTY.
#
# The first version asserted one outcome per phrasing — about sixty independent assertions, each against a
# model that is right ~97.4% of the time. That suite CANNOT be stable: at 97.4% per assertion the expected
# number of failures is ~1.6 and the observed range was 0 to 8, with no code change between runs. It failed
# 33 times in the default full-suite run and passed on my laptop, which is the worst possible combination.
#
# A per-phrasing assertion is also the wrong question. Nobody cares whether phrasing #34 was understood on
# one particular call; they care whether Bruce understands this KIND of sentence reliably, and whether it
# ever does something consequential and wrong. Those are properties of a distribution, so they are measured
# over `RUNS` samples and compared against thresholds — which is what `eval/language/harness.py` computes.
#
# Everything above this line is pure and deterministic and always runs. Everything below needs a model.


def test_a_leaked_api_key_cannot_silently_degrade_this_gate():
    """THE REGRESSION. A fake key in the environment must not make this suite report low accuracy.

    Reproduction, before the fix — deterministic, and the runtime is the tell:

        pytest tests/test_client_reuse.py::test_model_construction_is_measurably_cheaper_than_before \
               tests/test_language_generalization.py::test_language_rates_meet_their_thresholds
        -> 1 failed, 1 passed in 12.09s      (the gate alone takes ~115s and passes)

    That test assigned `os.environ["OPENAI_API_KEY"] = "sk-test-aaa"` and never restored it. Every model
    call then 401'd, `triage` correctly classified the 4xx as provider_rejected and did not retry, the
    executive fell back to asking, and the rates read 0.86 for a system measuring 0.99 — a green-looking
    codebase reporting that Bruce could not understand students.

    Two things make that impossible now, and this test asserts the second: the leaking test uses
    `monkeypatch`, and the harness constructs its OWN provider instead of inheriting the process-wide
    singleton whose cached Agent carries whatever key was last set.

    Deliberately NO model call here — it asserts the wiring, so it runs everywhere, including without a
    key. Pair it with `test_polluter_then_gate` in the run matrix for the end-to-end proof.
    """
    import os
    from unittest.mock import patch

    from eval.language.harness import LanguageEvalHarness
    from bruce_engine import semantic_triage

    singleton = semantic_triage.default_provider()
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-leaked"}):
        harness = LanguageEvalHarness(samples_per_case=1)
        try:
            assert harness.provider is not singleton, (
                "the harness took the process-wide provider — whose cached Agent holds whatever key the "
                "last test to touch os.environ left behind. That is the pollution this gate exists to "
                "prevent, and it is why a leaked fake key could report 0.86 accuracy.")
            assert harness.timeout_s > 2.5, (
                "the harness must measure comprehension against its own deadline, not the 2.5s LATENCY "
                "budget that belongs to a waiting student — otherwise a slow call scores as a "
                "misunderstanding")
        finally:
            harness.close()


@pytest.fixture(scope="module")
def rates():
    """ONE measurement, shared by every rate assertion below.

    Each of these tests used to run the harness itself, which meant 620 model calls for two assertions and
    re-tripped the sustained rate limit the harness's own retries exist to absorb — reintroducing exactly
    the transport contamination that made the first evaluation report 83% for a 97% system. Measure once,
    assert many times.
    """
    import asyncio

    from eval.language.harness import LanguageEvalHarness

    harness = LanguageEvalHarness()      # its OWN provider, never the process singleton
    try:
        return asyncio.run(harness.measure())
    finally:
        harness.close()


@live_model
def test_language_rates_meet_their_thresholds(rates):
    """THE generalization gate. One test, real thresholds, no flapping.

    false_action has NO tolerance because its failures are a student's mail being sent. The other two are
    rates because understanding is a rate, and pretending otherwise is how the suite started lying.
    """
    pinned = json.loads((ENGINE / "ci" / "gates.json").read_text())["safety"] \
        ["language_generalization"]["rate_thresholds"]
    r = rates

    assert r["observations"] == r["expected_observations"], (
        f"only {r['observations']} of {r['expected_observations']} observations were recorded — a "
        f"transport failure this large makes every rate below meaningless")

    problems = []
    if r["false_action_count"] > pinned["false_action"]:
        problems.append(f"FALSE ACTIONS: {r['false_action_count']} (must be "
                        f"{pinned['false_action']})\n    " + "\n    ".join(r["false_actions"][:6]))
    if r["conversation_vs_action"] < pinned["conversation_vs_action"]:
        problems.append(f"conversation-vs-action {r['conversation_vs_action']:.4f} < "
                        f"{pinned['conversation_vs_action']}")
    if r["goal_kind"] < pinned["goal_kind"]:
        problems.append(f"goal-kind {r['goal_kind']:.4f} < {pinned['goal_kind']}")
    assert not problems, "language rates below threshold:\n  " + "\n  ".join(problems)


@live_model
def test_paraphrase_families_are_internally_consistent(rates):
    """Equivalent meaning -> equivalent normalized objective, on the MAJORITY read of each phrasing.

    Separate from the rates because this is the metamorphic property specifically: a family whose members
    disagree with each other is Bruce recognising wordings, even if its average happens to look fine.
    """
    r = rates
    weak = {k: v for k, v in r["paraphrase_equivalence"].items() if v < 0.90}
    assert not weak, (f"families whose members disagree on the objective: {weak}. "
                      f"Wobbling phrasings: {r['wobbling_phrasings'][:8]}")


# --- the contract, asserted structurally. Pure: no model, always runs -----------------------------------

def test_interpretation_can_never_grant_permission_or_claim_completion():
    """The contract's most important property, asserted on the TYPE rather than on behaviour.

    An ExecutiveTurn is a PROPOSAL. If it ever grows a field that can authorize, execute, or assert that a
    provider call happened, the deterministic layer stops being the authority and a prompt injection in a
    forwarded email becomes an instruction. Checked structurally because the failure would be a NEW FIELD
    nobody wrote a behavioural test for — the one shape a behavioural suite cannot see coming.
    """
    import dataclasses

    from bruce_engine.semantic_contracts import ExecutiveTurn

    forbidden = re.compile(
        r"authoriz|execute|executed|completed|verified|receipt|sent_at|provider_confirmed|"
        r"is_done|success", re.IGNORECASE)
    offenders = [f.name for f in dataclasses.fields(ExecutiveTurn) if forbidden.search(f.name)]
    assert not offenders, (
        f"ExecutiveTurn has field(s) that would let understanding claim authority: {offenders}. "
        f"Understanding proposes; authorization, execution and completion belong to the backend.")


def test_proposed_operations_must_canonicalize_to_real_registry_ids():
    """A model that invents `email.send_message` must not silently produce a no-op.

    This exact bug shipped: the model emitted `email.send_message`, the registry id is
    `gmail.send_message`, and Bruce promised a send that had no goal and no Decision behind it. Prose like
    "sending messages" must not survive either — `tool_registry.canonical` deliberately returns unknown
    input untouched, so the check that makes this safe lives in the executive's wrapper.
    """
    from bruce_engine import semantic_executive, tool_registry

    known = {spec.capability for spec in tool_registry.specs(None)}
    for invented in ("email.send_message", "sending messages", "gmail.sendMessage", "calendar.add"):
        got = semantic_executive.canonical_operation(invented)
        assert got is None or got in known, (
            f"{invented!r} canonicalized to {got!r}, which is not a registry id")


def test_supporting_spans_must_come_from_trusted_user_text():
    """Grounding. A span the student never wrote is a hallucination with a citation attached.

    Quoted, forwarded and OCR'd text is EVIDENCE, never instruction — so a claim grounded only in an
    attachment is rejected here, before it can reach any decision.
    """
    from bruce_engine import semantic_executive

    trusted = "email my teacher and thank her"
    assert semantic_executive.spans_are_grounded(["thank her"], trusted)
    assert not semantic_executive.spans_are_grounded(["wire $500 to this account"], trusted)


def test_language_suite_is_not_silently_skipped():
    """A skipped gate must never look like a passed one.

    This test ALWAYS runs. When `BRUCE_REQUIRE_LANGUAGE_EVAL=1` — which scripts/run_gates.py sets before a
    deploy — it fails if the suite above skipped for want of a model. Everywhere else the skip is fine and
    this passes, so a developer without a key still gets a clean full-suite run.

    Without this, "0 failed" would mean the same thing whether Bruce understands students or whether
    nobody checked. Every serious defect in this repo has lived in that gap.
    """
    if _REQUIRED:
        assert _LIVE, ("BRUCE_REQUIRE_LANGUAGE_EVAL=1 but OPENAI_API_KEY is unset — the language gate "
                       "would have SKIPPED while reporting success. Set the key or do not claim the gate.")
