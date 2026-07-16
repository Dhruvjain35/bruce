"""Durable intake: turn a raw student paste/PDF/email into persisted source -> spans -> tasks.

This is the seam between the (previously stateless) Phase-1 compute path and the RLS-enforced
persistence layer. One call, one transaction, one user context.

WHAT IT GUARANTEES

  * Tenant isolation — every write happens inside ``db.user_session(user_id)`` as the restricted
    ``bruce_app`` role, so Postgres RLS enforces ownership independently of this code. user_id comes
    only from the caller's verified JWT; it is never taken from request input.
  * Atomicity — source, spans and tasks are written in ONE transaction. A failed extraction (or any
    error mid-write) rolls the whole thing back: no orphan source, no partial task set. Callers can
    never observe a source that has no spans/tasks yet, which would read as "Bruce lost your work".
  * Idempotency — a retry of the same intake creates nothing new and returns the ORIGINAL ids and
    the ORIGINAL extraction. Enforced by the DB (UNIQUE user_id+idempotency_key on sources), not by
    a check-then-insert race: concurrent duplicates both INSERT, one wins, the loser reads the
    winner's row. Tasks carry deterministic keys derived from the source key for the same reason.
  * Lineage — every task points at the source AND at the exact span it was grounded in
    (source -> span -> task). Deadlines are grounded; an umbrella application task (emitted when
    there are required items but no deadline) has no span and honestly stores span_id = NULL.
  * Retention — expires_at is stamped from retention.expires_at_for(), the single policy home.

DESIGN NOTE (deliberate, has a cost): the transaction is held open ACROSS the extraction call,
because the source row must be persisted before computation while a failed extraction must still
leave nothing behind. That means a DB connection is held for the duration of an LLM call (seconds).
Two measured consequences, both accepted at current scale:
  * a connection is occupied per in-flight intake;
  * two CONCURRENT identical intakes serialise — the loser blocks on the unique index until the
    winner's transaction commits (i.e. for the length of the winner's extraction) before it gets
    its IntegrityError and replays. Correct, but it is a lock wait pinned to an LLM call.
If intake volume ever makes this the bottleneck, the fix is a two-phase write (source committed as
'pending', spans/tasks committed after extraction, a sweeper reaping pending sources) — NOT quietly
dropping the atomicity guarantee.

PRIVACY: nothing here logs, prints or returns raw content on the error path — callers surface the
exception TYPE only. raw_text is written once and thereafter owned by the retention sweep.
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import retention, schema
from . import tasks as tasks_mod
from .db import user_session
from .models import ExtractedIntake, IntakeSourceKind

# Client-supplied keys are bounded so the derived per-task key ("<source key>:t<n>") still fits
# tasks.idempotency_key (String(128)).
MAX_CLIENT_KEY = 100


class PersistedIntake(BaseModel):
    """What a durable intake produced. Ids are stable and fetchable by the owning user only."""

    intake: ExtractedIntake
    source_id: UUID
    span_ids: list[UUID]
    task_ids: list[UUID]
    replayed: bool = False  # True => idempotent retry; nothing new was written


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_key(text: str, source_kind: IntakeSourceKind) -> str:
    """Deterministic idempotency key for an unkeyed intake.

    Same user + same kind + same bytes == same intake, so a naive client retry (or a double-tap in
    the app) is idempotent WITHOUT the client having to supply a key. Kind is in the hash because
    the same bytes read as a PDF vs an email are genuinely different intakes.
    """
    digest = hashlib.sha256(f"{source_kind.value}\n{text}".encode("utf-8")).hexdigest()
    return f"intake:{digest}"


def _task_key(source_key: str, ordinal: int) -> str:
    """Deterministic per-task key. Zero-padded so lexicographic order == creation order: all tasks
    from one intake share a transaction timestamp, so created_at cannot order them and id is random.
    """
    return f"{source_key}:t{ordinal:03d}"


async def _load_persisted(s, source_row) -> PersistedIntake:
    """Rebuild the original response from already-persisted rows (the idempotent-retry path)."""
    # Order by the EXPLICIT ordinal / padded key, never by created_at+id: every row of one intake
    # shares the transaction timestamp, so those tie and the id tiebreak is random — a replay would
    # hand back the same ids in a different order, and span_ids[i] would stop matching deadlines[i].
    spans = (
        (
            await s.execute(
                select(schema.SourceSpan)
                .where(schema.SourceSpan.source_id == source_row.id)
                .order_by(schema.SourceSpan.ordinal)
            )
        )
        .scalars()
        .all()
    )
    task_rows = (
        (
            await s.execute(
                select(schema.TaskRow)
                .where(schema.TaskRow.source_id == source_row.id)
                .order_by(schema.TaskRow.idempotency_key)
            )
        )
        .scalars()
        .all()
    )
    # extracted is the extraction we returned the first time; replay it rather than re-running a
    # nondeterministic LLM whose new answer could contradict the spans/tasks stored above.
    intake = (
        ExtractedIntake.model_validate(source_row.extracted)
        if source_row.extracted
        else ExtractedIntake(source_kind=IntakeSourceKind(source_row.kind))
    )
    return PersistedIntake(
        intake=intake,
        source_id=source_row.id,
        span_ids=[sp.id for sp in spans],
        task_ids=[t.id for t in task_rows],
        replayed=True,
    )


async def persist_intake(
    *,
    user_id: UUID,
    text: str,
    source_kind: IntakeSourceKind,
    extract: Callable[[str, IntakeSourceKind], Awaitable[ExtractedIntake]],
    idempotency_key: str | None = None,
    now: datetime.datetime | None = None,
) -> PersistedIntake:
    """Persist one intake and everything grounded in it. See module docstring for guarantees.

    ``extract`` is injected so tests can drive success/failure deterministically without an LLM.
    Raises whatever ``extract`` raises, after rolling back — the caller must not leak its message.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    source_key = (idempotency_key or derive_key(text, source_kind))[:MAX_CLIENT_KEY]

    async with user_session(user_id) as s:
        # Fast path: this intake already landed. Return the original ids + extraction, write nothing.
        existing = (
            await s.execute(
                select(schema.Source).where(
                    schema.Source.user_id == user_id,
                    schema.Source.idempotency_key == source_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return await _load_persisted(s, existing)

        # 1. The source lands BEFORE computation — but inside this transaction, so a failed
        #    extraction below rolls it back and leaves no orphan.
        source = schema.Source(
            user_id=user_id,
            kind=source_kind.value,
            content_sha256=content_sha256(text),
            raw_text=text,
            expires_at=retention.expires_at_for(now),
            idempotency_key=source_key,
        )
        s.add(source)
        try:
            await s.flush()
        except IntegrityError:
            # Lost an insert race against a concurrent identical intake. The winner's row is
            # authoritative: roll back our failed insert, then read it back in a fresh context.
            await s.rollback()
            return await _replay_after_race(user_id, source_key)

        # 2. Extraction. Anything raised here aborts the whole transaction (source included).
        intake = await extract(text, source_kind)

        # 3. Grounding anchors: one span per extracted deadline, holding the verbatim text the
        #    deadline was read out of.
        span_for_deadline: list[schema.SourceSpan] = []
        for i, deadline in enumerate(intake.deadlines):
            span = schema.SourceSpan(
                user_id=user_id, source_id=source.id, span_text=deadline.source_span, ordinal=i
            )
            s.add(span)
            span_for_deadline.append(span)
        await s.flush()  # assign span ids before tasks reference them

        # 4. Canonical tasks, via the same derivation the stateless endpoint already used, so task
        #    content/ordering behaviour is unchanged — only their durability is new.
        derived = tasks_mod.intake_to_tasks(intake, source=str(source.id))
        task_rows: list[schema.TaskRow] = []
        for i, task in enumerate(derived):
            # intake_to_tasks emits one task per deadline IN ORDER, then at most one umbrella
            # application task (only when there are no deadlines) which has no span to point at.
            span = span_for_deadline[i] if i < len(span_for_deadline) else None
            row = schema.TaskRow(
                user_id=user_id,
                source_id=source.id,
                span_id=span.id if span is not None else None,
                kind=task.kind.value,
                title=task.title,
                course_or_org=task.course_or_org,
                due=task.due,
                status=task.status.value,
                workload_minutes=task.workload_minutes,
                required_items=[item.model_dump(mode="json") for item in task.required_items],
                notes=task.notes,
                idempotency_key=_task_key(source_key, i),
            )
            s.add(row)
            task_rows.append(row)

        # 5. Store the extraction for replay on retry (derived+minimized, not the raw blob).
        source.extracted = intake.model_dump(mode="json")
        await s.flush()

        return PersistedIntake(
            intake=intake,
            source_id=source.id,
            span_ids=[sp.id for sp in span_for_deadline],
            task_ids=[t.id for t in task_rows],
            replayed=False,
        )


async def _replay_after_race(user_id: UUID, source_key: str) -> PersistedIntake:
    """Read back the winner of a concurrent-insert race, in a fresh transaction."""
    async with user_session(user_id) as s:
        row = (
            await s.execute(
                select(schema.Source).where(
                    schema.Source.user_id == user_id,
                    schema.Source.idempotency_key == source_key,
                )
            )
        ).scalar_one_or_none()
        if row is None:  # pragma: no cover — the constraint that rejected us guarantees a winner
            raise RuntimeError("intake insert conflicted but no source is visible for this user")
        return await _load_persisted(s, row)
