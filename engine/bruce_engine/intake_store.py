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
from .models import ExtractedIntake, IntakeSourceKind, MissionPhase

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


# =================================================================================================
# ASYNC INTAKE — two-phase durable write (see docs + intake_jobs.py)
#
# Phase 1 (create_pending_intake): a SHORT transaction commits source(pending) + mission(understanding)
#   + phase event + intake_job(pending). No model call, no transaction held over the network — the
#   request returns 202 immediately.
# Phase 2 (complete_intake_extraction): the worker runs the existing extraction service OUTSIDE any
#   transaction, then persists spans/tasks + advances the mission in one user-scoped transaction.
#   Idempotent: if the content already landed (a reclaimed job), it advances the mission and returns
#   without duplicating anything.
# =================================================================================================


class PendingIntake(BaseModel):
    """What phase 1 durably created. The request returns these ids immediately (202)."""

    source_id: UUID
    mission_id: UUID
    job_id: UUID
    state: str  # canonical mission phase, e.g. "understanding"
    display_status: str
    replayed: bool = False  # True => idempotent retry; nothing new was written


_INTAKE_PHASE_STATUS = {
    MissionPhase.understanding: {
        IntakeSourceKind.image: "Understanding your flyer…",
        IntakeSourceKind.pdf: "Reading your document…",
    },
    MissionPhase.extracting: "Reading the details…",
    MissionPhase.awaiting_approval: "Ready — review what I found",
    MissionPhase.blocked: "Hit a snag — will retry",
    MissionPhase.failed: "Couldn't read that one",
}


def understanding_status(source_kind: IntakeSourceKind) -> str:
    by_kind = _INTAKE_PHASE_STATUS[MissionPhase.understanding]
    return by_kind.get(source_kind, "Understanding what you sent…")


def phase_status(phase: MissionPhase, source_kind: IntakeSourceKind = IntakeSourceKind.text) -> str:
    if phase is MissionPhase.understanding:
        return understanding_status(source_kind)
    return _INTAKE_PHASE_STATUS.get(phase, "Working…")


def derive_key_bytes(data: bytes, source_kind: IntakeSourceKind) -> str:
    digest = hashlib.sha256(source_kind.value.encode() + b"\n" + (data or b"")).hexdigest()
    return f"intake:{digest}"


async def _advance_mission(s, mission_id: UUID, user_id: UUID, phase: MissionPhase, short_status: str,
                           *, error: str | None = None) -> None:
    """Update the mission's phase + append a durable phase event, in the caller's transaction.

    Phase events are the ordered, append-only log the client polls; the mission row is the current
    snapshot. Written together so a reader never sees a phase the log doesn't record.
    """
    mission = (
        await s.execute(
            select(schema.Mission).where(schema.Mission.id == mission_id, schema.Mission.user_id == user_id)
        )
    ).scalar_one_or_none()
    if mission is None:  # pragma: no cover — caller owns the mission it just created
        raise RuntimeError("mission not visible to its owner")
    mission.phase = phase.value
    mission.short_status = short_status
    mission.version = (mission.version or 1) + 1
    if error is not None:
        mission.error = error
    if phase in (MissionPhase.succeeded,):
        mission.status = "succeeded"
    elif phase is MissionPhase.failed:
        mission.status = "failed"
    s.add(schema.MissionPhaseEvent(user_id=user_id, mission_id=mission_id, phase=phase.value, short_status=short_status))


async def create_pending_intake(
    *,
    user_id: UUID,
    source_kind: IntakeSourceKind,
    text: str | None = None,
    input_bytes: bytes | None = None,
    mime: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    now: datetime.datetime | None = None,
) -> PendingIntake:
    """Phase 1: durably record the intake and return immediately. NO model call here.

    Idempotent: a retry with the same key (or same content) returns the original mission/source/job
    without creating anything new — a double-tap in the app can never spawn a second mission.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if text is not None:
        key = (idempotency_key or derive_key(text, source_kind))[:MAX_CLIENT_KEY]
        sha = content_sha256(text)
    else:
        key = (idempotency_key or derive_key_bytes(input_bytes or b"", source_kind))[:MAX_CLIENT_KEY]
        sha = hashlib.sha256(input_bytes or b"").hexdigest()

    display = understanding_status(source_kind)

    async with user_session(user_id) as s:
        existing = (
            await s.execute(
                select(schema.Source).where(schema.Source.user_id == user_id, schema.Source.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return await _load_pending(s, user_id, key)

        source = schema.Source(
            user_id=user_id, kind=source_kind.value, content_sha256=sha,
            raw_text=text,  # image/pdf: NULL until the worker transcribes it
            expires_at=retention.expires_at_for(now), idempotency_key=key,
        )
        s.add(source)
        try:
            await s.flush()  # assign source.id; UNIQUE(user_id, key) rejects a concurrent duplicate
        except IntegrityError:
            await s.rollback()
            async with user_session(user_id) as s2:
                return await _load_pending(s2, user_id, key)
        mission = schema.Mission(
            user_id=user_id, kind="intake", status="running",
            phase=MissionPhase.understanding.value, short_status=display,
            goal={"intent": "intake", "source_kind": source_kind.value, "source_id": str(source.id)},
            idempotency_key=key,
        )
        s.add(mission)
        await s.flush()  # assign mission.id (source's unique key already won the race above)

        s.add(schema.MissionPhaseEvent(
            user_id=user_id, mission_id=mission.id, phase=MissionPhase.understanding.value, short_status=display,
        ))
        job = schema.IntakeJob(
            user_id=user_id, source_id=source.id, mission_id=mission.id,
            status="pending", source_kind=source_kind.value, mime=mime,
            input_text=text, input_bytes=input_bytes, max_attempts=max_attempts, idempotency_key=key,
        )
        s.add(job)
        await s.flush()
        return PendingIntake(
            source_id=source.id, mission_id=mission.id, job_id=job.id,
            state=MissionPhase.understanding.value, display_status=display, replayed=False,
        )


async def _load_pending(s, user_id: UUID, key: str) -> PendingIntake:
    """Rebuild the PendingIntake for an idempotent retry from the already-committed rows."""
    source = (await s.execute(
        select(schema.Source).where(schema.Source.user_id == user_id, schema.Source.idempotency_key == key)
    )).scalar_one()
    mission = (await s.execute(
        select(schema.Mission).where(schema.Mission.user_id == user_id, schema.Mission.idempotency_key == key)
    )).scalar_one()
    job = (await s.execute(
        select(schema.IntakeJob).where(schema.IntakeJob.user_id == user_id, schema.IntakeJob.idempotency_key == key)
    )).scalar_one()
    return PendingIntake(
        source_id=source.id, mission_id=mission.id, job_id=job.id,
        state=mission.phase, display_status=mission.short_status, replayed=True,
    )


def _write_spans_and_tasks(s, source, intake: ExtractedIntake, source_key: str, user_id: UUID):
    """Shared span+task writer (same shape as persist_intake steps 3-5). Returns (spans, task_rows)."""
    span_for_deadline: list[schema.SourceSpan] = []
    for i, deadline in enumerate(intake.deadlines):
        span = schema.SourceSpan(user_id=user_id, source_id=source.id, span_text=deadline.source_span, ordinal=i)
        s.add(span)
        span_for_deadline.append(span)
    derived = tasks_mod.intake_to_tasks(intake, source=str(source.id))
    task_rows: list[schema.TaskRow] = []
    for i, task in enumerate(derived):
        span = span_for_deadline[i] if i < len(span_for_deadline) else None
        row = schema.TaskRow(
            user_id=user_id, source_id=source.id, span_id=span.id if span is not None else None,
            kind=task.kind.value, title=task.title, course_or_org=task.course_or_org, due=task.due,
            status=task.status.value, workload_minutes=task.workload_minutes,
            required_items=[item.model_dump(mode="json") for item in task.required_items],
            notes=task.notes, idempotency_key=_task_key(source_key, i),
        )
        s.add(row)
        task_rows.append(row)
    return span_for_deadline, task_rows


async def complete_intake_extraction(
    *, user_id: UUID, source_id: UUID, mission_id: UUID, source_key: str,
    intake: ExtractedIntake, transcript: str | None = None,
) -> PersistedIntake:
    """Phase 2: persist an already-computed extraction and advance the mission to awaiting_approval.

    IDEMPOTENT: if the source already has an extraction stored (a reclaimed job whose content landed
    before the worker recorded completion), it does NOT rewrite spans/tasks — it just ensures the
    mission is at awaiting_approval and returns the existing ids. This is what makes a worker crash
    between "content committed" and "job marked completed" safe.
    """
    async with user_session(user_id) as s:
        source = (await s.execute(
            select(schema.Source).where(schema.Source.id == source_id, schema.Source.user_id == user_id)
        )).scalar_one()

        if source.extracted is not None:  # already persisted — idempotent no-op on content
            persisted = await _load_persisted(s, source)
            await _advance_mission(s, mission_id, user_id, MissionPhase.awaiting_approval,
                                   phase_status(MissionPhase.awaiting_approval, IntakeSourceKind(source.kind)))
            return persisted

        if transcript is not None and source.raw_text is None:
            source.raw_text = transcript  # image/pdf: store what vision read, for grounding provenance
        spans, tasks = _write_spans_and_tasks(s, source, intake, source_key, user_id)
        source.extracted = intake.model_dump(mode="json")
        await _advance_mission(s, mission_id, user_id, MissionPhase.awaiting_approval,
                               phase_status(MissionPhase.awaiting_approval, IntakeSourceKind(source.kind)))
        await s.flush()
        return PersistedIntake(
            intake=intake, source_id=source.id,
            span_ids=[sp.id for sp in spans], task_ids=[t.id for t in tasks], replayed=False,
        )


async def advance_intake_phase(*, user_id: UUID, mission_id: UUID, phase: MissionPhase,
                               source_kind: IntakeSourceKind = IntakeSourceKind.text) -> None:
    """Move an intake mission to a non-terminal phase (e.g. extracting) + append the phase event."""
    async with user_session(user_id) as s:
        await _advance_mission(s, mission_id, user_id, phase, phase_status(phase, source_kind))


async def fail_intake_mission(*, user_id: UUID, mission_id: UUID, phase: MissionPhase, reason: str,
                              source_kind: IntakeSourceKind = IntakeSourceKind.text) -> None:
    """Move a mission to blocked (recoverable) or failed (terminal). reason is a TYPE/short cause,
    never student content — it is stored on the mission and shown to the user."""
    async with user_session(user_id) as s:
        await _advance_mission(s, mission_id, user_id, phase, phase_status(phase, source_kind), error=reason[:200])
