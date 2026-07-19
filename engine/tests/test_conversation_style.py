"""Bite 1 Voice OS — prohibited-phrase absence, serious-context precision, and the HARD fact-
preservation guard (styling may never change a number/date/price/url/@handle/email)."""

from __future__ import annotations

import pytest

from bruce_engine.conversation_contract import RiskLevel
from bruce_engine.conversation_style import (
    ConversationStyleEngine, StyleViolation, VoiceProfile, assert_facts_preserved,
)

eng = ConversationStyleEngine()


def test_prohibited_phrases_stripped():
    out = eng.render("Great question! I'd be happy to help. as an ai i can look at this").lower()
    for p in ("great question", "i'd be happy to", "as an ai"):
        assert p not in out


def test_serious_context_strips_emoji_and_keeps_facts():
    out = eng.render("sending $25 to @coach_lee on 2026-03-14 🎉", risk_level=RiskLevel.high)
    assert "🎉" not in out
    for fact in ("$25", "@coach_lee", "2026-03-14"):
        assert fact in out


def test_fact_preservation_guard_rejects_dropped_fact():
    with pytest.raises(StyleViolation):
        assert_facts_preserved("registration is $40 by 2026-05-01", "registration is due soon")


def test_facts_survive_casual_lowercasing():
    out = eng.render("Applications due 2026-05-01, fee $40, email admin@school.edu",
                     profile=VoiceProfile(lowercase=True))
    for fact in ("2026-05-01", "$40", "admin@school.edu"):
        assert fact in out


def test_event_template_never_claims_added_and_is_fact_locked():
    out = eng.template("event_saved_calendar_unavailable", title="Startup School 2026",
                       when="jul 25-26\n", where="chase center, sf")
    assert "Startup School 2026" in out and "chase center, sf" in out
    assert "calendar" in out.lower()
    assert "added to your calendar" not in out.lower()   # honest: never claims it was added


def test_could_not_read_template_asks_for_resend():
    assert "resend" in eng.template("could_not_read_attachment").lower()
