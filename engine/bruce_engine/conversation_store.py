"""Persistence + retrieval for the conversation runtime. Every write/read happens under
user_session(user_id) so Postgres RLS enforces tenancy on the most sensitive student free-text.
Idempotent on (channel, provider_message_id, role) and on the event-candidate idempotency key.
"""

from __future__ import annotations

import dataclasses
import datetime
from uuid import UUID

from sqlalchemy import select

from . import schema
from .conversation_contract import ConversationDecision
from .db import user_session


@dataclasses.dataclass
class TurnBrief:
    role: str          # "user" | "assistant"
    text: str | None


async def _turn_exists(s, user_id, channel, provider_message_id, role) -> bool:
    return (await s.execute(select(schema.ConversationTurn.id).where(
        schema.ConversationTurn.user_id == user_id,
        schema.ConversationTurn.channel == channel,
        schema.ConversationTurn.provider_message_id == provider_message_id,
        schema.ConversationTurn.role == role,
    ))).scalar_one_or_none() is not None


async def persist_user_turn(user_id: UUID, *, channel: str, channel_identity: str,
                            provider_message_id: str, text: str | None) -> None:
    async with user_session(user_id) as s:
        if await _turn_exists(s, user_id, channel, provider_message_id, "user"):
            return
        s.add(schema.ConversationTurn(
            user_id=user_id, channel=channel, channel_identity=channel_identity,
            provider_message_id=provider_message_id, role="user", text=text))


async def persist_assistant_turn(user_id: UUID, *, channel: str, channel_identity: str,
                                 provider_message_id: str, decision: ConversationDecision,
                                 styled_text: str, mission_id: UUID | None = None,
                                 event_candidate_id: UUID | None = None) -> None:
    async with user_session(user_id) as s:
        if await _turn_exists(s, user_id, channel, provider_message_id, "assistant"):
            return
        s.add(schema.ConversationTurn(
            user_id=user_id, channel=channel, channel_identity=channel_identity,
            provider_message_id=provider_message_id, role="assistant",
            intent=decision.intent.value, response_type=decision.response_type.value,
            text=styled_text, decision=decision.model_dump(mode="json"),   # 13 fields only, no CoT
            risk_level=decision.risk_level.value, confidence=decision.confidence,
            mission_id=mission_id, event_candidate_id=event_candidate_id))


async def load_recent_turns(user_id: UUID, *, channel: str, channel_identity: str,
                            limit: int = 8) -> list[TurnBrief]:
    """A bounded window of recent turns (text only — never the decision JSONB/CoT), oldest→newest."""
    async with user_session(user_id) as s:
        rows = (await s.execute(select(schema.ConversationTurn)
                .where(schema.ConversationTurn.user_id == user_id,
                       schema.ConversationTurn.channel == channel,
                       schema.ConversationTurn.channel_identity == channel_identity)
                .order_by(schema.ConversationTurn.created_at.desc()).limit(limit))).scalars().all()
    return [TurnBrief(role=r.role, text=r.text) for r in reversed(rows)]


async def persist_event_candidate(user_id: UUID, *, title: str, idempotency_key: str,
                                  starts_at: datetime.datetime | None = None,
                                  ends_at: datetime.datetime | None = None, all_day: bool = False,
                                  location: str | None = None, confidence: float | None = None,
                                  missing_fields: list | None = None, provenance: dict | None = None,
                                  source_id: UUID | None = None,
                                  inbound_message_id: UUID | None = None) -> UUID:
    """Persist a reviewed event candidate (status='proposed'). Idempotent per (user, key)."""
    async with user_session(user_id) as s:
        existing = (await s.execute(select(schema.EventCandidate).where(
            schema.EventCandidate.user_id == user_id,
            schema.EventCandidate.idempotency_key == idempotency_key))).scalar_one_or_none()
        if existing is not None:
            return existing.id
        ec = schema.EventCandidate(
            user_id=user_id, title=title, starts_at=starts_at, ends_at=ends_at, all_day=all_day,
            location=location, confidence=confidence, missing_fields=missing_fields,
            provenance=provenance, source_id=source_id, inbound_message_id=inbound_message_id,
            status="proposed", idempotency_key=idempotency_key)
        s.add(ec)
        await s.flush()
        return ec.id
