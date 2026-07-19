"""Persistence + incremental sync for the SchoolConnector canonical academic graph.

Every read/write happens under ``user_session(user_id)`` so Postgres RLS enforces tenancy on a student's
whole academic record — the store never bypasses the tenant boundary. Two honest layers of change
detection live here:

  1. the CONNECTOR reports created/updated/deleted since a restart-safe cursor (``connector.sync``), and
  2. the STORE re-verifies at persist time by comparing a content hash against the persisted row —

so a provider that under-reports a change is still caught when its payload hash differs. Every upsert
writes a ``SchoolObjectChange`` row (the change history) and refreshes the ``school_sources`` evidence
anchor, so every object read back carries its provider id, URL, timestamps, evidence, and change log.

Provider-neutral: this module imports the canonical contract + schema only. It takes a ``SchoolConnector``
by its Protocol and never names Canvas.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
from uuid import UUID

from sqlalchemy import delete, select

from . import schema
from . import school_connector as sc
from .db import user_session
from .school_capability import CapabilityState

# The three cursor-synced resources. Dimension objects (institution/terms/instructors/materials/
# submissions) are pulled in full each sync — they are small and idempotent, and not every provider
# exposes an incremental feed for them.
SYNC_RESOURCES = ("courses", "assignments", "announcements")
_MODEL_FOR_RESOURCE = {"courses": sc.Course, "assignments": sc.Assignment, "announcements": sc.Announcement}
_OBJECT_TYPE_FOR_RESOURCE = {"courses": "course", "assignments": "assignment", "announcements": "announcement"}


@dataclasses.dataclass
class SyncSummary:
    """What one ``sync_provider`` run did — created/updated/deleted counts per object type + cursors."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    unsupported: list[str] = dataclasses.field(default_factory=list)  # capabilities the provider declined
    cursors: dict[str, str | None] = dataclasses.field(default_factory=dict)


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _capstate(prov: sc.Provenance) -> str:
    cs = prov.capability_state
    return cs.value if isinstance(cs, CapabilityState) else str(cs)


def _strip_provenance(value):
    """Recursively drop every ``provenance`` block from a dumped object.

    Provenance is bookkeeping (ids, URLs, and — crucially — the volatile ``last_synced_at`` clock), not
    content. It must NOT enter the change hash: otherwise a folded sub-object's ``last_synced_at`` (a
    course's instructors, a submission's rubric/feedback) would move on every sync and mark the parent
    'updated' every time, even when nothing real changed."""
    if isinstance(value, dict):
        return {k: _strip_provenance(v) for k, v in value.items() if k != "provenance"}
    if isinstance(value, list):
        return [_strip_provenance(v) for v in value]
    return value


def _payload(obj) -> dict:
    """Minimized normalized payload for the evidence anchor + change hash: content only, no provenance
    at any depth (so the hash tracks real content changes, not sync-clock churn)."""
    return _strip_provenance(obj.model_dump(mode="json", exclude={"provenance"}))


# ---------------------------------------------------------------------------------------------------
# evidence anchor (school_sources + school_source_spans)
# ---------------------------------------------------------------------------------------------------


async def _upsert_source(s, user_id: UUID, *, object_type: str, prov: sc.Provenance,
                         payload: dict, content_hash: str) -> UUID:
    row = (await s.execute(select(schema.SchoolSource).where(
        schema.SchoolSource.user_id == user_id, schema.SchoolSource.provider == prov.provider,
        schema.SchoolSource.object_type == object_type,
        schema.SchoolSource.provider_id == prov.provider_id))).scalar_one_or_none()
    if row is None:
        row = schema.SchoolSource(
            user_id=user_id, provider=prov.provider, object_type=object_type, provider_id=prov.provider_id,
            source_url=prov.source_url, source_timestamp=prov.source_timestamp,
            last_synced_at=prov.last_synced_at, content_hash=content_hash, capability_state=_capstate(prov),
            payload=payload)
        s.add(row)
        await s.flush()
    else:
        row.source_url = prov.source_url
        row.source_timestamp = prov.source_timestamp
        row.last_synced_at = prov.last_synced_at
        row.content_hash = content_hash
        row.capability_state = _capstate(prov)
        row.payload = payload
    # capture a verbatim grounding span once (the "source_span" object), if the connector gave one
    span_text = prov.evidence.span_text if prov.evidence else None
    if span_text:
        exists = (await s.execute(select(schema.SchoolSourceSpan.id).where(
            schema.SchoolSourceSpan.source_id == row.id,
            schema.SchoolSourceSpan.span_text == span_text))).scalar_one_or_none()
        if exists is None:
            s.add(schema.SchoolSourceSpan(user_id=user_id, source_id=row.id, span_text=span_text, ordinal=0))
    return row.id


def _prov_cols(prov: sc.Provenance, content_hash: str, source_id: UUID) -> dict:
    return dict(
        provider=prov.provider, provider_id=prov.provider_id, source_url=prov.source_url,
        source_timestamp=prov.source_timestamp, last_synced_at=prov.last_synced_at,
        capability_state=_capstate(prov), content_hash=content_hash, source_id=source_id)


async def _upsert(s, user_id: UUID, model, object_type: str, obj, cols: dict, *,
                  cursor_value: str | None) -> str | None:
    """Upsert one canonical object; record + return the change_type (created/updated/None-if-unchanged)."""
    prov: sc.Provenance = obj.provenance
    payload = _payload(obj)
    content_hash = _hash(payload)
    existing = (await s.execute(select(model).where(
        model.user_id == user_id, model.provider == prov.provider,
        model.provider_id == prov.provider_id))).scalar_one_or_none()
    source_id = await _upsert_source(s, user_id, object_type=object_type, prov=prov,
                                     payload=payload, content_hash=content_hash)
    prov_cols = _prov_cols(prov, content_hash, source_id)

    if existing is None:
        s.add(model(user_id=user_id, **cols, **prov_cols))
        change, changed_fields = "created", []
    elif existing.content_hash != content_hash:
        changed_fields = [k for k, v in cols.items() if getattr(existing, k, None) != v]
        for k, v in {**cols, **prov_cols}.items():
            setattr(existing, k, v)
        change = "updated"
    else:
        existing.last_synced_at = prov.last_synced_at  # touch freshness only; not a change
        return None

    s.add(schema.SchoolObjectChange(
        user_id=user_id, provider=prov.provider, object_type=object_type, provider_id=prov.provider_id,
        change_type=change, cursor_value=cursor_value, changed_fields=changed_fields))
    return change


# ---------------------------------------------------------------------------------------------------
# per-type column extractors (canonical object -> ORM columns, excluding provenance)
# ---------------------------------------------------------------------------------------------------


def _course_cols(c: sc.Course) -> dict:
    return dict(
        name=c.name, course_code=c.course_code, workflow_state=c.workflow_state,
        term_provider_id=c.term_provider_id, term_name=c.term_name,
        institution_provider_id=c.institution_provider_id, url=c.url,
        detail={"instructors": [i.model_dump(mode="json") for i in c.instructors],
                "sections": [se.model_dump(mode="json") for se in c.sections]})


def _assignment_cols(a: sc.Assignment) -> dict:
    return dict(
        course_provider_id=a.course_provider_id, name=a.name, description=a.description, due_at=a.due_at,
        unlock_at=a.unlock_at, lock_at=a.lock_at, points_possible=a.points_possible,
        submission_types=list(a.submission_types), submission_state=a.submission.kind.value,
        submitted_at=a.submission.submitted_at, score=a.submission.score, grade=a.submission.grade,
        late=a.submission.late, missing=a.submission.missing)


def _announcement_cols(a: sc.Announcement) -> dict:
    return dict(course_provider_id=a.course_provider_id, title=a.title, message=a.message, posted_at=a.posted_at)


def _institution_cols(i: sc.Institution) -> dict:
    return dict(name=i.name)


def _term_cols(t: sc.AcademicTerm) -> dict:
    return dict(name=t.name, start_at=t.start_at, end_at=t.end_at, is_current=t.is_current)


def _instructor_cols(i: sc.Instructor) -> dict:
    return dict(name=i.name, email=i.email, role=i.role)


def _material_cols(m: sc.Material) -> dict:
    return dict(course_provider_id=m.course_provider_id, title=m.title, kind=m.kind)


def _submission_cols(sub: sc.Submission) -> dict:
    return dict(
        assignment_provider_id=sub.assignment_provider_id, state=sub.state.value, submitted_at=sub.submitted_at,
        attempt=sub.attempt, late=sub.late, missing=sub.missing,
        score=sub.grade.score if sub.grade else None, grade=sub.grade.grade if sub.grade else None,
        points_possible=sub.grade.points_possible if sub.grade else None,
        graded_at=sub.grade.graded_at if sub.grade else None,
        rubric=sub.rubric.model_dump(mode="json") if sub.rubric else None,
        feedback=[f.model_dump(mode="json") for f in sub.feedback])


# ---------------------------------------------------------------------------------------------------
# sync driver
# ---------------------------------------------------------------------------------------------------


async def _get_cursor(s, user_id: UUID, provider: str, resource: str) -> sc.SyncCursor | None:
    row = (await s.execute(select(schema.SchoolSyncCursor).where(
        schema.SchoolSyncCursor.user_id == user_id, schema.SchoolSyncCursor.provider == provider,
        schema.SchoolSyncCursor.resource == resource))).scalar_one_or_none()
    if row is None:
        return None
    return sc.SyncCursor(provider=provider, resource=resource, cursor_value=row.cursor_value, updated_at=row.synced_at)


async def _put_cursor(s, user_id: UUID, cursor: sc.SyncCursor) -> None:
    row = (await s.execute(select(schema.SchoolSyncCursor).where(
        schema.SchoolSyncCursor.user_id == user_id, schema.SchoolSyncCursor.provider == cursor.provider,
        schema.SchoolSyncCursor.resource == cursor.resource))).scalar_one_or_none()
    if row is None:
        s.add(schema.SchoolSyncCursor(
            user_id=user_id, provider=cursor.provider, resource=cursor.resource,
            cursor_value=cursor.cursor_value, synced_at=cursor.updated_at))
    else:
        row.cursor_value = cursor.cursor_value
        row.synced_at = cursor.updated_at


async def sync_provider(connector: sc.SchoolConnector, user_id: UUID) -> SyncSummary:
    """Sync one provider's data for one student. Idempotent + restart-safe; safe to run repeatedly.

    Order: pull the dimension graph (institution/terms/instructors/materials) in full, then the three
    cursor-synced resources via ``connector.sync`` (restart-safe delta), persisting each change and its
    provenance, then pull each touched assignment's submission detail. Cursors advance only after their
    resource is fully applied, so a crash mid-run resumes from the last committed cursor.
    """
    summary = SyncSummary()
    async with user_session(user_id) as s:
        # -- dimension objects (small, idempotent full pulls) --------------------------------------
        inst = await connector.get_institution()
        if inst.ok:
            await _upsert(s, user_id, schema.SchoolInstitution, "institution", inst.data,
                          _institution_cols(inst.data), cursor_value=None)
        elif inst.state is CapabilityState.unsupported:
            summary.unsupported.append("institution")

        terms = await connector.list_terms()
        if terms.ok:
            for t in terms.data:
                await _upsert(s, user_id, schema.SchoolTerm, "term", t, _term_cols(t), cursor_value=None)

        instructors = await connector.list_instructors()
        if instructors.ok:
            for i in instructors.data:
                await _upsert(s, user_id, schema.SchoolInstructor, "instructor", i, _instructor_cols(i),
                              cursor_value=None)

        materials = await connector.list_materials()
        if materials.ok:
            for m in materials.data:
                await _upsert(s, user_id, schema.SchoolMaterial, "material", m, _material_cols(m),
                              cursor_value=None)

        # -- schedule events: honor an honest unsupported state (record it, persist nothing) --------
        events = await connector.list_schedule_events()
        if events.state is CapabilityState.unsupported:
            summary.unsupported.append("schedule_events")
        elif events.ok:
            for ev in events.data:
                await _upsert(s, user_id, schema.SchoolScheduleEvent, "schedule_event", ev,
                              dict(title=ev.title, start_at=ev.start_at, end_at=ev.end_at,
                                   location=ev.location, course_provider_id=ev.course_provider_id),
                              cursor_value=None)

        # -- cursor-synced resources (restart-safe delta + persist re-verification) -----------------
        touched_assignments: set[str] = set()
        cols_for = {"course": _course_cols, "assignment": _assignment_cols, "announcement": _announcement_cols}
        model_for = {"course": schema.SchoolCourse, "assignment": schema.SchoolAssignment,
                     "announcement": schema.SchoolAnnouncement}
        for resource in SYNC_RESOURCES:
            cursor = await _get_cursor(s, user_id, connector.provider, resource)
            page_res = await connector.sync(resource, cursor)
            if not page_res.ok:
                if page_res.state is CapabilityState.unsupported:
                    summary.unsupported.append(f"sync:{resource}")
                continue
            page = page_res.data
            object_type = _OBJECT_TYPE_FOR_RESOURCE[resource]
            model_cls = _MODEL_FOR_RESOURCE[resource]
            for ch in page.changes:
                if ch.change_type is sc.ChangeType.deleted:
                    await _delete_object(s, user_id, model_for[object_type], connector.provider,
                                         object_type, ch.provider_id, cursor_value=page.next_cursor.cursor_value)
                    summary.deleted += 1
                    continue
                obj = model_cls(**ch.object)
                change = await _upsert(s, user_id, model_for[object_type], object_type, obj,
                                       cols_for[object_type](obj), cursor_value=page.next_cursor.cursor_value)
                if change == "created":
                    summary.created += 1
                elif change == "updated":
                    summary.updated += 1
                if object_type == "assignment":
                    touched_assignments.add(obj.provider_id)
            await _put_cursor(s, user_id, page.next_cursor)
            summary.cursors[resource] = page.next_cursor.cursor_value

        # -- submission detail for touched assignments (feedback/rubric/grade facets) ---------------
        for aid in touched_assignments:
            sub_res = await connector.get_submission(aid)
            if sub_res.ok and sub_res.data is not None:
                await _upsert(s, user_id, schema.SchoolSubmission, "submission", sub_res.data,
                              _submission_cols(sub_res.data), cursor_value=None)

    return summary


async def _delete_object(s, user_id: UUID, model, provider: str, object_type: str,
                         provider_id: str, *, cursor_value: str | None) -> None:
    res = await s.execute(delete(model).where(
        model.user_id == user_id, model.provider == provider, model.provider_id == provider_id))
    if res.rowcount:
        s.add(schema.SchoolObjectChange(
            user_id=user_id, provider=provider, object_type=object_type, provider_id=provider_id,
            change_type="deleted", cursor_value=cursor_value, changed_fields=[]))


# ---------------------------------------------------------------------------------------------------
# read-back (canonical objects with provenance + change history rebuilt) — for the query layer
# ---------------------------------------------------------------------------------------------------


async def _changes_for(s, user_id: UUID, object_type: str, provider_id: str) -> list[sc.ChangeRecord]:
    rows = (await s.execute(select(schema.SchoolObjectChange).where(
        schema.SchoolObjectChange.user_id == user_id, schema.SchoolObjectChange.object_type == object_type,
        schema.SchoolObjectChange.provider_id == provider_id)
        .order_by(schema.SchoolObjectChange.detected_at))).scalars().all()
    return [sc.ChangeRecord(change_type=sc.ChangeType(r.change_type), detected_at=r.detected_at,
                            cursor_value=r.cursor_value, changed_fields=list(r.changed_fields or []))
            for r in rows]


async def _rebuild_prov(s, user_id: UUID, row, object_type: str) -> sc.Provenance:
    """Rebuild the provenance block from a persisted row, incl. the durable evidence id + change history."""
    evidence = None
    if row.source_id is not None:
        span = (await s.execute(select(schema.SchoolSourceSpan).where(
            schema.SchoolSourceSpan.source_id == row.source_id)
            .order_by(schema.SchoolSourceSpan.ordinal))).scalars().first()
        evidence = sc.SourceRef(
            source_id=str(row.source_id), source_url=row.source_url,
            span_id=str(span.id) if span else None, span_text=span.span_text if span else None)
    changes = await _changes_for(s, user_id, object_type, row.provider_id)
    return sc.Provenance(
        provider=row.provider, provider_id=row.provider_id, source_url=row.source_url,
        source_timestamp=row.source_timestamp, last_synced_at=row.last_synced_at, evidence=evidence,
        capability_state=CapabilityState(row.capability_state), changes=changes)


async def list_courses(user_id: UUID, *, provider: str = "canvas") -> list[sc.Course]:
    async with user_session(user_id) as s:
        rows = (await s.execute(select(schema.SchoolCourse).where(
            schema.SchoolCourse.user_id == user_id, schema.SchoolCourse.provider == provider))).scalars().all()
        out = []
        for r in rows:
            prov = await _rebuild_prov(s, user_id, r, "course")
            detail = r.detail or {}
            out.append(sc.Course(
                provider_id=r.provider_id, name=r.name, course_code=r.course_code,
                workflow_state=r.workflow_state, term_provider_id=r.term_provider_id, term_name=r.term_name,
                institution_provider_id=r.institution_provider_id,
                instructors=[sc.Instructor(**i) for i in detail.get("instructors", [])],
                sections=[sc.Section(**se) for se in detail.get("sections", [])], url=r.url, provenance=prov))
        return out


def _submission_state(row: schema.SchoolAssignment) -> sc.SubmissionState:
    return sc.SubmissionState(
        kind=sc.SubmissionStateKind(row.submission_state), submitted_at=row.submitted_at, score=row.score,
        grade=row.grade, late=row.late, missing=row.missing)


async def _assignment_from_row(s, user_id: UUID, r: schema.SchoolAssignment) -> sc.Assignment:
    prov = await _rebuild_prov(s, user_id, r, "assignment")
    return sc.Assignment(
        provider_id=r.provider_id, course_provider_id=r.course_provider_id, name=r.name,
        description=r.description, due_at=r.due_at, unlock_at=r.unlock_at, lock_at=r.lock_at,
        points_possible=r.points_possible, submission_types=list(r.submission_types or []), url=r.source_url,
        submission=_submission_state(r), provenance=prov)


async def list_assignments(user_id: UUID, *, provider: str = "canvas") -> list[sc.Assignment]:
    async with user_session(user_id) as s:
        rows = (await s.execute(select(schema.SchoolAssignment).where(
            schema.SchoolAssignment.user_id == user_id,
            schema.SchoolAssignment.provider == provider))).scalars().all()
        return [await _assignment_from_row(s, user_id, r) for r in rows]


async def list_announcements(user_id: UUID, *, provider: str = "canvas") -> list[sc.Announcement]:
    async with user_session(user_id) as s:
        rows = (await s.execute(select(schema.SchoolAnnouncement).where(
            schema.SchoolAnnouncement.user_id == user_id,
            schema.SchoolAnnouncement.provider == provider)
            .order_by(schema.SchoolAnnouncement.posted_at.desc()))).scalars().all()
        out = []
        for r in rows:
            prov = await _rebuild_prov(s, user_id, r, "announcement")
            out.append(sc.Announcement(
                provider_id=r.provider_id, course_provider_id=r.course_provider_id, title=r.title,
                message=r.message, posted_at=r.posted_at, url=r.source_url, provenance=prov))
        return out


@dataclasses.dataclass
class ChangedItem:
    """A change-history entry paired with the object it concerns — the unit "what changed?" returns."""

    object_type: str
    provider_id: str
    change_type: sc.ChangeType
    detected_at: datetime.datetime
    changed_fields: list[str]
    source_url: str | None
    cursor_value: str | None


async def changes_since(user_id: UUID, *, since: datetime.datetime | None = None,
                        provider: str = "canvas") -> list[ChangedItem]:
    """Every classified change (newest first), optionally since a timestamp — powers "what changed?"."""
    async with user_session(user_id) as s:
        q = select(schema.SchoolObjectChange).where(
            schema.SchoolObjectChange.user_id == user_id, schema.SchoolObjectChange.provider == provider)
        if since is not None:
            q = q.where(schema.SchoolObjectChange.detected_at >= since)
        rows = (await s.execute(q.order_by(schema.SchoolObjectChange.detected_at.desc()))).scalars().all()
        out = []
        for r in rows:
            src = (await s.execute(select(schema.SchoolSource.source_url).where(
                schema.SchoolSource.user_id == user_id, schema.SchoolSource.object_type == r.object_type,
                schema.SchoolSource.provider_id == r.provider_id))).scalar_one_or_none()
            out.append(ChangedItem(
                object_type=r.object_type, provider_id=r.provider_id,
                change_type=sc.ChangeType(r.change_type), detected_at=r.detected_at,
                changed_fields=list(r.changed_fields or []), source_url=src, cursor_value=r.cursor_value))
        return out
