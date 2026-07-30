"""THE CAPABILITY NAMESPACE, and the promise that must not outrun it.

MEASURED LIVE, 2026-07-30, on the founder's own account. One message: "email dhruvhydrox@gmail.com with
subject bruce staging test b515b40 and body ...". The model read it correctly — `intent=actionable`,
every entity right — and named the capability `email.send_message`. The registry id is
`gmail.send_message`.

Right SHAPE, so `transitions.is_operation_id` accepted it. Wrong NAMESPACE, so `tool_registry.get`
returned None, `goal_runtime.creation_verdict` answered `capability_not_in_registry`, and NO goal and NO
Decision were created. Zero provider calls happened — the safety envelope held exactly as designed. Then
the turn fell through to the model's own words and Bruce said "yep, i can do that. i'll send an email
to..." and sent nothing.

TWO DEFECTS, and they need two fixes. Translating the name is not enough on its own: the reason the
student was lied to is that the reply layer will promise anything the model writes, whether or not
anything exists to make it true. So this file tests both, and the second one is tested with the alias
DELIBERATELY REMOVED — otherwise "no false promise" would pass for the wrong reason, because the alias
fixed the turn before the gate was ever consulted.
"""

from __future__ import annotations

import pytest

from bruce_engine import goal_runtime, goal_slots, response_composer, tool_registry
from bruce_engine.conversation_contract import (ConversationDecision, ExtractedEntity, IntentKind,
                                                ResponseType, RiskLevel)

SEND = "gmail.send_message"
ALIAS = "email.send_message"
CREATE = "calendar.create_event"


def _decision(*, caps=(), intent=IntentKind.actionable, text="ok", entities=()):
    return ConversationDecision(
        intent=intent, response_type=ResponseType.direct_answer, user_visible_response=text,
        extracted_entities=list(entities), required_capabilities=list(caps), needs_mission=False,
        proposed_goal=None, risk_level=RiskLevel.none, confidence=0.95)


# ==========================================================================================================
# 1. THE ALIAS
# ==========================================================================================================

def test_the_exact_capability_the_model_emitted_now_resolves():
    """THE REGRESSION. This one string cost a live founder turn."""
    assert tool_registry.canonical(ALIAS) == SEND
    assert tool_registry.get(tool_registry.canonical(ALIAS)) is not None
    assert goal_slots.kind_for_capability(tool_registry.canonical(ALIAS)) is goal_slots.GoalKind.send_email


def test_the_verdict_that_refused_the_live_turn_now_creates_a_goal():
    """`creation_verdict` is where the turn actually died. Asserted through the same call the handler
    makes, with the same intent the model emitted."""
    before = goal_runtime.creation_verdict(decision=_decision(), capability=ALIAS, continuation=False)
    after = goal_runtime.creation_verdict(
        decision=_decision(), capability=tool_registry.canonical(ALIAS), continuation=False)
    assert before.reason == goal_runtime.UNKNOWN_CAPABILITY and not before.create, \
        "the raw alias should still be unknown — canonicalization is the caller's job, not the verdict's"
    assert after.create and after.reason == goal_runtime.ACTIONABLE


def test_the_handler_canonicalizes_what_the_model_named():
    """The fix has to be where the model's word ENTERS, or every caller needs its own copy of the map."""
    from bruce_engine import goal_handler
    assert goal_handler.capability_for_turn(_decision(caps=[ALIAS]), None) == SEND
    assert goal_handler.capability_for_turn(_decision(caps=[SEND]), None) == SEND


def test_goal_selection_canonicalizes_the_same_way():
    """Selection and creation must agree about what the model named, or a turn is selected onto a goal it
    is then refused by."""
    from bruce_engine import goal_selection
    assert goal_selection._named_capability(_decision(caps=[ALIAS])) == SEND


@pytest.mark.parametrize("alias, real", [
    ("email.send_message", "gmail.send_message"),
    ("email.reply_to_thread", "gmail.reply_to_thread"),
    ("google_calendar.create_event", "calendar.create_event"),
    ("google_calendar.update_event", "calendar.update_event"),
    ("google_calendar.delete_event", "calendar.delete_event"),
])
def test_both_namespaces_a_model_might_use_resolve(alias, real):
    """The model reasons in FAMILIES ("email") and `authorization_evidence` keys on PROVIDERS
    ("google_calendar"); the registry keys on neither consistently. All three now land."""
    assert tool_registry.canonical(alias) == real
    assert tool_registry.get(real) is not None


def test_the_alias_map_is_derived_and_can_never_point_at_nothing():
    """Hand-listed aliases rot. Every alias must resolve to a capability that EXISTS, and no alias may
    shadow a real id — a rename that broke either is a test failure rather than a live 'i'll send'."""
    for alias, real in tool_registry._ALIASES.items():
        assert tool_registry.get(real) is not None, f"{alias} -> {real}, which is not a capability"
        assert tool_registry.get(alias) is None, f"{alias} shadows a real capability id"


def test_canonicalization_translates_and_never_invents():
    """The safety property. It may rescue a badly-named turn; it may not conjure a capability."""
    for unknown in ("sending messages", "email.bogus", "slack.post_message", "", "   ", None):
        cap = tool_registry.canonical(unknown)
        assert tool_registry.get(cap) is None, f"{unknown!r} was invented into {cap!r}"
    # and an unknown one still refuses to open a goal, exactly as it did before
    v = goal_runtime.creation_verdict(
        decision=_decision(), capability=tool_registry.canonical("slack.post_message"),
        continuation=False)
    assert not v.create and v.reason == goal_runtime.UNKNOWN_CAPABILITY


def test_a_real_id_is_never_rewritten():
    for cap in [t.capability for t in tool_registry._TOOLS]:
        assert tool_registry.canonical(cap) == cap


# ==========================================================================================================
# 2. THE PROMISE
# ==========================================================================================================

def test_the_exact_sentence_bruce_said_is_a_promise():
    """THE REGRESSION, again. This is verbatim what the founder was told."""
    said = ("yep, i can do that. i'll send an email to dhruvhydrox@gmail.com with "
            "subject \u201cbruce staging test b515b40\u201d and that body.")
    assert response_composer.promises_action(said)


@pytest.mark.parametrize("reply", [
    "want me to send it?",
    "ok here's the email, going to a@b.c:\n\nsubject: hi\n\nbody text\n\nwant me to send it?",
    "sent it to a@b.c \u2705",
    "ok, i won't send it.",
    "i can help write it if u want",
    "56",
    "",
])
def test_an_offer_a_receipt_and_a_refusal_are_not_promises(reply):
    """The partner to every promise case. A gate that fired on all of these would replace Bruce's
    proposal — the very sentence the confirmation depends on — with a limitation."""
    assert not response_composer.promises_action(reply), reply


def test_a_promise_with_no_decision_behind_it_is_downgraded():
    """THE FIX. No Goal, no Decision, no action handler -> Bruce may not say it will send."""
    said = "yep, i can do that. i'll send an email to a@b.c with that body."
    out = response_composer.no_false_promise(said, handler="conversation", has_live_decision=False)
    assert out != said
    assert "i'm not gonna say i will" in out
    assert "?" in out, "a limitation with no question is where a student stops trying"


def test_a_promise_survives_when_a_pending_decision_actually_exists():
    """The anti-vacuous partner. If nothing could ever promise, Bruce could not tell a student what it is
    about to do once they say yes."""
    said = "i'll send it as soon as u say go"
    assert response_composer.no_false_promise(
        said, handler="conversation", has_live_decision=True) == said


def test_an_action_handlers_conditional_promise_survives():
    """`goal_handler._blocked` says "reconnect it with that turned on and i'll send this email." That is a
    truthful conditional from a handler that only speaks from real state, and downgrading it would delete
    the one sentence that tells the student how to fix their own connection."""
    said = ("ur gmail is connected but i was never given permission to send email. "
            "reconnect it with that turned on and i'll send this email.")
    assert response_composer.no_false_promise(
        said, handler="goal", has_live_decision=False) == said


def test_the_promise_gate_does_not_disturb_the_completion_gate():
    """Two different lies, two different gates, and neither may swallow the other's cases."""
    landed = "added it to ur calendar \u2705"
    assert response_composer.claims_action_completion(landed)
    assert not response_composer.promises_action(landed)


def test_the_unknown_operation_path_ends_in_a_truthful_limitation():
    """The whole failure, end to end, with the ALIAS DELIBERATELY UNAVAILABLE — the second fix has to
    hold on its own, or it is only passing because the first one rescued the turn.

    An operation the registry does not declare creates no goal, and the reply that would have promised it
    becomes a limitation plus one question.
    """
    unknown = tool_registry.canonical("slack.post_message")
    verdict = goal_runtime.creation_verdict(decision=_decision(caps=[unknown]), capability=unknown,
                                            continuation=False)
    assert not verdict.create, "an unknown operation opened durable state"

    promised = "sure, i'll post that to slack for u."
    out = response_composer.no_false_promise(promised, handler="conversation", has_live_decision=False)
    assert out != promised and "i'm not gonna say i will" in out


def test_a_conditional_offer_backed_by_real_state_survives():
    """The false positive the suite caught. The flyer lane says "want me to add it to ur google calendar?
    just say the word and i'll put it on there." — the second sentence promises, and it is TRUE, because
    an EventCandidate is saved and waiting. Downgrading it broke the offer -> "ya" -> execute flow.

    `_has_live_decision` counts BOTH lanes for exactly this reason; here the pure gate is pinned so the
    rule "real state licences the promise" cannot be quietly narrowed back to one lane.
    """
    offer = ("got it, saved this event:\nrehearsal\nfriday at 4pm\n"
             "want me to add it to ur google calendar? just say the word and i'll put it on there.")
    assert response_composer.promises_action(offer), "the sentence really is a promise"
    assert response_composer.no_false_promise(
        offer, handler="event_candidate", has_live_decision=True) == offer
    # ...and with NOTHING saved, the same words are not allowed to stand.
    assert response_composer.no_false_promise(
        offer, handler="event_candidate", has_live_decision=False) != offer


def test_the_downgrade_never_denies_an_ability_bruce_has():
    """"i can't send messages for you" was the transcript's OTHER lie, told while the broker was
    answering ok=True in the same process. The honest non-promise must not become that."""
    out = response_composer.no_false_promise(
        "i'll send it", handler="conversation", has_live_decision=False)
    for denial in ("can't", "cannot", "unable", "not able"):
        assert denial not in out.lower(), f"the downgrade denies an ability: {out!r}"
