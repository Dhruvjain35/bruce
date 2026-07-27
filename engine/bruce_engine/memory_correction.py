"""Correcting and forgetting — the two things a student must be able to do to a memory, and the only two
operations permitted to touch a row after it is written.

CORRECTION NEVER EDITS. A repair writes a NEW record and closes the old one out by pointing at it.
Overwriting would be smaller code and it would destroy the only thing that makes a wrong memory
recoverable: if Bruce acted on a fact and the student later says "no, it's Ms. Nguyen", the question that
matters is what Bruce believed AT THE TIME it acted. An overwrite answers "Ms. Nguyen" and the email that
went to the wrong teacher becomes inexplicable. The table enforces this rather than trusting this module:
`0028_typed_memory` installs a BEFORE UPDATE trigger that permits changes to exactly four lifecycle
columns, so raw SQL, a future module and a migration author all hit the same wall.

FORGETTING IS A REDACTING TOMBSTONE — the row survives, the content does not. Both alternatives are worse
in a specific way:

  * A HARD DELETE loses the ability to STAY forgotten. The message the memory came from is still in the
    student's inbox, still in `inbound_messages`, still re-processable by intake. Delete the row and the
    next pass over that material re-derives the same fact and silently serves it again. A student who
    says "forget that" does not mean "forget it until you read that message again" — they mean it is
    gone. Keeping a content-free tombstone keyed by `source_message_id` is what lets `memory_writer.
    remember` refuse the re-learn. It also, deliberately, does NOT block them from teaching Bruce the
    same thing again in a NEW message: saying it again is them choosing to.
  * AN ORDINARY TOMBSTONE that keeps `value` and merely sets a flag is not forgetting, it is hiding, and
    it is one dropped WHERE clause from being undone. So the same statement that sets `forgotten_at`
    NULLs subject, predicate, value, evidence, the reason and the derived index keys, and
    `ck_memory_forgotten_is_redacted` makes a forgotten-but-still-populated row impossible to store at
    all. What survives is the content-free lineage — id, user, kind, source message id, timestamps —
    which is exactly what `retention.py` keeps after erasing a source's raw text. The precedent is
    deliberate: this codebase already has one answer to "delete the content, keep the fact that there was
    content", and having two would mean neither is trusted.

The default scope is the CLAIM, not the row. "forget that ms delgado is my teacher" is about a belief;
leaving three superseded rows carrying the same value behind would technically satisfy a per-row forget
and obviously fail the student.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID

import sqlalchemy as sa

from . import memory_record as mr
from . import memory_writer
from .db import user_session
from .memory_record import MEMORY_RECORDS, Basis, MemoryRecord
from .memory_retrieval import MemoryRetriever

log = logging.getLogger("bruce.memory")   # CONTENT-FREE: ids, scopes, counts — never the forgotten value


class CorrectionError(Exception):
    """A repair that could never be legitimate — a target that does not exist, or belongs elsewhere."""


class ForgetScope(str, Enum):
    claim = "claim"
    """Everything Bruce believes about this subject and relation, including superseded history. The
    default, because that is what "forget that" means."""

    record = "record"
    """This row alone. For a student editing one entry in a memory list, where the surrounding history is
    still theirs to keep."""

    source = "source"
    """Everything learned from one message. For "delete what you got from that email"."""


# --- correction ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryCorrection:
    """The student saying Bruce got it wrong. Writes a replacement and closes out the original.

    A correction is subject to the same write policy as any other memory — it needs the student's own
    words and the span they are in. A correction that could skip the policy would be the easiest way to
    get an ungrounded fact into the store: claim to be fixing something.
    """

    user_id: UUID
    target_memory_id: UUID
    new_value: str
    trusted_text: str
    stated_span: str
    source_message_id: str | None = None
    observed_at: datetime | None = None
    contradicts: bool = True
    """True when the new value REPLACES the old one ("no, it's Ms. Nguyen"). False when it refines a
    still-true statement ("she's actually the AP teacher too"). Both supersede; only a contradiction sets
    `contradicted_by`, which is what tells provenance whether the old belief was wrong or just partial."""

    async def apply(self, *, now: datetime | None = None) -> MemoryRecord:
        now = now or datetime.now(timezone.utc)
        target = await MemoryRetriever(self.user_id).record(self.target_memory_id)
        if target is None:
            # Also the answer for a forgotten target: there is nothing left to correct, and re-creating
            # it under the guise of a correction would walk straight around the forget.
            raise CorrectionError(f"no live memory {self.target_memory_id} for this user")

        proposal = memory_writer.MemoryProposal(
            user_id=self.user_id, kind=target.kind, subject=target.subject, predicate=target.predicate,
            value=self.new_value,
            reason_it_matters=f"corrects {target.memory_id}",
            trusted_text=self.trusted_text, stated_span=self.stated_span,
            source_message_id=self.source_message_id, observed_at=self.observed_at or now,
            retention_policy=target.retention_policy, sensitivity=target.sensitivity,
            user_editable=target.user_editable)
        replacement = memory_writer.build(proposal, basis=Basis.corrected, now=now)

        async with user_session(self.user_id) as s:
            await s.execute(sa.insert(MEMORY_RECORDS).values(
                **mr.to_row(replacement), reason_it_matters=proposal.reason_it_matters,
                created_at=sa.func.now()))
            # The ONLY columns this statement may touch. The trigger rejects anything else, so a future
            # edit here that tried to fix the value in place would fail loudly instead of quietly
            # rewriting what Bruce believed when it acted.
            await s.execute(sa.update(MEMORY_RECORDS).where(
                MEMORY_RECORDS.c.id == target.memory_id,
                MEMORY_RECORDS.c.user_id == self.user_id,
                MEMORY_RECORDS.c.superseded_by.is_(None)).values(
                superseded_by=replacement.memory_id,
                contradicted_by=replacement.memory_id if self.contradicts else None))
        log.info("memory_corrected old=%s new=%s contradicts=%s", target.memory_id,
                 replacement.memory_id, self.contradicts)
        return replacement


async def confirm(user_id: UUID, memory_id: UUID, *, at: datetime | None = None) -> bool:
    """The student said it again. Resets the freshness clock without writing a duplicate row.

    Without this, a fact repeated every month either accumulates a row per mention or ages out while the
    student is actively telling Bruce it is still true. `last_confirmed_at` is one of the four columns the
    append-only trigger permits, for exactly this.
    """
    async with user_session(user_id) as s:
        result = await s.execute(sa.update(MEMORY_RECORDS).where(
            MEMORY_RECORDS.c.id == memory_id, MEMORY_RECORDS.c.user_id == user_id,
            MEMORY_RECORDS.c.forgotten_at.is_(None)).values(
            last_confirmed_at=at or datetime.now(timezone.utc)))
    return result.rowcount > 0


# --- forgetting ------------------------------------------------------------------------------------

_REDACTED = {
    "subject": None, "predicate": None, "value": None,
    # sa.null(), not None: a Python None bound to a JSONB column is the JSON literal `null`, which is a
    # VALUE and not an absence. The append-only trigger caught it, which is the point of having the
    # trigger — but a redaction that leaves `'null'::jsonb` behind would otherwise have looked erased.
    "evidence": sa.null(),
    "reason_it_matters": None, "entity_key": None, "domain": None,
}
"""Every column that can carry content. Listed once, applied in the same statement that sets
`forgotten_at`, and checked by `ck_memory_forgotten_is_redacted` — so a forgotten row that still says
anything cannot be committed even by a caller that skipped this module."""


@dataclass(frozen=True)
class MemoryForget:
    """"Forget that." Permanent, and irreversible by design — there is no un-forget, because a feature
    that restores a memory the student erased is a feature that makes "forget that" untrue."""

    user_id: UUID
    scope: ForgetScope = ForgetScope.claim
    memory_id: UUID | None = None
    source_message_id: str | None = None

    async def apply(self, *, now: datetime | None = None) -> int:
        """Redact everything in scope. Returns the number of rows erased; idempotent, because the
        statement only touches rows that are not already forgotten."""
        now = now or datetime.now(timezone.utc)
        where = [MEMORY_RECORDS.c.user_id == self.user_id, MEMORY_RECORDS.c.forgotten_at.is_(None)]

        if self.scope is ForgetScope.source:
            if not self.source_message_id:
                raise CorrectionError("forgetting a source needs the source_message_id")
            where.append(MEMORY_RECORDS.c.source_message_id == self.source_message_id)
        else:
            if self.memory_id is None:
                raise CorrectionError("forgetting a record or a claim needs the memory_id")
            target = await MemoryRetriever(self.user_id).record(self.memory_id)
            if target is None:
                return 0
            if self.scope is ForgetScope.record:
                where.append(MEMORY_RECORDS.c.id == self.memory_id)
            else:
                # The whole belief, superseded history included. Matched on the derived index keys rather
                # than on the supersession chain: a chain walk misses a row whose link was never set, and
                # a row carrying the same claim that is invisible to a forget is the exact failure this
                # scope exists to prevent.
                where.append(MEMORY_RECORDS.c.entity_key == target.entity_key)
                where.append(MEMORY_RECORDS.c.predicate == target.predicate)

        async with user_session(self.user_id) as s:
            result = await s.execute(sa.update(MEMORY_RECORDS).where(sa.and_(*where)).values(
                forgotten_at=now, forget_scope=self.scope.value, **_REDACTED))
        log.info("memory_forgotten scope=%s rows=%d", self.scope.value, result.rowcount)
        return result.rowcount
