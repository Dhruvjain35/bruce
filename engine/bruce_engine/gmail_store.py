"""Gmail sent-ledger store (Phase G) — the persistence behind Gmail's exactly-once send.

A calendar create is idempotent by a client-set id + the provider's 409; a Gmail send is not (Google assigns
the id, and Gmail can't be searched by our marker header). So the ledger IS the idempotency: before sending we
look the marker up; after a verified send we record (marker -> message_id) under a UNIQUE(user, marker)
constraint. A concurrent double-send loses the race on that constraint and resolves to the winner's message.
Owner-scoped (tenant_isolation) — the same session discipline calendar entities use.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import schema
from .db import user_session


def _to_dict(r: "schema.GmailSentLedger") -> dict:
    return {"marker": r.marker, "message_id": r.message_id, "thread_id": r.thread_id,
            "to_addr": r.to_addr, "subject": r.subject}


async def get_by_marker(user_id: UUID, marker: str) -> dict | None:
    async with user_session(user_id) as s:
        row = (await s.execute(select(schema.GmailSentLedger).where(
            schema.GmailSentLedger.user_id == user_id,
            schema.GmailSentLedger.marker == marker))).scalar_one_or_none()
        return _to_dict(row) if row is not None else None


async def record_sent(user_id: UUID, *, marker: str, message_id: str, thread_id: str | None,
                      to_addr: str | None = None, subject: str | None = None,
                      agent_run_id: UUID | None = None) -> dict:
    """Persist (marker -> message_id) exactly once. If a row for this marker already exists (a retry or a
    concurrent send that won the race), return THAT row's mapping instead of writing a second — so the caller
    always ends up pointing at the one real message."""
    existing = await get_by_marker(user_id, marker)
    if existing is not None:
        return existing
    try:
        async with user_session(user_id) as s:
            s.add(schema.GmailSentLedger(
                user_id=user_id, marker=marker, message_id=message_id, thread_id=thread_id,
                to_addr=to_addr, subject=subject, agent_run_id=agent_run_id))
            await s.flush()
    except IntegrityError:
        # lost the UNIQUE(user, marker) race -> the winner's row is authoritative
        won = await get_by_marker(user_id, marker)
        if won is not None:
            return won
        raise
    return {"marker": marker, "message_id": message_id, "thread_id": thread_id,
            "to_addr": to_addr, "subject": subject}
