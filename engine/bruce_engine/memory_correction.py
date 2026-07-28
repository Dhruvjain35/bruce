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
from .memory_candidate import MemoryCandidate, ProvenanceClass, SubjectType
from .memory_record import MemoryKind, RetentionPolicy, Sensitivity, SourceType
from .memory_writer import MemoryWriter

# Only a trusted correction may retire a belief; everything else is mapped to its honest provenance so
# the writer refuses it for the stated reason rather than for a generic one.
_PROVENANCE_FOR = {
    "quoted": ProvenanceClass.quoted_content,
    "forwarded": ProvenanceClass.forwarded_content,
    "attachment": ProvenanceClass.attachment_content,
    "provider": ProvenanceClass.provider_verified,
    "model": ProvenanceClass.model_inference,
}

_SUBJECT_TYPE = {
    "profile": SubjectType.user, "relationships": SubjectType.relationship,
    "world": SubjectType.world, "entity": SubjectType.provider_entity,
    "episodic": SubjectType.conversation, "style": SubjectType.style,
}
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
        # Checked here AND independently by the writer, which refuses any `supersedes` whose provenance
        # is not `trusted_user_correction`. Two checks because they answer to different callers: this one
        # keeps a stable, specific reason for the conversation layer, and the writer's is what holds if a
        # future path reaches it without coming through here.
        log.info("correction_refused reason=%s source=%s", UNTRUSTED, source_type)
        return CorrectionResult(False, UNTRUSTED)

    now = now or datetime.now(timezone.utc)
    R = schema.MemoryRecordRow
    tid = UUID(str(target_id))

    async with user_session(user_id) as s:
        old = (await s.execute(select(R).where(R.memory_id == tid,
                                               R.user_id == user_id))).scalar_one_or_none()
    if old is None or old.status != "active":
        return CorrectionResult(False, NOT_FOUND)

    # THE CANDIDATE. A correction is a memory write like any other, so it is proposed rather than
    # performed: the writer applies the same policy, the same provenance validation and the same
    # per-kind confidence floor it applies to a first-time fact. Before #124A this module inserted its
    # own row, which meant an untrusted "correction" bypassed every one of those checks — and made
    # "only MemoryWriter creates active memory" a claim the AST proof had to name two modules to state.
    provenance = (ProvenanceClass.trusted_user_correction if source_type == "trusted_user_text"
                  else _PROVENANCE_FOR.get(source_type, ProvenanceClass.model_inference))
    candidate = MemoryCandidate(
        user_id=user_id, subject_type=_SUBJECT_TYPE.get(old.kind, SubjectType.user),
        subject_id=old.subject or mr.SELF, kind=MemoryKind(old.kind), predicate=old.predicate or "",
        proposed_value=new_value, normalized_value=mr.normalize(new_value)[:300],
        evidence_text=evidence_text or new_value, source_type=SourceType(source_type),
        source_id=source_message_id, provenance_class=provenance,
        explicitly_stated_by_user=(source_type == "trusted_user_text"), inferred=False,
        confidence=confidence, expected_stability=RetentionPolicy(old.retention_policy),
        usefulness_reason=old.reason_it_matters or "the student corrected this",
        sensitivity_class=Sensitivity(old.sensitivity),
        retention_recommendation=RetentionPolicy(old.retention_policy), observed_at=now)

    receipt = await MemoryWriter(user_id=user_id).evaluate(candidate, supersedes=tid)
    if not receipt.stored:
        log.info("correction_refused reason=%s predicate=%s", receipt.reason, old.predicate)
        return CorrectionResult(False, receipt.reason, superseded_memory_id=str(tid))

    # The audit row is a DIFFERENT table, and coordinating it is what this module still owns. It records
    # that a correction happened and links both sides; it never carries either value.
    async with user_session(user_id) as s:
        s.add(schema.MemoryCorrectionRow(
            user_id=user_id, memory_id=tid, replacement_memory_id=UUID(str(receipt.memory_id)),
            source_message_id=source_message_id, reason=(reason or "")[:200] or None,
            corrected_at=now))

    memory_cache.invalidate(user_id, reason="correction")
    log.info("correction_applied old=%s new=%s predicate=%s", tid, receipt.memory_id, old.predicate)
    return CorrectionResult(True, APPLIED, superseded_memory_id=str(tid),
                            replacement_memory_id=str(receipt.memory_id))


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
