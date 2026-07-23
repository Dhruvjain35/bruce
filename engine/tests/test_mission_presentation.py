"""P0 MissionStartPresentation — context-aware ack + status generated from state, never a canned line."""

from __future__ import annotations

from bruce_engine import mission_presentation as mp
from bruce_engine.conversation_contract import (
    ConversationDecision, ExtractedEntity, IntentKind, ResponseType, RiskLevel,
)

EM = "—"


def _dec(entities):
    return ConversationDecision(
        intent=IntentKind.casual, response_type=ResponseType.direct_answer, user_visible_response="x",
        extracted_entities=entities, risk_level=RiskLevel.none, confidence=0.8)


def test_extract_flyer_facts_grounded_only():
    d = _dec([ExtractedEntity(type="event_title", value="Startup School 2026"),
              ExtractedEntity(type="date", value="July 25-26", normalized="2026-07-25"),
              ExtractedEntity(type="location", value="Chase Center, SF"),
              ExtractedEntity(type="price", value="$40"),
              ExtractedEntity(type="registration_url", value="https://ex.com/reg"),
              ExtractedEntity(type="gibberish", value="ignore me")])
    f = mp.extract_flyer_facts(d)
    assert f["event"] == "Startup School 2026" and f["location"] == "Chase Center, SF"
    assert f["date"] == "2026-07-25" and f["price"] == "$40" and f["url"] == "https://ex.com/reg"
    assert "gibberish" not in f.values()


def test_low_confidence_entity_is_dropped():
    d = _dec([ExtractedEntity(type="event_title", value="Maybe Event", confidence=0.1)])
    assert "event" not in mp.extract_flyer_facts(d)


def _pres(facts):
    return mp.MissionStartPresentation(mission_id="m", user_goal="g", capability="c",
                                       source_summary="flyer", extracted_facts=facts)


def test_render_start_specific_when_confident():
    out = mp.render_start(_pres({"event": "Startup School 2026", "location": "Chase Center", "date": "2026-07-25"}))
    assert "Startup School 2026" in out and "Chase Center" in out and "2026-07-25" in out
    assert "checking the details" in out and "ping u" in out
    assert EM not in out


def test_render_start_generic_when_no_facts_and_never_claims_completion():
    out = mp.render_start(_pres({}))
    assert "pulling the dates" in out
    for claim in ("registered", "submitted", "booked", "signed you up", "done"):
        assert claim not in out.lower()
    assert EM not in out


def test_render_status_from_persisted_state_is_honest():
    state = {"phase": "understanding", "goal": {"proposed_goal": "the hackathon reg",
             "extracted_facts": {"event": "Startup School", "date": "2026-07-25"}}}
    out = mp.render_status(state)
    assert "Startup School" in out
    assert "haven't submitted or registered" in out.lower()      # honest: no external action yet
    assert EM not in out
