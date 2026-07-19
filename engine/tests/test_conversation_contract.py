"""Bite 1 — the conversation-brain structured contract: EXACTLY 13 fields, no chain-of-thought."""

from __future__ import annotations

import pytest

from bruce_engine.conversation_contract import (
    ConversationDecision, IntentKind, ResponseType, RiskLevel,
)

_EXPECTED = {
    "intent", "response_type", "user_visible_response", "attachment_summary", "extracted_entities",
    "needs_clarification", "clarification_question", "needs_mission", "proposed_goal",
    "required_capabilities", "risk_level", "confidence", "unsupported_reason",
}


def test_contract_has_exactly_13_fields_and_no_cot():
    fields = set(ConversationDecision.model_fields)
    assert fields == _EXPECTED
    assert not (fields & {"reasoning", "scratchpad", "thoughts", "chain_of_thought", "cot", "notes"})


def test_contract_validates_and_bounds_confidence():
    d = ConversationDecision(intent=IntentKind.casual, response_type=ResponseType.direct_answer,
                             user_visible_response="yo")
    assert d.confidence == 0.5 and d.risk_level is RiskLevel.none and d.needs_mission is False
    with pytest.raises(Exception):
        ConversationDecision(intent=IntentKind.casual, response_type=ResponseType.direct_answer,
                             user_visible_response="x", confidence=1.5)
