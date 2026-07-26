"""Notification harness — factual accuracy first, then whether it sounds like a person.

The live failure that motivated this: Bruce texted "hey, dhruvhydrox@gmail.com replied to the email i
sent for you". Two things were wrong. It was FALSE (nobody had replied), and even had it been true it
reads like a database row: an email address where a name belongs, and "the email i sent for you" as
filler nobody says out loud.

Scenarios below are table-driven so the grading criteria — accuracy, brevity, relationship fit, no
system language, no ungrounded facts — are applied uniformly to every case rather than ad hoc.
"""

from __future__ import annotations

import pytest

from bruce_engine.notification_brief import (MAX_CHARS, NotificationBrief, Relationship, Urgency,
                                             deterministic_notification, validate_notification)

AI_TELLS = ("i detected", "notification", "monitored thread", "on your behalf", "reply_detected",
            "delve", "furthermore", "additionally", "it is important to note")


def _brief(**kw) -> NotificationBrief:
    base = dict(event_type="reply_received", verified=True, agent_run_id="run-1", receipt_id="run-1:notify")
    base.update(kw)
    return NotificationBrief(**base)


# ---------------------------------------------------------------------------------------------------
# Scenario table: (id, brief kwargs, must_contain, must_not_contain)
# ---------------------------------------------------------------------------------------------------
SCENARIOS = [
    ("teacher_named", dict(actor_name="mr. smith", actor_relationship=Relationship.teacher,
                           reply_summary="the assignment's due friday", confidence=0.9),
     ["mr. smith", "friday"], ["@"]),
    ("teacher_unnamed", dict(actor_relationship=Relationship.teacher), ["teacher"], ["@"]),
    ("coach_named", dict(actor_name="coach", actor_relationship=Relationship.coach,
                         reply_summary="practice moved to 6", confidence=0.95), ["coach", "6"], ["@"]),
    ("coach_unnamed", dict(actor_relationship=Relationship.coach), ["coach"], ["@"]),
    ("professional", dict(actor_name="maya", actor_relationship=Relationship.professional,
                          reply_summary="about the internship", confidence=0.9), ["maya"], ["@"]),
    ("peer", dict(actor_name="sam", actor_relationship=Relationship.peer), ["sam"], ["@"]),
    ("counselor", dict(actor_relationship=Relationship.counselor), ["counselor"], ["@"]),
    ("support", dict(actor_relationship=Relationship.support), ["replied"], ["@"]),
    ("unknown_sender", dict(actor_relationship=Relationship.unknown), ["replied"], []),
    ("self_reply", dict(actor_relationship=Relationship.self_), ["ur reply"], ["@"]),
    ("deadline_change", dict(actor_name="mr. smith", actor_relationship=Relationship.teacher,
                             reply_summary="deadline moved to the 12th", confidence=0.9,
                             important_dates=("the 12th",)), ["12"], []),
    ("missing_file", dict(actor_name="mr. smith", actor_relationship=Relationship.teacher,
                          reply_summary="he needs the form", confidence=0.9,
                          important_requests=("the form",)), ["form"], []),
    ("acceptance", dict(actor_name="maya", actor_relationship=Relationship.professional,
                        reply_summary="you got the spot", confidence=0.92), ["maya"], []),
    ("rejection", dict(actor_name="maya", actor_relationship=Relationship.professional,
                       reply_summary="they went with someone else", confidence=0.9), ["maya"], []),
    ("ambiguous_low_conf", dict(actor_name="mr. smith", actor_relationship=Relationship.teacher,
                                reply_summary="something about scheduling", confidence=0.4),
     ["mr. smith"], ["something about scheduling"]),
    ("sensitive", dict(actor_name="ms. lee", actor_relationship=Relationship.counselor,
                       reply_summary="about your medical accommodation", confidence=0.95,
                       sensitive_content=True), ["ms. lee"], ["medical"]),
    ("no_actionable_content", dict(actor_name="sam", actor_relationship=Relationship.peer,
                                   reply_summary=None), ["sam"], []),
    ("urgent", dict(actor_name="coach", actor_relationship=Relationship.coach,
                    reply_summary="he needs an answer today", confidence=0.9,
                    urgency=Urgency.high), ["coach"], []),
    ("appointment_confirm", dict(actor_name="the clinic", actor_relationship=Relationship.support,
                                 reply_summary="ur appointment is confirmed", confidence=0.9),
     ["clinic"], []),
    ("with_next_action", dict(actor_name="mr. smith", actor_relationship=Relationship.teacher,
                              reply_summary="he wants the form", confidence=0.9,
                              suggested_next_action="want me to send it?"), ["form"], []),
    ("no_reply_event", dict(event_type="no_reply", verified=True), ["no reply"], ["replied"]),
    ("send_failed_event", dict(event_type="send_failed", verified=True), ["didn't send"], ["replied"]),
]


@pytest.mark.parametrize("sid,kw,must,must_not", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_scenario_is_accurate_brief_and_human(sid, kw, must, must_not):
    brief = _brief(**kw)
    text = deterministic_notification(brief)
    low = text.lower()

    # factual accuracy — the validator is the same gate production uses
    verdict = validate_notification(text, brief)
    assert verdict.ok, f"{sid}: validator rejected the floor: {verdict.issues} :: {text}"

    # brevity
    assert len(text) <= MAX_CHARS, f"{sid}: {len(text)} chars"

    # naturalness
    assert "—" not in text, f"{sid}: em dash"
    for tell in AI_TELLS:
        assert tell not in low, f"{sid}: AI/system phrasing {tell!r}"

    for frag in must:
        assert frag.lower() in low, f"{sid}: expected {frag!r} in {text!r}"
    for frag in must_not:
        assert frag.lower() not in low, f"{sid}: {frag!r} must not appear in {text!r}"


# --- the specific live failure ------------------------------------------------------------------------

def test_the_actual_bad_notification_is_rejected():
    """Verbatim from production. Fails on both counts: an address where a name belongs, and the
    'email i sent for you' filler."""
    brief = _brief(actor_name="mr. smith", actor_relationship=Relationship.teacher)
    bad = "hey, dhruvhydrox@gmail.com replied to the email i sent for you"
    v = validate_notification(bad, brief)
    assert not v.ok
    assert "address_when_name_known" in v.issues and "system_language" in v.issues


def test_a_reply_claim_requires_a_verified_reply():
    """The false-notification class. Words cannot outrun the facts."""
    unverified = _brief(event_type="reply_received", verified=False, actor_name="coach")
    assert "unverified_reply_claim" in validate_notification("coach replied", unverified).issues
    no_reply = _brief(event_type="no_reply", verified=True)
    assert "unverified_reply_claim" in validate_notification("they replied", no_reply).issues


def test_summaries_and_numbers_must_be_grounded():
    brief = _brief(actor_name="coach", actor_relationship=Relationship.coach)
    assert "invented_summary" in validate_notification("coach replied. he said yes", brief).issues
    assert "ungrounded_number" in validate_notification("coach replied. practice at 6", brief).issues
    grounded = _brief(actor_name="coach", actor_relationship=Relationship.coach,
                      reply_summary="practice at 6", confidence=0.9)
    assert validate_notification("coach replied. practice at 6", grounded).ok


def test_low_confidence_offers_instead_of_asserting():
    brief = _brief(actor_name="mr. smith", actor_relationship=Relationship.teacher,
                   reply_summary="maybe about the field trip", confidence=0.3)
    text = deterministic_notification(brief)
    assert "field trip" not in text.lower()
    assert "want me to pull out" in text.lower()


def test_sensitive_content_is_never_summarised():
    brief = _brief(actor_name="ms. lee", actor_relationship=Relationship.counselor,
                   reply_summary="about your medical accommodation", confidence=0.99,
                   sensitive_content=True)
    text = deterministic_notification(brief)
    assert "medical" not in text.lower() and "accommodation" not in text.lower()


def test_address_used_only_when_it_is_the_only_identity():
    anon = _brief(actor_relationship=Relationship.unknown, actor_address="who@x.com")
    assert validate_notification("u got a reply from who@x.com", anon).ok
    named = _brief(actor_name="maya", actor_relationship=Relationship.professional)
    assert "address_when_name_known" in validate_notification("maya@x.com replied", named).issues


def test_floor_never_repeats_one_fixed_template_across_relationships():
    """A fixed template is what produced the bad text. Different events must not all read identically."""
    texts = {sid: deterministic_notification(_brief(**kw)) for sid, kw, _, _ in SCENARIOS}
    assert len(set(texts.values())) >= len(texts) // 2, "too many scenarios collapse to the same string"
    openings = [t.split()[0] for t in texts.values()]
    assert len(set(openings)) > 1, "every notification opens with the same word"
