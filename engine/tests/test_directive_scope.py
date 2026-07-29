"""Clause-level negation scope — the fix for the turn that cancelled the founder's goal.

The transcript's turn 2 was "make it a professional email and send it dont show me draft".
`decision_resolver` saw "dont", called the turn a refusal, and the goal was CANCELLED. The negation
governs SHOWING THE DRAFT; the student was asking Bruce to hurry up, not to stop.

Every test here asserts BEHAVIOUR. The safety-critical direction is asserted in both directions on
purpose: a module that read every negation as harmless would be a far worse bug than the one it replaces,
so each "this does not reject" has a partner proving that a real refusal still rejects.
"""

from __future__ import annotations

import pytest

from bruce_engine.directive_scope import CONFLICTING_OPERATION, Polarity, ShowDraft, interpret


# --- the required readings ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, op, show", [
    ("dont send it", Polarity.reject, ShowDraft.unchanged),
    ("dont show me the draft", Polarity.none, ShowDraft.false),
    ("dont show me the draft just send it", Polarity.approve, ShowDraft.false),
    ("dont send it without showing me first", Polarity.reject, ShowDraft.true),
    ("make it a professional email and send it dont show me draft", Polarity.approve, ShowDraft.false),
    ("dont send it actually send it", Polarity.approve, ShowDraft.unchanged),
    ("send it actually dont", Polarity.reject, ShowDraft.unchanged),
])
def test_the_required_scope_readings(text, op, show):
    r = interpret(text)
    assert r.operation_polarity is op, f"{text!r} -> {r.operation_polarity}"
    assert r.presentation_show_draft is show, f"{text!r} -> {r.presentation_show_draft}"


def test_the_transcript_turn_that_cancelled_the_goal_now_approves_and_hides_the_draft():
    """THE REGRESSION TEST. This exact string cancelled a real goal in production."""
    r = interpret("make it a professional email and send it dont show me draft")
    assert r.approves_operation and not r.rejects_operation
    assert r.presentation_show_draft is ShowDraft.false
    assert r.tone_update == "professional"


# --- the safety direction, asserted both ways --------------------------------------------------------

@pytest.mark.parametrize("text", [
    "dont send it", "do not send it", "don't send it", "no dont send that email",
    "stop dont email her", "nah dont send it",
])
def test_a_real_refusal_of_the_operation_still_rejects(text):
    """The partner to every "this is only presentation" case. If these ever stop rejecting, mail goes out
    after someone said don't — strictly worse than the bug this module fixes."""
    assert interpret(text).rejects_operation, text


@pytest.mark.parametrize("text", [
    "dont show me the draft", "dont show me it first", "no need to show me the draft",
    "dont preview it for me",
])
def test_a_presentation_negation_never_rejects_the_operation(text):
    r = interpret(text)
    assert not r.rejects_operation, text
    assert r.presentation_show_draft is ShowDraft.false


def test_the_double_negative_asks_for_the_preview_rather_than_hiding_it():
    """"dont send it without showing me first" means SHOW me. Reading the negation as hiding the draft
    would do the exact opposite of what was said, immediately before an irreversible action."""
    r = interpret("dont send it without showing me first")
    assert r.presentation_show_draft is ShowDraft.true
    assert r.rejects_operation


# --- ambiguity is a result, not a guess --------------------------------------------------------------

@pytest.mark.parametrize("text", ["send it dont send it", "email it now dont email it"])
def test_two_bare_opposites_are_unclear_and_carry_a_reason(text):
    r = interpret(text)
    assert r.operation_polarity is Polarity.unclear
    assert r.ambiguity_reason == CONFLICTING_OPERATION
    assert not r.approves_operation and not r.rejects_operation, "unclear must authorize nothing"


def test_a_correction_marker_is_what_makes_a_later_directive_win():
    """Anti-vacuous pair: identical opposing directives resolve differently based ONLY on the marker, so
    the marker is doing the work rather than position alone."""
    assert interpret("send it dont send it").operation_polarity is Polarity.unclear
    assert interpret("send it actually dont send it").operation_polarity is Polarity.reject


# --- punctuation-free teen texting -------------------------------------------------------------------

@pytest.mark.parametrize("text, op", [
    ("js make it whatever u think is appropriate and send it alr", Polarity.approve),
    ("YES WRITE IT AND SEND IT NO MORE QUESTIONS", Polarity.approve),
    ("omg r u retarded a heartfelt thank u message", Polarity.none),
    ("send it", Polarity.approve),
    ("this", Polarity.none),
])
def test_unpunctuated_texting_reads_correctly(text, op):
    assert interpret(text).operation_polarity is op, text


def test_shouting_an_approval_is_still_an_approval_not_a_refusal():
    """"NO MORE QUESTIONS" contains "no". It is impatience, not a refusal of the send."""
    r = interpret("YES WRITE IT AND SEND IT NO MORE QUESTIONS")
    assert r.approves_operation


# --- tone + hygiene ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text, tone", [
    ("make it professional", "professional"), ("make it shorter", "shorter"),
    ("keep it casual", "casual"), ("make it more heartfelt", "warmer"),
])
def test_tone_edits_are_extracted_generically(text, tone):
    assert interpret(text).tone_update == tone


def test_empty_and_whitespace_authorize_nothing():
    for t in (None, "", "   ", "\n"):
        r = interpret(t)
        assert r.operation_polarity is Polarity.none and r.presentation_show_draft is ShowDraft.unchanged


def test_every_span_is_a_real_substring_of_the_trusted_text():
    """Provenance has to be checkable. A span that is not in the message is a fabricated citation."""
    text = "make it professional and send it dont show me draft"
    for span in interpret(text).spans:
        assert span in text, span


def test_the_result_is_immutable():
    r = interpret("send it")
    with pytest.raises(Exception):
        r.operation_polarity = Polarity.reject      # type: ignore[misc]
