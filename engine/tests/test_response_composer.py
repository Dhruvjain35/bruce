"""ResponseComposer harness (G0.6) — pins the response-experience invariants: a verified action handler's
success copy is trusted, a NON-action reply that fabricates a calendar completion is downgraded to an honest
non-claim, ordinary chat is never touched, and the natural-voice compose stays fact-safe + em-dash-free."""

from __future__ import annotations

from bruce_engine import response_composer as rc
from bruce_engine.conversation_style import ConversationStyleEngine, VoiceProfile


# --- no_false_completion (the load-bearing guarantee) ---------------------------------------------

def test_action_handler_success_copy_passes_through():
    """A verified calendar handler legitimately claims completion (it only says ✅ after a read-back)."""
    reply = "done, chess club is now on ur calendar for july 25 ✅"
    for h in ("calendar_schedule", "calendar_approval", "calendar_mutation"):
        assert rc.no_false_completion(reply, handler=h) == reply


def test_fabricated_calendar_completion_from_non_action_handler_is_downgraded():
    """The exact failure the user hated: a reply that says it added something, with NO action handler behind
    it, is a lie -> honest non-claim."""
    fabricated = "sure, i added it to ur calendar ✅"
    out = rc.no_false_completion(fabricated, handler="default_reply")
    assert out != fabricated
    assert "didn't actually" in out and "want me to" in out       # honest, offers to really do it


def test_various_fabricated_completions_are_caught():
    for reply in (
        "no worries, i put startup school on ur calendar ✅",
        "deleted chess club from ur calendar for you",
        "moved it to 9pm ✅",
        "it's on the books!",
    ):
        assert rc.claims_action_completion(reply), reply
        assert rc.no_false_completion(reply, handler="default_reply") != reply


def test_ordinary_chat_is_never_touched():
    """Casual replies with completion WORDS but no calendar object must pass untouched (no false positive)."""
    for reply in (
        "nice, i'm done with my homework too lol",
        "you're all set for the test, just review chapter 3",
        "yeah i can add that to ur calendar if u want",     # OFFER, not a claim
        "that's scheduled for finals week at school, not something i track",
    ):
        assert not rc.claims_action_completion(reply), reply
        assert rc.no_false_completion(reply, handler="default_reply") == reply


def test_offer_is_not_a_completion_claim():
    reply = "want me to add chess club to ur calendar?"
    assert not rc.claims_action_completion(reply)
    assert rc.no_false_completion(reply, handler="default_reply") == reply


def test_offers_with_calendar_verbs_are_not_false_positives():
    """Adversarial-probe regressions: future/modal OFFERS containing 'put/added ... on your calendar' must
    NOT be downgraded — that would replace a good event-candidate offer with an honest-non-claim."""
    for offer in (
        "i'll put it on ur calendar once you connect google",
        "i can put that on your calendar if you want",
        "i'll get startup school added to your calendar once connected",
        "want me to move it to 9pm? ✅ or ❌",
        "that's on your calendar already right?",
        "you're all set — it's scheduled for finals week at school",   # non-Bruce event, no receipt
        "i saved your timezone as central",
    ):
        assert not rc.claims_action_completion(offer), offer
        assert rc.no_false_completion(offer, handler="default_reply") == offer


def test_object_and_receipt_anchored_fabrications_are_caught():
    """Adversarial-probe regressions: receipts that omit the literal 'calendar' word but carry a ✅ or an
    explicit first-person past tense are still fabrications."""
    for fabricated in (
        "it is now on your calendar",
        "it's in your calendar now",
        "cool, i scheduled that for you",
        "added ✅",
        "i went ahead and added chess club",
    ):
        assert rc.claims_action_completion(fabricated), fabricated
        assert rc.no_false_completion(fabricated, handler="default_reply") != fabricated


def test_real_receipt_survives_a_trailing_offer():
    """A genuine verified receipt with a follow-up question must still be caught (sentence-level scan)."""
    reply = "added chess club to ur calendar ✅. want me to add another?"
    assert rc.claims_action_completion(reply)


# --- compose (natural voice, delegating to the style engine) --------------------------------------

def test_compose_strips_filler_and_em_dashes():
    style = ConversationStyleEngine()
    out = rc.compose("I'd be happy to help — here's the plan.", style=style,
                     profile=VoiceProfile(lowercase=True))
    assert "—" not in out                                   # hard no-em-dash
    assert "happy to" not in out.lower()                    # corporate filler stripped


def test_compose_preserves_facts():
    style = ConversationStyleEngine()
    out = rc.compose("the meeting is at 3:30pm on 2026-09-03, room 214.", style=style)
    assert "3:30pm" in out and "2026-09-03" in out and "214" in out
