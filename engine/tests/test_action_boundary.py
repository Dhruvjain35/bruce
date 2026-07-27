"""The execution boundary: a refusal must be unable to cross it EVEN WHEN THE MODEL IS WRONG.

These tests deliberately feed the derivation a model reading that says "yes, executable, go" while the
student's own trusted text says don't. Prompt work took model false actions from 0.281 to 0.061 and will
not reach zero, because the model is a probabilistic reader. So the property under test is not that the
model is right — it is that being wrong cannot produce a write.
"""

from __future__ import annotations

import itertools

import pytest

from bruce_engine import execution_derivation as ed
from bruce_engine import user_action_boundary as uab
from bruce_engine.semantic_contracts import (Actionability, DecisionPolarity, Family, GoalCount,
                                             OperationFamily, SemanticTurn, TurnContext, TurnRole)

LIVE_CAPS = frozenset({"calendar.create_event", "calendar.update_event", "calendar.delete_event",
                       "gmail.send_message", "gmail.find_reply"})

# The most dangerous possible model output: confident, executable, fully resolved, and approving.
WORST_CASE = SemanticTurn(
    turn_role=TurnRole.decision_response, actionability=Actionability.executable,
    decision_polarity=DecisionPolarity.approve, goal_count=GoalCount.one,
    domain_candidates=(Family.calendar,), operation_family=OperationFamily.create, confidence=0.99)

REFUSALS = [
    "no", "nope", "nah", "dont", "don't", "don’t do that", "never mind", "nvm",
    "actually nah don't add it", "no dont put practice on friday", "please dont schedule anything",
    "wait no", "hold off", "forget it", "stop", "cancel that", "not yet", "no thanks",
    "add practice friday actually no dont", "email coach wait no ill do it",
    "i changed my mind, dont send that email", "do NOT email my teacher", "skip the email for now",
    "ugh no", "nah im good", "dont bother", "scratch that", "drop it",
]


def ctx_for(text, *, pending=True, active=True):
    return TurnContext(
        live_families=frozenset({Family.calendar, Family.communication}), live_capabilities=LIVE_CAPS,
        pending_decision_id="dec-1" if pending else None,
        active_run_id="run-1" if active else None, active_run_domain="calendar",
        action_boundary=uab.evaluate(text, has_pending_decision=pending, has_active_run=active))


@pytest.mark.parametrize("text", REFUSALS)
def test_a_refusal_cannot_produce_a_write_even_when_the_model_approves(text):
    d = ed.derive(WORST_CASE, ctx_for(text))
    assert not ed.derives_a_write(d), (text, d.rule)
    assert d.execution_class == "fast_conversation"
    assert d.rule.startswith("deterministic_veto_") or d.rule == "veto_provider_entity"


@pytest.mark.parametrize("text", REFUSALS)
def test_a_refusal_creates_no_authorization_and_no_run(text):
    """Zero-call in the only sense this layer can assert it: nothing downstream is handed anything it
    could execute — no capability, no new approval, no execution class that spawns a run."""
    d = ed.derive(WORST_CASE, ctx_for(text))
    assert d.capabilities == ()
    assert d.execution_class not in ("direct_action", "background_mission")


def test_the_veto_is_monotonic_across_the_whole_semantic_vocabulary():
    """Whatever the model says — any role, any actionability, any family, any operation, any polarity —
    a refused turn derives no write."""
    boundary_ctx = ctx_for("actually nah don't add it")
    n = 0
    for role, act, fam, op, pol in itertools.product(
            TurnRole, Actionability, Family, OperationFamily, DecisionPolarity):
        d = ed.derive(SemanticTurn(turn_role=role, actionability=act, domain_candidates=(fam,),
                                   operation_family=op, decision_polarity=pol,
                                   goal_count=GoalCount.one, confidence=0.99), boundary_ctx)
        assert not ed.derives_a_write(d), (role, act, fam, op, pol, d.rule)
        n += 1
    assert n == 7 * 5 * 7 * 9 * 4


def test_quoted_approval_never_authorizes():
    """Someone else's yes, pasted by the student, is information about a conversation. The trusted-text
    separation is reused from the P0 boundary rather than reimplemented."""
    b = uab.evaluate('coach replied "yes add it" — what does he mean',
                     has_pending_decision=True)
    assert not b.authorizes()


def test_cancelling_bruces_plan_is_not_deleting_a_calendar_event():
    """The evaluation caught these collapsed together: a refusal derived calendar.delete_event."""
    plan = uab.evaluate("cancel that", has_active_run=True)
    assert plan.polarity is uab.Polarity.cancellation and plan.target is uab.Target.active_run

    entity = uab.evaluate("cancel that and delete the dentist appointment", has_active_run=True)
    assert entity.target is uab.Target.provider_entity
    d = ed.derive(WORST_CASE, ctx_for("cancel that and delete the dentist appointment"))
    assert not ed.derives_a_write(d)          # needs its own resolution and its own authorization
    assert d.needs_clarification


def test_an_affirmative_still_gets_through():
    """A veto that blocked everything would be safe and useless."""
    d = ed.derive(WORST_CASE, ctx_for("yes do it"))
    assert d.execution_class == "direct_action" and d.decision_id == "dec-1"


def test_silence_about_a_pending_question_is_not_consent():
    b = uab.evaluate("whats the weather", has_pending_decision=True)
    assert b.polarity is uab.Polarity.unrelated and not b.authorizes()


# --- dual channel --------------------------------------------------------------------------------------

def test_the_safer_reading_wins_when_the_two_channels_disagree():
    refused = uab.evaluate("actually nah don't add it", has_pending_decision=True)
    assert uab.reconcile(refused, "approve") == uab.CONTRADICTION_BLOCK

    unsure = uab.evaluate("hmm maybe", has_pending_decision=True)
    assert uab.reconcile(unsure, "approve") == uab.CONTRADICTION_CLARIFY

    yes = uab.evaluate("yes do it", has_pending_decision=True)
    assert uab.reconcile(yes, "decline") == uab.CONTRADICTION_CLARIFY
    assert uab.reconcile(yes, "approve") == uab.AGREE


def test_curly_apostrophes_do_not_bypass_the_boundary():
    """Three separate matchers in this codebase have failed on a curly apostrophe in production."""
    for text in ("don’t add it", "don't add it", "dont add it"):
        assert uab.evaluate(text, has_pending_decision=True).blocks_execution(), text
