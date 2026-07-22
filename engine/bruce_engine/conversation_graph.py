"""ConversationContextGraph persistence (Bite 2 A2). Persistence ONLY — no retrieval, no chat.db
enrichment, no model-context assembly (that is A3).

A THIN graph over the existing content rows: every function references inbound_messages /
conversation_turns / outbound_messages / message_attachments / messaging_identities by id and stores
only relationships. Writes run under worker_session() (the ingestion path), which the tenant_or_worker
RLS policy admits; owner_user_id is ALWAYS the resolved linked user, and reconciliation/edges never
cross owners. Reaction / unsent / edit events update graph state and NEVER create a conversation turn.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import schema
from .db import worker_session
from .messaging import InboundMessage

# Relationship + reaction constants (provider-neutral).
REPLY_TO = "reply_to"
THREAD_ROOT = "thread_root"
EDIT_OF = "edit_of"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def resolve_owner(provider: str, channel_identity: str) -> UUID | None:
    """The LINKED user for a sender handle, or None if unlinked/blocked/disconnected. Worker-context
    lookup — the graph never persists for an unlinked sender (no owner)."""
    async with worker_session() as s:
        row = (await s.execute(select(schema.MessagingIdentity).where(
            schema.MessagingIdentity.channel == provider,
            schema.MessagingIdentity.channel_identity == channel_identity))).scalar_one_or_none()
        if row is None or row.blocked_at is not None or row.disconnected_at is not None:
            return None
        return row.user_id


async def _sender_identity_id(s, provider: str, channel_identity: str) -> UUID | None:
    row = (await s.execute(select(schema.MessagingIdentity.id).where(
        schema.MessagingIdentity.channel == provider,
        schema.MessagingIdentity.channel_identity == channel_identity))).scalar_one_or_none()
    return row


async def _reconcile(s, user_id: UUID, provider: str, provider_message_id: str, message_id: UUID) -> None:
    """A newly-ingested message becomes the target of any edge/reaction that referenced its provider id
    while it was still unresolved. Scoped to the SAME owner — never links across users."""
    await s.execute(update(schema.ConversationMessageRelationship)
        .where(schema.ConversationMessageRelationship.user_id == user_id,
               schema.ConversationMessageRelationship.provider == provider,
               schema.ConversationMessageRelationship.unresolved_target_provider_message_id == provider_message_id)
        .values(target_message_id=message_id, unresolved_target_provider_message_id=None))
    await s.execute(update(schema.ConversationReactionEvent)
        .where(schema.ConversationReactionEvent.user_id == user_id,
               schema.ConversationReactionEvent.provider == provider,
               schema.ConversationReactionEvent.unresolved_target_provider_message_id == provider_message_id)
        .values(target_message_id=message_id, unresolved_target_provider_message_id=None))


async def _upsert_message(s, user_id: UUID, *, provider: str, provider_message_id: str, direction: str,
                          **fields) -> UUID:
    """Idempotent canonical node on (provider, provider_message_id). Redelivery updates content refs +
    freshness, never a second row. Returns the canonical message id, then reconciles pending references."""
    values = {"user_id": user_id, "provider": provider, "provider_message_id": provider_message_id,
              "direction": direction, **{k: v for k, v in fields.items() if v is not None}}
    stmt = pg_insert(schema.ConversationMessage).values(**values)
    update_cols = {k: stmt.excluded[k] for k in fields if fields[k] is not None}
    update_cols["updated_at"] = _now()
    stmt = stmt.on_conflict_do_update(constraint="uq_conv_msg_provider", set_=update_cols) \
               .returning(schema.ConversationMessage.id)
    mid = (await s.execute(stmt)).scalar_one()
    await _reconcile(s, user_id, provider, provider_message_id, mid)
    return mid


async def _add_relationship(s, user_id: UUID, *, source_message_id: UUID, relationship_type: str,
                            provider: str, target_provider_message_id: str | None) -> None:
    """Create/keep one edge per (source, type). If the target message already exists it is linked now;
    otherwise the provider id is parked as unresolved and reconciled when the target arrives."""
    target_id = None
    if target_provider_message_id:
        target_id = (await s.execute(select(schema.ConversationMessage.id).where(
            schema.ConversationMessage.user_id == user_id,
            schema.ConversationMessage.provider == provider,
            schema.ConversationMessage.provider_message_id == target_provider_message_id))).scalar_one_or_none()
    stmt = pg_insert(schema.ConversationMessageRelationship).values(
        user_id=user_id, source_message_id=source_message_id, relationship_type=relationship_type,
        provider=provider, target_message_id=target_id,
        unresolved_target_provider_message_id=None if target_id else target_provider_message_id)
    await s.execute(stmt.on_conflict_do_nothing(constraint="uq_conv_rel_source_type"))


async def ingest_inbound_message(msg: InboundMessage) -> UUID | None:
    """Upsert the canonical inbound node + reply/thread edges for a LINKED user. Idempotent on the
    provider message id. Returns the canonical message id (None if unlinked / no owner)."""
    if msg.user_id is None:
        return None
    provider = msg.channel.value
    async with worker_session() as s:
        sender = await _sender_identity_id(s, provider, msg.channel_identity)
        mid = await _upsert_message(
            s, msg.user_id, provider=provider, provider_message_id=msg.provider_message_id,
            direction="inbound", provider_chat_id=msg.thread_id, service=msg.service,
            sender_identity_id=sender, received_at=msg.timestamp)
        if msg.reply_to_message_id:
            await _add_relationship(s, msg.user_id, source_message_id=mid, relationship_type=REPLY_TO,
                                    provider=provider, target_provider_message_id=msg.reply_to_message_id)
        if msg.thread_root_message_id:
            await _add_relationship(s, msg.user_id, source_message_id=mid, relationship_type=THREAD_ROOT,
                                    provider=provider, target_provider_message_id=msg.thread_root_message_id)
        return mid


async def ingest_outbound_message(user_id: UUID, *, provider: str, provider_message_id: str,
                                  provider_chat_id: str | None, outbound_message_id: UUID | None,
                                  conversation_turn_id: UUID | None = None, sent_at=None) -> UUID:
    """Upsert a canonical node for a reply Bruce sends (references outbound_messages / the turn)."""
    async with worker_session() as s:
        return await _upsert_message(
            s, user_id, provider=provider, provider_message_id=provider_message_id, direction="outbound",
            provider_chat_id=provider_chat_id, outbound_message_id=outbound_message_id,
            conversation_turn_id=conversation_turn_id, sent_at=sent_at)


async def record_reaction(user_id: UUID, *, provider: str, provider_event_id: str, reaction_type: str,
                          removed: bool, channel_identity: str | None = None,
                          target_provider_message_id: str | None = None,
                          occurred_at: datetime.datetime | None = None) -> None:
    """Persist a tapback add/remove (never a turn). Idempotent on (provider, provider_event_id). Target
    resolves now if present, else reconciles later. A removal is its own event, not a delete."""
    async with worker_session() as s:
        actor = await _sender_identity_id(s, provider, channel_identity) if channel_identity else None
        target_id = None
        if target_provider_message_id:
            target_id = (await s.execute(select(schema.ConversationMessage.id).where(
                schema.ConversationMessage.user_id == user_id,
                schema.ConversationMessage.provider == provider,
                schema.ConversationMessage.provider_message_id == target_provider_message_id))).scalar_one_or_none()
        stmt = pg_insert(schema.ConversationReactionEvent).values(
            user_id=user_id, provider=provider, provider_event_id=provider_event_id,
            reaction_type=reaction_type, removed=removed, actor_identity_id=actor,
            target_message_id=target_id, occurred_at=occurred_at or _now(),
            unresolved_target_provider_message_id=None if target_id else target_provider_message_id)
        await s.execute(stmt.on_conflict_do_nothing(constraint="uq_conv_reaction_event"))


async def mark_unsent(user_id: UUID, *, provider: str, provider_message_id: str,
                      unsent_at: datetime.datetime | None = None) -> None:
    """Mark a canonical message unsent WITHOUT recovering text (macOS already erased it). No-op if the
    message was never ingested. Never creates a turn."""
    async with worker_session() as s:
        await s.execute(update(schema.ConversationMessage)
            .where(schema.ConversationMessage.user_id == user_id,
                   schema.ConversationMessage.provider == provider,
                   schema.ConversationMessage.provider_message_id == provider_message_id)
            .values(unsent_at=unsent_at or _now()))


async def mark_edited(user_id: UUID, *, provider: str, provider_message_id: str,
                      edited_at: datetime.datetime | None = None) -> None:
    """Record edit state on the canonical message. No duplicate reasoning; never a turn."""
    async with worker_session() as s:
        await s.execute(update(schema.ConversationMessage)
            .where(schema.ConversationMessage.user_id == user_id,
                   schema.ConversationMessage.provider == provider,
                   schema.ConversationMessage.provider_message_id == provider_message_id)
            .values(edited_at=edited_at or _now()))


async def link_attachments(user_id: UUID, message_id: UUID, attachment_ids: list[UUID], *,
                           relationship: str = "attached") -> None:
    """Join a graph message to EXISTING message_attachments rows, preserving order. Idempotent."""
    async with worker_session() as s:
        for i, aid in enumerate(attachment_ids):
            stmt = pg_insert(schema.ConversationMessageAttachment).values(
                user_id=user_id, message_id=message_id, attachment_id=aid,
                relationship=relationship, ordinal=i)
            await s.execute(stmt.on_conflict_do_nothing(constraint="uq_conv_msg_att"))
