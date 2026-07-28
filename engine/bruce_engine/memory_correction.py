"""Correction — "that's wrong" must change what Bruce believes without destroying what it believed.

CORRECTION NEVER EDITS. A repair writes a NEW record and closes the old one out by pointing at it.
Overwriting would be less code and it would destroy the only thing that makes a wrong memory recoverable:
if Bruce emailed the wrong teacher because it believed "mr smith", and the student later says "it's ms
smith", the question that matters afterwards is what Bruce believed AT THE TIME. An overwrite answers
"ms smith" and the wrong email becomes inexplicable. `memory_records_no_edit` in 0029 enforces this in
the database, so this module is not the only thing standing between a correction and an UPDATE.

NO TWO ACTIVE CONFLICTING FACTS. The invariant that makes retrieval trustworthy: for one student, one
subject and one predicate there is at most one `active` row. It is enforced in the same transaction that
inserts the replacement, not by a later reconciliation, because the window between "new fact written" and
"old fact retired" is exactly when a turn would retrieve both and pick one at random.

THE CORRECTION IS VISIBLE IMMEDIATELY. The cache generation is bumped inside the same call, so the next
retrieval in the same process cannot serve a context built before the correction. That is the failure
worth defending against: a student corrects Bruce and Bruce repeats the old value in the next message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update

from . import memory_cache
from . import memory_record as mr
from . import schema
from .db import user_session

log = logging.getLogger("bruce.memory.correction")   # CONTENT-FREE: ids, predicates, counts


@dataclass(frozen=True)
class CorrectionResult:
    applied: bool
    reason: str
    superseded_memory_id: str | None = None
    replacement_memory_id: str | None = None


NOT_FOUND = "no_matching_memory"
UNTRUSTED = "correction_must_come_from_trusted_user_text"
APPLIED = "applied"


async def find_target(user_id: UUID, *, subject: str | None, predicate: str | None,
                      entity_hint: str | None = None) -> schema.MemoryRecordRow | None:
    """Which record is being corrected.

    Resolution is by IDENTITY, not similarity: the folded subject plus the predicate, optionally narrowed
    by an entity the current conversation already resolved. A fuzzy match here would let "that's wrong"
    retire a fact the student was not talking about, and a student has no way to see that happen.
    """
    R = schema.MemoryRecordRow
    q = select(R).where(R.user_id == user_id, R.status == "active", R.forgotten_at.is_(None))
    if predicate:
        q = q.where(R.predicate == predicate)
    key = mr.entity_key(entity_hint or subject or "")
    if key:
        q = q.where(R.entity_key == key)
    async with user_session(user_id) as s:
        return (await s.execute(q.order_by(R.observed_at.desc()).limit(1))).scalar_one_or_none()


async def apply(user_id: UUID, *, target_id: UUID | str, new_value: str,
                value_json: dict | None = None, source_message_id: str | None,
                source_type: str = "trusted_user_text", evidence_text: str | None = None,
                confidence: float = 1.0, reason: str | None = None,
                now: datetime | None = None) -> CorrectionResult:
    """Retire the old record, write the replacement, and link them — in one transaction.

    `source_type` is checked rather than trusted: only the student's own words may correct a fact about
    them. A forwarded email that contradicts something they told Bruce is EVIDENCE that one of the two is
    wrong, not an instruction about which.
    """
    if source_type != "trusted_user_text":
        log.info("correction_refused reason=%s source=%s", UNTRUSTED, source_type)
        return CorrectionResult(False, UNTRUSTED)

    now = now or datetime.now(timezone.utc)
    R = schema.MemoryRecordRow
    tid = UUID(str(target_id))

    async with user_session(user_id) as s:
        old = (await s.execute(select(R).where(R.memory_id == tid, R.user_id == user_id))).scalar_one_or_none()
        if old is None or old.status != "active":
            return CorrectionResult(False, NOT_FOUND)

        replacement_id = uuid4()
        # THE LINEAGE. The replacement is a new VERSION of the same claim, not a new claim, so it
        # inherits the root and the key. Without this a corrected fact becomes two unrelated rows and a
        # later "forget that" reaches only the newest — leaving the previous value readable through
        # provenance and correction history, which is what #122 shipped.
        root = old.claim_root_id or old.memory_id
        # Backfill the key when the original row predates claim lineage or was written by a path that
        # did not set one. `uq_memory_active_claim` is partial on `claim_key IS NOT NULL`, so a NULL key
        # means the database's active-claim uniqueness guarantee silently does not cover this claim —
        # and a correction is exactly when a second active version would appear.
        key = old.claim_key or mr.claim_key(kind=old.kind, subject=old.subject,
                                            predicate=old.predicate)

        # RETIRE BEFORE INSERTING. Since 0030 the database holds at most one ACTIVE row per claim, so
        # inserting the replacement while the old one is still active is an integrity error rather than
        # a brief window. That ordering is now load-bearing, not stylistic — and it is the same
        # invariant either way: two active versions of one claim never exist, and the transaction makes
        # the pair atomic.
        await s.execute(update(R).where(R.memory_id == tid, R.user_id == user_id)
                        .values(status="superseded", superseded_by_id=replacement_id))
        # Anything ELSE still active for the same claim goes too — how a duplicate that predates the
        # unique index gets cleaned up instead of quietly outliving the correction.
        await s.execute(update(R).where(
            R.user_id == user_id, R.status == "active", R.predicate == old.predicate,
            R.entity_key == old.entity_key, R.memory_id != replacement_id)
            .values(status="superseded", superseded_by_id=replacement_id))

        s.add(R(
            memory_id=replacement_id, user_id=user_id, kind=old.kind, subject=old.subject,
            claim_root_id=root, claim_key=key,
            predicate=old.predicate, value_json=value_json or {"value": new_value},
            normalized_value=mr.normalize(new_value)[:300], evidence_text=evidence_text,
            source_message_id=source_message_id, source_type=source_type, confidence=confidence,
            observed_at=now, last_confirmed_at=now, expires_at=old.expires_at,
            freshness_class="fresh", retention_policy=old.retention_policy,
            sensitivity=old.sensitivity, user_editable=old.user_editable, status="active",
            entity_key=old.entity_key, domain=old.domain, reason_it_matters=old.reason_it_matters))
        await s.flush()

        s.add(schema.MemoryCorrectionRow(
            user_id=user_id, memory_id=tid, replacement_memory_id=replacement_id,
            source_message_id=source_message_id, reason=(reason or "")[:200] or None,
            corrected_at=now))

    memory_cache.invalidate(user_id, reason="correction")
    log.info("correction_applied old=%s new=%s predicate=%s", tid, replacement_id, old.predicate)
    return CorrectionResult(True, APPLIED, superseded_memory_id=str(tid),
                            replacement_memory_id=str(replacement_id))


async def contradict(user_id: UUID, *, target_id: UUID | str, by_memory_id: UUID | str | None = None,
                     now: datetime | None = None) -> bool:
    """Mark a record contradicted when two trusted statements conflict and neither wins.

    Distinct from superseded on purpose: superseded means "replaced by this newer thing", contradicted
    means "Bruce no longer knows". Both leave ordinary retrieval, and only one of them has an answer to
    put in its place — collapsing them would let Bruce present a guess as a correction.
    """
    R = schema.MemoryRecordRow
    async with user_session(user_id) as s:
        result = await s.execute(update(R).where(
            R.memory_id == UUID(str(target_id)), R.user_id == user_id, R.status == "active")
            .values(status="contradicted",
                    contradicted_by_id=UUID(str(by_memory_id)) if by_memory_id else None))
    changed = bool(result.rowcount)
    if changed:
        memory_cache.invalidate(user_id, reason="contradiction")
    return changed


async def active_conflicts(user_id: UUID) -> list[tuple[str, str, int]]:
    """(entity_key, predicate, count) wherever more than one active record exists for the same fact.

    The invariant expressed as a query rather than a comment, so the harness can assert it is empty
    after a correction storm instead of asserting that the code looks right.
    """
    R = schema.MemoryRecordRow
    async with user_session(user_id) as s:
        rows = (await s.execute(select(R.entity_key, R.predicate, schema.func.count())
                                .where(R.user_id == user_id, R.status == "active",
                                       R.forgotten_at.is_(None))
                                .group_by(R.entity_key, R.predicate)
                                .having(schema.func.count() > 1))).all()
    return [(r[0], r[1], r[2]) for r in rows]
