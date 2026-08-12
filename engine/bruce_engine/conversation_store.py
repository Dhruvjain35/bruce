"""Persistence + retrieval for the conversation runtime. Every write/read happens under
user_session(user_id) so Postgres RLS enforces tenancy on the most sensitive student free-text.
Idempotent on (channel, provider_message_id, role) and on the event-candidate idempotency key.
"""

from __future__ import annotations

import dataclasses
import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import schema
from .conversation_contract import ConversationDecision
from .db import user_session


@dataclasses.dataclass
class TurnBrief:
    role: str          # "user" | "assistant"
    text: str | None


async def _turn_id(s, user_id, channel, provider_message_id, role) -> UUID | None:
    return (await s.execute(select(schema.ConversationTurn.id).where(
        schema.ConversationTurn.user_id == user_id,
        schema.ConversationTurn.channel == channel,
        schema.ConversationTurn.provider_message_id == provider_message_id,
        schema.ConversationTurn.role == role,
    ))).scalar_one_or_none()


async def _turn_exists(s, user_id, channel, provider_message_id, role) -> bool:
    return await _turn_id(s, user_id, channel, provider_message_id, role) is not None


@dataclasses.dataclass(frozen=True)
class TurnClaim:
    """Who owns this inbound message. `claimed` is True for EXACTLY ONE caller, decided by Postgres."""

    turn_id: UUID | None
    claimed: bool


async def claim_inbound_turn(user_id: UUID, *, channel: str, channel_identity: str,
                             provider_message_id: str, text: str | None) -> TurnClaim:
    """Atomically claim the right to run this inbound message as a turn.

    THE INVARIANT: one canonical inbound provider message -> at most one active Bruce turn -> at most one
    consequential execution.

    WHY THE INSERT IS THE CLAIM. `uq_turn_msg_role` already constrains
    (user_id, channel, provider_message_id, role) to be unique, so `ON CONFLICT DO NOTHING RETURNING id`
    makes the DATABASE pick the winner: exactly one caller gets a row back no matter how many arrive at
    once. No SELECT decides anything, so there is no window between the check and the act.

    WHAT IT REPLACES. `conversation_runtime._already_answered` asked whether an ASSISTANT turn existed —
    a row written by `_finalize` at the very END of the turn, after the reasoner, the composer and any
    Gmail send. Two concurrent deliveries both saw no assistant row and both proceeded, several seconds
    apart from writing one. That window is why DEFECT-5's "just retry the timed-out POST" fix could not
    be shipped as written: the retry lands inside it and the professor gets the mail twice.

    The predecessor of this function could not be used as the decision either — it returned the existing
    id on a redelivery, which is indistinguishable from having created it.

    NO LEASE, NO TAKEOVER, DELIBERATELY. A turn whose process dies holds its claim forever and a
    redelivery is refused. That is what "at most one consequential execution" means, and it is not a
    regression: `relay.py` returns "retry" on timeout without checkpointing or queueing, so a crashed
    turn is already lost today. Bounded recovery needs a lease plus a marker for "consequential work has
    started" — otherwise a takeover after a send is exactly the double-send this closes — and those are
    columns this table does not have.
    """
    async with user_session(user_id) as s:
        table = schema.ConversationTurn.__table__
        won = (await s.execute(
            pg_insert(table)
            .values(user_id=user_id, channel=channel, channel_identity=channel_identity,
                    provider_message_id=provider_message_id, role="user", text=text)
            .on_conflict_do_nothing(constraint="uq_turn_msg_role")
            .returning(table.c.id))).scalar_one_or_none()
        if won is not None:
            return TurnClaim(turn_id=won, claimed=True)
        # Lost. Still return the canonical id: the caller may need it to reference the turn even though
        # it must not act on it, and a loser that cannot name the turn cannot log about it either.
        return TurnClaim(turn_id=await _turn_id(s, user_id, channel, provider_message_id, "user"),
                         claimed=False)


async def persist_user_turn(user_id: UUID, *, channel: str, channel_identity: str,
                            provider_message_id: str, text: str | None) -> UUID:
    """Write THE canonical inbound-turn row and return its id.

    IT RETURNS THE ID NOW, and that is not a convenience. This row is the canonical inbound-turn ledger —
    the one thing shadow observation is reconciled against — and until the id came back out of here, the
    shadow job could only reference the turn by re-deriving (channel, provider_message_id). Two ledgers
    joined on a reconstruction instead of on an identity is how a reconciliation ends up agreeing with
    itself; `semantic_shadow_jobs.conversation_turn_id` (migration 0033) is that identity, and this is
    where it comes from.

    A redelivery returns the EXISTING id rather than None, so the caller's downstream write is idempotent
    for the same reason this one is instead of skipped alongside it.

    A THIN WRAPPER over `claim_inbound_turn` now, keeping this signature for the callers that only ever
    wanted the id (the shadow ledger, and five test modules). The atomicity lives in one place; anything
    that needs to DECIDE on ownership must call `claim_inbound_turn` and read `claimed`, because "I got
    an id back" and "I created this row" are exactly the two facts this function cannot tell apart.
    """
    return (await claim_inbound_turn(
        user_id, channel=channel, channel_identity=channel_identity,
        provider_message_id=provider_message_id, text=text)).turn_id


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
