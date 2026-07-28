"""Forgetting — and the reason it is a redacting tombstone rather than a delete.

THE ROW SURVIVES, THE CONTENT DOES NOT. The same statement that sets `forgotten_at` NULLs the subject,
predicate, value, evidence and reason. `ck_memory_forgotten_redacted` refuses to store a forgotten row
that still has content, so redaction is the database's guarantee and not this module's.

WHY NOT A HARD DELETE. A hard delete loses the ability to STAY forgotten. The message the memory came
from is still in the student's inbox and still in `inbound_messages`, so the next intake pass re-derives
the same fact and serves it back. "Forget that" does not mean "forget it until you read that email
again". The content-free tombstone, keyed by source message, is what lets the writer refuse the re-learn
— and deliberately does NOT stop the student re-teaching it in a NEW message, because saying it again is
them choosing to.

WHY NOT A FLAG THAT KEEPS THE VALUE. That is hiding, not forgetting: one dropped WHERE clause from being
undone, and the value is still sitting in the database the whole time.

BROAD FORGETS ARE PREVIEWED, NOT GUESSED. "forget everything about my coach" can reach a dozen records
across four kinds, and the student cannot see what they are about to lose. `preview` returns the count
and the shape without the content, so the caller can say what will go before it goes. There is no
un-forget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import null, select, update

from . import memory_cache
from . import memory_record as mr
from . import schema
from .db import user_session

log = logging.getLogger("bruce.memory.forget")   # CONTENT-FREE: scopes, counts — never what was forgotten

FACT, SUBJECT, KIND, SOURCE = "fact", "subject", "kind", "source"
SCOPES = (FACT, SUBJECT, KIND, SOURCE)

# Above this, a forget is broad enough that a student should see what it covers first. Not a safety
# limit — nothing here is unsafe — but "forget everything about my coach" removing a deadline they still
# needed is a real way to lose someone's trust in one message.
PREVIEW_THRESHOLD = 3

# `value_json` uses SQL NULL explicitly. Assigning Python None to a JSONB column produces the JSON value
# `null`, which is not SQL NULL — the column reads as populated, the redaction CHECK passes it, and the
# append-only trigger rejects the whole forget with "forgetting must erase content". Measured: every
# forget failed until this line was `null()`.
_CONTENT_COLUMNS = dict(
    subject=None, predicate=None, value_json=null(), normalized_value=None, evidence_text=None,
    reason_it_matters=None, entity_key=None)


@dataclass(frozen=True)
class ForgetPreview:
    scope: str
    target: str | None
    count: int
    kinds: tuple[str, ...]
    needs_confirmation: bool

    def summary(self) -> str:
        """Content-free by construction — how many and of what kind, never what they say."""
        if not self.count:
            return "nothing"
        kinds = ", ".join(self.kinds)
        return f"{self.count} thing{'s' if self.count != 1 else ''} ({kinds})"


@dataclass(frozen=True)
class ForgetResult:
    scope: str
    target: str | None
    forgotten: int


def _scope_clause(R, *, scope: str, target: str | None):
    if scope == FACT:
        return R.memory_id == UUID(str(target))
    if scope == SUBJECT:
        return R.entity_key == mr.entity_key(target or "")
    if scope == KIND:
        return R.kind == target
    if scope == SOURCE:
        return R.source_message_id == target
    raise ValueError(f"unknown forget scope: {scope}")


async def preview(user_id: UUID, *, scope: str, target: str | None) -> ForgetPreview:
    """What a forget would reach, without touching anything."""
    R = schema.MemoryRecordRow
    async with user_session(user_id) as s:
        rows = (await s.execute(select(R.kind).where(
            R.user_id == user_id, R.forgotten_at.is_(None), R.status != "forgotten",
            _scope_clause(R, scope=scope, target=target)))).scalars().all()
    kinds = tuple(sorted(set(rows)))
    return ForgetPreview(scope=scope, target=target, count=len(rows), kinds=kinds,
                         needs_confirmation=scope != FACT and len(rows) > PREVIEW_THRESHOLD)


async def forget(user_id: UUID, *, scope: str, target: str | None,
                 source_message_id: str | None = None,
                 now: datetime | None = None) -> ForgetResult:
    """Redact everything in scope, record the tombstone event, and kill every cached context.

    The redaction and the status change are ONE statement. Two statements would leave a window in which
    a row is marked forgotten and still holds its value, and that window is precisely when a concurrent
    retrieval would read it.
    """
    now = now or datetime.now(timezone.utc)
    R = schema.MemoryRecordRow
    async with user_session(user_id) as s:
        result = await s.execute(
            update(R)
            .where(R.user_id == user_id, R.forgotten_at.is_(None),
                   _scope_clause(R, scope=scope, target=target))
            .values(status="forgotten", forgotten_at=now, **_CONTENT_COLUMNS))
        count = int(result.rowcount or 0)
        s.add(schema.MemoryForgetEventRow(
            user_id=user_id, scope=scope, target=(str(target)[:200] if target else None),
            source_message_id=source_message_id, record_count=count, forgotten_at=now))

    # Every cached context for this student becomes unreachable, including ones that OMITTED the
    # forgotten fact for budget reasons and would now select something different. Silence is deliberate:
    # forgetting is not an event worth texting someone about.
    memory_cache.invalidate(user_id, reason="forget")
    log.info("memory_forgotten scope=%s count=%d", scope, count)
    return ForgetResult(scope=scope, target=(str(target) if target else None), forgotten=count)


async def is_forgotten_source(user_id: UUID, source_message_id: str) -> bool:
    """Was a memory from this exact message already forgotten? The re-learn guard.

    Without it, forgetting is undone by the next intake pass over the same inbox, and the student
    experiences Bruce ignoring them.
    """
    F = schema.MemoryForgetEventRow
    async with user_session(user_id) as s:
        found = (await s.execute(select(F.id).where(
            F.user_id == user_id, F.source_message_id == source_message_id).limit(1))).first()
        if found is not None:
            return True
        R = schema.MemoryRecordRow
        tomb = (await s.execute(select(R.memory_id).where(
            R.user_id == user_id, R.source_message_id == source_message_id,
            R.forgotten_at.isnot(None)).limit(1))).first()
    return tomb is not None
