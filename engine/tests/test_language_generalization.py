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

import pytest

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


# --- the actual generalization requirement -----------------------------------------------------------

def _interpret(text: str, **ctx):
    """Bruce's understanding of one turn, as a normalized objective.

    Deliberately routed through the real production seam rather than a test-only shim, so that a change
    which makes this pass has actually changed what a student experiences.
    """
    from bruce_engine import semantic_executive

    return semantic_executive.interpret_text(text, **ctx)


@pytest.mark.parametrize("family", FAMILIES, ids=[f["id"] for f in FAMILIES])
def test_every_paraphrase_in_a_family_reaches_the_same_objective(family):
    """THE CORE REQUIREMENT. Same meaning -> same operation, regardless of wording.

    Held-out members are the real test: nobody wrote a rule for them. A family that only passes on its
    seeds is a family Bruce memorized.
    """
    expected = family["objective"]["operation"]
    ctx = family.get("context", {})
    missed = []
    for phrasing in family["seed"] + family["held_out"]:
        got = _interpret(phrasing, **ctx).proposed_operation_id
        if got != expected:
            missed.append(f"  {phrasing!r}\n      -> {got!r}, expected {expected!r}")
    assert not missed, (
        f"family {family['id']!r} ({family['why']}) — {len(missed)} of "
        f"{len(family['seed']) + len(family['held_out'])} phrasings did not reach {expected!r}:\n"
        + "\n".join(missed))


@pytest.mark.parametrize("family", FAMILIES, ids=[f["id"] for f in FAMILIES])
def test_held_out_phrasings_are_understood_as_well_as_seeds(family):
    """Seeds and held-out members must be understood EQUALLY well.

    Separated from the test above so the failure message distinguishes "Bruce understands this objective"
    from "Bruce understands the two wordings someone happened to write down". A large gap between the two
    rates is the signature of pattern-matching.
    """
    expected = family["objective"]["operation"]
    ctx = family.get("context", {})
    seed_hits = sum(1 for p in family["seed"] if _interpret(p, **ctx).proposed_operation_id == expected)
    held_hits = sum(1 for p in family["held_out"] if _interpret(p, **ctx).proposed_operation_id == expected)
    seed_rate = seed_hits / len(family["seed"])
    held_rate = held_hits / len(family["held_out"])
    assert held_rate == seed_rate == 1.0, (
        f"family {family['id']!r}: seeds {seed_hits}/{len(family['seed'])} ({seed_rate:.0%}), "
        f"held-out {held_hits}/{len(family['held_out'])} ({held_rate:.0%}). "
        f"A gap here means Bruce recognises wordings, not meanings.")


# --- metamorphic properties --------------------------------------------------------------------------

_META = DATA["metamorphic"]
_MCTX = _META.get("_context", {})


@pytest.mark.parametrize("pair", _META["slang_invariance"], ids=lambda p: p[1][:34])
def test_slang_does_not_change_the_objective(pair):
    plain, slang = pair
    assert _interpret(slang).proposed_operation_id == _interpret(plain).proposed_operation_id, (
        f"slang changed the objective: {plain!r} vs {slang!r}")


@pytest.mark.parametrize("pair", _META["typo_invariance"], ids=lambda p: p[1][:34])
def test_typos_do_not_change_the_objective(pair):
    clean, typo = pair
    assert _interpret(typo).proposed_operation_id == _interpret(clean).proposed_operation_id, (
        f"a typo changed the objective: {clean!r} vs {typo!r}")


@pytest.mark.parametrize("pair", _META["punctuation_invariance"], ids=lambda p: p[1][:34])
def test_punctuation_does_not_change_the_objective(pair):
    a, b = pair
    c = _MCTX.get("punctuation_invariance", {})
    assert _interpret(b, **c).proposed_operation_id == _interpret(a, **c).proposed_operation_id


@pytest.mark.parametrize("pair", _META["word_order_invariance"], ids=lambda p: p[1][:34])
def test_word_order_does_not_lose_the_objective(pair):
    a, b = pair
    c = _MCTX.get("word_order_invariance", {})
    assert _interpret(b, **c).proposed_operation_id == _interpret(a, **c).proposed_operation_id, (
        f"reordering lost the objective: {a!r} vs {b!r}")


@pytest.mark.parametrize("pair", _META["politeness_invariance"], ids=lambda p: p[1][:34])
def test_politeness_does_not_change_authorization(pair):
    """Adding "please" must not change what Bruce is allowed to do — in either direction."""
    blunt, polite = pair
    c = _MCTX.get("politeness_invariance", {"has_pending_decision": True})
    assert _interpret(polite, **c).operation_polarity == _interpret(blunt, **c).operation_polarity


@pytest.mark.parametrize("text", _META["unrelated_negation_must_not_cancel"])
def test_unrelated_negation_does_not_cancel_the_operation(text):
    """"send it, no rush" is an approval containing the word "no".

    A negation is ABOUT something. Bruce deleted a real calendar event once because a negation attached to
    the wrong clause, so this direction is a correctness bug, not a politeness one.
    """
    turn = _interpret(text, has_pending_decision=True)
    assert turn.operation_polarity != "reject", (
        f"{text!r} was read as a refusal — the negation does not attach to the operation")


@pytest.mark.parametrize("text", _META["operation_refusal_must_block"])
def test_operation_refusal_always_blocks(text):
    """The safety direction. This must hold even when understanding is uncertain.

    Asymmetric on purpose: a missed approval costs one extra question, a missed REFUSAL sends mail the
    student said not to send. When in doubt this must resolve to reject.
    """
    turn = _interpret(text, has_pending_decision=True)
    assert turn.operation_polarity == "reject", (
        f"{text!r} was NOT read as refusing the operation — this direction must never fail open")


# --- ordinary conversation must stay ordinary --------------------------------------------------------

@pytest.mark.parametrize("text", DATA["conversation"]["cases"])
def test_ordinary_talk_creates_no_goal_even_with_goals_open(text):
    """A student can chat while work is in flight without corrupting it.

    The inverse failure of everything above: over-eager action. "im so tired lol" must not select the open
    email goal and must not propose an operation.
    """
    turn = _interpret(text, has_open_goal=True)
    assert turn.mode == "conversation", f"{text!r} was classified {turn.mode!r}, not conversation"
    assert turn.proposed_operation_id is None, (
        f"{text!r} proposed {turn.proposed_operation_id!r} — ordinary talk must not propose an operation")


# --- continuations depend on state, not vocabulary ---------------------------------------------------

@pytest.mark.parametrize("case", DATA["continuations"]["cases"], ids=lambda c: c["text"][:34])
def test_continuations_resolve_against_live_state(case):
    """"send it" means nothing without a draft on screen, and everything with one.

    `accept` is a set because several of these have two equally-correct readings (see the corpus note).
    `must_not_execute` is the property that does not get a set: whatever a refusal or a status enquiry is
    called, it may not carry an approval, because that is the difference between asking a question and
    sending someone's mail.
    """
    ctx = {"pending_send_decision": {"has_pending_decision": True},
           "open_email_goal": {"has_open_goal": True}}[case["needs"]]
    turn = _interpret(case["text"], **ctx)
    assert turn.mode.value in case["accept"], (
        f"{case['text']!r} with {case['needs']} -> mode {turn.mode.value!r}, "
        f"expected one of {case['accept']}")
    if case.get("must_not_execute"):
        assert turn.operation_polarity != "affirm", (
            f"{case['text']!r} carried an APPROVAL ({turn.operation_polarity!r}) — this turn must never "
            f"be able to authorize the pending send")


# --- the model may understand, but it may never decide -----------------------------------------------

def test_interpretation_can_never_authorize_execute_or_claim_completion():
    """The contract's most important property, asserted on the type itself.

    A SemanticTurn is a PROPOSAL. If it ever grows a field that can authorize, execute, or assert that a
    provider call happened, the deterministic layer stops being the authority and a prompt injection in a
    forwarded email becomes an instruction. This is checked structurally rather than by behaviour because
    the failure would be a new field nobody wrote a test for.
    """
    from bruce_engine.semantic_contracts import ExecutiveTurn

    forbidden = re.compile(
        r"authoriz|execute|executed|completed|verified|receipt|sent_at|provider_confirmed|"
        r"is_done|success", re.IGNORECASE)
    offenders = [name for name in {f.name for f in __import__("dataclasses").fields(ExecutiveTurn)} if forbidden.search(name)]
    assert not offenders, (
        f"ExecutiveTurn has field(s) that would let the model claim authority: {offenders}. "
        f"Understanding is a proposal; authorization, execution and completion are the backend's.")


def test_proposed_operations_must_canonicalize_to_real_registry_ids():
    """A model that invents `email.send_message` must not silently produce a no-op.

    This exact bug shipped: the model emitted `email.send_message`, the registry id is
    `gmail.send_message`, and Bruce promised a send that had no goal and no Decision behind it.
    """
    from bruce_engine import semantic_executive, tool_registry

    known = {spec.capability for spec in tool_registry.specs(None)}
    for invented in ("email.send_message", "sending messages", "gmail.sendMessage", "calendar.add"):
        got = semantic_executive.canonical_operation(invented)
        assert got is None or got in known, (
            f"{invented!r} canonicalized to {got!r}, which is not a registry id")


def test_supporting_spans_must_come_from_trusted_user_text():
    """Grounding. A span the student never wrote is a hallucination with a citation attached.

    Quoted, forwarded and OCR'd text is evidence, never instruction — so a span that appears only in an
    attachment must be rejected here too.
    """
    from bruce_engine import semantic_executive

    trusted = "email my teacher and thank her"
    assert semantic_executive.spans_are_grounded(["thank her"], trusted)
    assert not semantic_executive.spans_are_grounded(["wire $500 to this account"], trusted)


# --- what Bruce still does NOT understand -------------------------------------------------------------

@pytest.mark.parametrize("gap", DATA["known_gaps"]["cases"], ids=lambda g: g["text"][:34])
@pytest.mark.xfail(strict=True, reason="a KNOWN language gap — see tests/data/paraphrase_families.json")
def test_known_language_gaps(gap):
    """Phrasings Bruce does not understand yet, asserted as strict xfail.

    Strict on purpose, in both directions. Deleting a case Bruce fails is how a suite stays green while
    the product stays broken — so these stay. And `strict=True` means that if one starts PASSING, the
    suite fails and someone has to come and move it into a real family. A known gap that quietly fixes
    itself is indistinguishable from a known gap nobody ever looked at again.
    """
    assert _interpret(gap["text"], recent_turns="the student mentioned a dentist appointment thursday 3pm",
                      has_open_goal=True).proposed_operation_id == gap["expected"]
