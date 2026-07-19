"""Bite 1 Voice OS — prohibited-phrase absence, serious-context precision, and the HARD fact-
preservation guard (styling may never change a number/date/price/url/@handle/email)."""

from __future__ import annotations

import pytest

from bruce_engine.conversation_contract import RiskLevel
from bruce_engine.conversation_style import (
    ConversationStyleEngine, StyleViolation, VoiceProfile, assert_facts_preserved, enforce_no_dashes,
)

eng = ConversationStyleEngine()
EM, EN = "—", "–"


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


# --- em-dash enforcement (student-facing Bruce never uses em dashes) --------------------------------

def test_render_strips_em_dash_the_live_cases():
    for src in ("yeah — unit circle stuff is mostly memorizing", "hey, not much — what's up?"):
        out = eng.render(src)
        assert EM not in out
    assert eng.render("yeah — unit circle stuff") == "yeah, unit circle stuff"


def test_render_strips_en_dash_used_as_punctuation():
    assert EN not in eng.render("the plan is simple – bring water and a pen")


def test_numeric_range_en_dash_is_preserved_as_a_fact():
    # a time/number range is a FACT, not sentence punctuation -> left intact, and facts survive
    out = eng.render("study block is 9:00 – 10:00 tomorrow")
    assert "9:00" in out and "10:00" in out and EN in out


def test_enforce_no_dashes_is_idempotent():
    once = enforce_no_dashes("a — b — c")
    assert enforce_no_dashes(once) == once and EM not in once


def test_template_output_never_has_em_dash():
    out = eng.template("unsupported_capability", capability="calendar", alternative="save it for you")
    assert EM not in out


def test_repo_conversation_fixture_scan_no_em_dash_in_shipped_copy():
    """Repository-wide conversation fixture scan: no shipped message template may contain an em dash."""
    offenders = {k: v for k, v in ConversationStyleEngine().templates.items() if EM in str(v)}
    assert not offenders, f"em dash in shipped copy: {sorted(offenders)}"
