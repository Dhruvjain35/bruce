"""The typed structured-output contract for Bruce's conversation brain (Bite 1).

The reasoner returns EXACTLY these 13 fields — and there is NO chain-of-thought / scratchpad field
anywhere, ever. The validated object is what gets persisted per turn (conversation_turns.decision
JSONB). Pure Pydantic, no vendor types: it doubles as the model's output schema and the durable shape.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class IntentKind(str, enum.Enum):
    casual = "casual"
    educational_help = "educational_help"
    image_understanding = "image_understanding"
    doc_understanding = "doc_understanding"
    extraction = "extraction"
    research = "research"
    actionable = "actionable"
    clarification = "clarification"
    approval = "approval"
    status_cancel_correction = "status_cancel_correction"
    unsupported = "unsupported"


class ResponseType(str, enum.Enum):
    direct_answer = "direct_answer"
    tutoring = "tutoring"
    summary = "summary"
    extraction_result = "extraction_result"
    clarification = "clarification"
    mission_ack = "mission_ack"
    status = "status"
    refusal = "refusal"


class RiskLevel(str, enum.Enum):
    none = "none"
    low = "low"
    sensitive = "sensitive"
    high = "high"


class ExtractedEntity(BaseModel):
    """A grounded entity pulled from the message/attachment — each carries the verbatim span it came
    from so nothing is invented."""

    type: str                                   # e.g. "event_title", "date", "location", "amount"
    value: str
    normalized: str | None = None               # ISO date / normalized form, when unambiguous
    source_span: str | None = None              # verbatim text it was read from (grounding)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ConversationDecision(BaseModel):
    """EXACTLY the 13 contract fields. No reasoning/scratchpad field — Bruce never persists CoT."""

    intent: IntentKind
    response_type: ResponseType
    user_visible_response: str                          # the reply, BEFORE voice styling
    attachment_summary: str | None = None               # grounded description; null if no attachment
    extracted_entities: list[ExtractedEntity] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    needs_mission: bool = False
    proposed_goal: str | None = None                    # plain-language goal if a mission is warranted
    required_capabilities: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.none
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    unsupported_reason: str | None = None
