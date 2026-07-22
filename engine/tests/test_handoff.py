"""D-INT-3 HandoffDecision authority (integration invariant 2).

The model may PROPOSE (needs_mission / proposed_goal / suggested capability) but may NEVER authorize
mission creation. These tests pin the deterministic policy and — critically — prove that a hallucinated
needs_mission flag with no explicit user handoff language can NEVER produce a mutating action or set
authorizes_mutation.
"""

from __future__ import annotations

from bruce_engine.handoff import (
    HandoffAction, HandoffInputs, MUTATING_ACTIONS, decide_handoff, has_explicit_handoff_language,
)


def test_explicit_language_detection():
    for yes in ("take this from here", "can u handle this for me", "keep following up pls",
                "did that actually go through?", "only bother me when you need my call"):
        assert has_explicit_handoff_language(yes)
    for no in ("what's 8x7", "thanks!", "can you explain this problem", "add this to my calendar", None):
        assert not has_explicit_handoff_language(no)


def test_hallucinated_needs_mission_without_explicit_language_never_mutates():
    # the model insists on a mission, user did not ask -> advisory only, no mutation, no mutating action
    inp = HandoffInputs(user_text="what is the derivative of x^2", model_needs_mission=True,
                        model_proposed_goal="do the whole worksheet", capability_supported=True,
                        confidence=0.99, has_matching_protocol=True)   # even with protocol+support+confidence
    d = decide_handoff(inp)
    assert d.action in (HandoffAction.answer_only, HandoffAction.remember_context)
    assert d.authorizes_mutation is False
    assert d.action not in MUTATING_ACTIONS


def test_no_signals_is_answer_only():
    d = decide_handoff(HandoffInputs(user_text="lol that's wild"))
    assert d.action is HandoffAction.answer_only and d.authorizes_mutation is False


def test_needs_mission_with_goal_but_no_explicit_language_is_remember_context():
    d = decide_handoff(HandoffInputs(user_text="here's the flyer", model_needs_mission=True,
                                     model_proposed_goal="register for the hackathon"))
    assert d.action is HandoffAction.remember_context and d.authorizes_mutation is False


def test_explicit_handoff_but_capability_unsupported_is_unsupported():
    d = decide_handoff(HandoffInputs(user_text="take this from here", capability_supported=False))
    assert d.action is HandoffAction.unsupported and d.authorizes_mutation is False


def test_explicit_supported_but_high_risk_requires_decision():
    d = decide_handoff(HandoffInputs(user_text="handle this for me", capability_supported=True,
                                     risk="high", confidence=0.9))
    assert d.action is HandoffAction.request_decision and d.requires_approval is True
    assert d.authorizes_mutation is False


def test_explicit_supported_but_irreversible_requires_decision():
    d = decide_handoff(HandoffInputs(user_text="handle this", capability_supported=True,
                                     reversible=False, confidence=0.9))
    assert d.action is HandoffAction.request_decision and d.authorizes_mutation is False


def test_explicit_supported_low_confidence_requires_decision():
    d = decide_handoff(HandoffInputs(user_text="take care of this", capability_supported=True,
                                     confidence=0.3))
    assert d.action is HandoffAction.request_decision and d.authorizes_mutation is False


def test_explicit_supported_acceptable_no_protocol_proposes():
    d = decide_handoff(HandoffInputs(user_text="take this from here", capability_supported=True,
                                     risk="low", reversible=True, confidence=0.8))
    assert d.action is HandoffAction.propose_mission and d.authorizes_mutation is False


def test_only_a_matching_protocol_can_authorize_mutation():
    d = decide_handoff(HandoffInputs(user_text="take this from here", capability_supported=True,
                                     risk="low", reversible=True, confidence=0.8, has_matching_protocol=True))
    assert d.action is HandoffAction.create_mission_under_protocol
    assert d.authorizes_mutation is True                   # the ONLY path that authorizes mutation


def test_authorizes_mutation_requires_all_of_explicit_supported_lowrisk_reversible_confident_protocol():
    # remove any single precondition -> never authorizes mutation
    base = dict(user_text="take this from here", capability_supported=True, risk="low",
                reversible=True, confidence=0.8, has_matching_protocol=True)
    assert decide_handoff(HandoffInputs(**base)).authorizes_mutation is True
    for override in (dict(user_text="just curious"), dict(capability_supported=False),
                     dict(risk="high"), dict(reversible=False), dict(confidence=0.2),
                     dict(has_matching_protocol=False)):
        assert decide_handoff(HandoffInputs(**{**base, **override})).authorizes_mutation is False
