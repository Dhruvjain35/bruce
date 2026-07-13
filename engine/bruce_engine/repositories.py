"""Repository layer — business logic never calls SQL directly.

Every student-owned accessor REQUIRES user_id in its signature (no unscoped get_x(id) exists to
misuse). Authorization is enforced at BOTH layers: the repository filters by user_id AND Postgres
RLS independently enforces it. Two interchangeable implementations behind each Protocol:
  * InMemory* — fast, deterministic, for unit tests.
  * Postgres* — real persistence via db.user_session (sets RLS context), optimistic concurrency,
    idempotency, and a repo-level FK cross-tenant guard (RLS alone can't block that).
"""

from __future__ import annotations

import uuid
from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from . import schema
from .db import user_session
from .records import (
    ConcurrencyError,
    CrossTenantError,
    MissionRecord,
    NotFoundError,
    SourceRecord,
    TaskRecord,
)


# --------------------------------------------------------------------------- protocols

class SourceRepository(Protocol):
    async def create(self, source: SourceRecord) -> SourceRecord: ...
    async def get_for_user(self, source_id: UUID, user_id: UUID) -> SourceRecord | None: ...


class TaskRepository(Protocol):
    async def create(self, task: TaskRecord) -> TaskRecord: ...
    async def get_for_user(self, task_id: UUID, user_id: UUID) -> TaskRecord | None: ...
    async def list_for_user(self, user_id: UUID) -> list[TaskRecord]: ...


class MissionRepository(Protocol):
    async def create(self, mission: MissionRecord) -> MissionRecord: ...
    async def get_for_user(self, mission_id: UUID, user_id: UUID) -> MissionRecord | None: ...
    async def update_phase(
        self, mission_id: UUID, user_id: UUID, expected_version: int, phase: str, short_status: str
    ) -> MissionRecord: ...
    async def finish(
        self, mission_id: UUID, user_id: UUID, expected_version: int, *, status: str, phase: str,
        short_status: str, plan: dict | None = None, error: str | None = None,
    ) -> MissionRecord: ...
    async def list_for_user(self, user_id: UUID) -> list[MissionRecord]: ...


# --------------------------------------------------------------------------- in-memory

class InMemoryStore:
    def __init__(self) -> None:
        self.sources: dict[UUID, SourceRecord] = {}
        self.tasks: dict[UUID, TaskRecord] = {}
        self.missions: dict[UUID, MissionRecord] = {}


class InMemorySourceRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    async def create(self, source: SourceRecord) -> SourceRecord:
        rec = source.model_copy(update={"id": source.id or uuid.uuid4(), "version": 1})
        self.store.sources[rec.id] = rec
        return rec

    async def get_for_user(self, source_id: UUID, user_id: UUID) -> SourceRecord | None:
        s = self.store.sources.get(source_id)
        return s if (s is not None and s.user_id == user_id) else None


class InMemoryTaskRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    async def create(self, task: TaskRecord) -> TaskRecord:
        if task.source_id is not None:
            src = self.store.sources.get(task.source_id)
            if src is None or src.user_id != task.user_id:
                raise CrossTenantError("source_id is not owned by this user")
        if task.idempotency_key:
            for t in self.store.tasks.values():
                if t.user_id == task.user_id and t.idempotency_key == task.idempotency_key:
                    return t
        rec = task.model_copy(update={"id": task.id or uuid.uuid4(), "version": 1})
        self.store.tasks[rec.id] = rec
        return rec

    async def get_for_user(self, task_id: UUID, user_id: UUID) -> TaskRecord | None:
        t = self.store.tasks.get(task_id)
        return t if (t is not None and t.user_id == user_id) else None

    async def list_for_user(self, user_id: UUID) -> list[TaskRecord]:
        return [t for t in self.store.tasks.values() if t.user_id == user_id]


class InMemoryMissionRepository:
    def __init__(self, store: InMemoryStore) -> None:
        self.store = store

    async def create(self, mission: MissionRecord) -> MissionRecord:
        if mission.idempotency_key:
            for m in self.store.missions.values():
                if m.user_id == mission.user_id and m.idempotency_key == mission.idempotency_key:
                    return m
        rec = mission.model_copy(update={"id": mission.id or uuid.uuid4(), "version": 1})
        self.store.missions[rec.id] = rec
        return rec

    async def get_for_user(self, mission_id: UUID, user_id: UUID) -> MissionRecord | None:
        m = self.store.missions.get(mission_id)
        return m if (m is not None and m.user_id == user_id) else None

    async def update_phase(
        self, mission_id: UUID, user_id: UUID, expected_version: int, phase: str, short_status: str
    ) -> MissionRecord:
        m = await self.get_for_user(mission_id, user_id)
        if m is None:
            raise NotFoundError(str(mission_id))
        if m.version != expected_version:
            raise ConcurrencyError(f"expected version {expected_version}, have {m.version}")
        rec = m.model_copy(update={"phase": phase, "short_status": short_status, "version": m.version + 1})
        self.store.missions[mission_id] = rec
        return rec

    async def finish(self, mission_id, user_id, expected_version, *, status, phase, short_status, plan=None, error=None):
        m = await self.get_for_user(mission_id, user_id)
        if m is None:
            raise NotFoundError(str(mission_id))
        if m.version != expected_version:
            raise ConcurrencyError(f"expected version {expected_version}, have {m.version}")
        rec = m.model_copy(update={
            "status": status, "phase": phase, "short_status": short_status,
            "plan": plan, "error": error, "version": m.version + 1,
        })
        self.store.missions[mission_id] = rec
        return rec

    async def list_for_user(self, user_id: UUID) -> list[MissionRecord]:
        return [m for m in self.store.missions.values() if m.user_id == user_id]


# --------------------------------------------------------------------------- postgres

def _source_rec(row: schema.Source) -> SourceRecord:
    return SourceRecord(
        id=row.id, user_id=row.user_id, kind=row.kind, content_sha256=row.content_sha256,
        raw_text=row.raw_text, version=row.version,
    )


def _task_rec(row: schema.TaskRow) -> TaskRecord:
    return TaskRecord(
        id=row.id, user_id=row.user_id, kind=row.kind, title=row.title, due=row.due,
        status=row.status, source_id=row.source_id, required_items=row.required_items or [],
        idempotency_key=row.idempotency_key, version=row.version,
    )


def _mission_rec(row: schema.Mission) -> MissionRecord:
    return MissionRecord(
        id=row.id, user_id=row.user_id, kind=row.kind, status=row.status, phase=row.phase,
        short_status=row.short_status, goal=row.goal or {}, plan=row.plan, error=row.error,
        idempotency_key=row.idempotency_key, version=row.version,
    )


class PostgresSourceRepository:
    async def create(self, source: SourceRecord) -> SourceRecord:
        async with user_session(source.user_id) as s:
            row = schema.Source(
                user_id=source.user_id, kind=source.kind,
                content_sha256=source.content_sha256, raw_text=source.raw_text,
            )
            s.add(row)
            await s.flush()
            return _source_rec(row)

    async def get_for_user(self, source_id: UUID, user_id: UUID) -> SourceRecord | None:
        async with user_session(user_id) as s:
            row = (
                await s.execute(
                    select(schema.Source).where(
                        schema.Source.id == source_id, schema.Source.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            return _source_rec(row) if row is not None else None


class PostgresTaskRepository:
    async def create(self, task: TaskRecord) -> TaskRecord:
        async with user_session(task.user_id) as s:
            # FK cross-tenant guard: source must be visible to THIS user (RLS + explicit filter).
            if task.source_id is not None:
                src = (
                    await s.execute(
                        select(schema.Source.id).where(
                            schema.Source.id == task.source_id,
                            schema.Source.user_id == task.user_id,
                        )
                    )
                ).scalar_one_or_none()
                if src is None:
                    raise CrossTenantError("source_id is not owned by this user")
            if task.idempotency_key:
                existing = (
                    await s.execute(
                        select(schema.TaskRow).where(
                            schema.TaskRow.user_id == task.user_id,
                            schema.TaskRow.idempotency_key == task.idempotency_key,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return _task_rec(existing)
            row = schema.TaskRow(
                user_id=task.user_id, kind=task.kind, title=task.title, due=task.due,
                status=task.status, source_id=task.source_id,
                required_items=task.required_items, idempotency_key=task.idempotency_key,
            )
            s.add(row)
            try:
                await s.flush()
            except IntegrityError:
                raise  # rolled back by user_session; idempotency race handled by re-create by caller
            return _task_rec(row)

    async def get_for_user(self, task_id: UUID, user_id: UUID) -> TaskRecord | None:
        async with user_session(user_id) as s:
            row = (
                await s.execute(
                    select(schema.TaskRow).where(
                        schema.TaskRow.id == task_id, schema.TaskRow.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            return _task_rec(row) if row is not None else None

    async def list_for_user(self, user_id: UUID) -> list[TaskRecord]:
        async with user_session(user_id) as s:
            rows = (
                await s.execute(select(schema.TaskRow).where(schema.TaskRow.user_id == user_id))
            ).scalars().all()
            return [_task_rec(r) for r in rows]


class PostgresMissionRepository:
    async def create(self, mission: MissionRecord) -> MissionRecord:
        try:
            async with user_session(mission.user_id) as s:
                if mission.idempotency_key:
                    existing = (
                        await s.execute(
                            select(schema.Mission).where(
                                schema.Mission.user_id == mission.user_id,
                                schema.Mission.idempotency_key == mission.idempotency_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        return _mission_rec(existing)
                row = schema.Mission(
                    user_id=mission.user_id, kind=mission.kind, status=mission.status,
                    phase=mission.phase, short_status=mission.short_status, goal=mission.goal,
                    plan=mission.plan, error=mission.error, idempotency_key=mission.idempotency_key,
                )
                s.add(row)
                await s.flush()
                return _mission_rec(row)
        except IntegrityError:
            # concurrent identical idempotent create won the race -> return the persisted one
            if mission.idempotency_key:
                async with user_session(mission.user_id) as s:
                    existing = (
                        await s.execute(
                            select(schema.Mission).where(
                                schema.Mission.user_id == mission.user_id,
                                schema.Mission.idempotency_key == mission.idempotency_key,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        return _mission_rec(existing)
            raise

    async def get_for_user(self, mission_id: UUID, user_id: UUID) -> MissionRecord | None:
        async with user_session(user_id) as s:
            row = (
                await s.execute(
                    select(schema.Mission).where(
                        schema.Mission.id == mission_id, schema.Mission.user_id == user_id
                    )
                )
            ).scalar_one_or_none()
            return _mission_rec(row) if row is not None else None

    async def update_phase(
        self, mission_id: UUID, user_id: UUID, expected_version: int, phase: str, short_status: str
    ) -> MissionRecord:
        async with user_session(user_id) as s:
            res = await s.execute(
                update(schema.Mission)
                .where(
                    schema.Mission.id == mission_id,
                    schema.Mission.user_id == user_id,
                    schema.Mission.version == expected_version,
                )
                .values(phase=phase, short_status=short_status, version=schema.Mission.version + 1)
            )
            if res.rowcount == 1:
                s.add(
                    schema.MissionPhaseEvent(
                        user_id=user_id, mission_id=mission_id, phase=phase, short_status=short_status
                    )
                )
                row = (
                    await s.execute(
                        select(schema.Mission).where(
                            schema.Mission.id == mission_id, schema.Mission.user_id == user_id
                        )
                    )
                ).scalar_one()
                return _mission_rec(row)
        # not updated: distinguish not-found (404) from version conflict (409)
        existing = await self.get_for_user(mission_id, user_id)
        if existing is None:
            raise NotFoundError(str(mission_id))
        raise ConcurrencyError(f"expected version {expected_version}, have {existing.version}")

    async def finish(self, mission_id, user_id, expected_version, *, status, phase, short_status, plan=None, error=None):
        async with user_session(user_id) as s:
            res = await s.execute(
                update(schema.Mission)
                .where(
                    schema.Mission.id == mission_id,
                    schema.Mission.user_id == user_id,
                    schema.Mission.version == expected_version,
                )
                .values(
                    status=status, phase=phase, short_status=short_status,
                    plan=plan, error=error, version=schema.Mission.version + 1,
                )
            )
            if res.rowcount == 1:
                s.add(schema.MissionPhaseEvent(
                    user_id=user_id, mission_id=mission_id, phase=phase, short_status=short_status
                ))
                row = (
                    await s.execute(
                        select(schema.Mission).where(
                            schema.Mission.id == mission_id, schema.Mission.user_id == user_id
                        )
                    )
                ).scalar_one()
                return _mission_rec(row)
        existing = await self.get_for_user(mission_id, user_id)
        if existing is None:
            raise NotFoundError(str(mission_id))
        raise ConcurrencyError(f"expected version {expected_version}, have {existing.version}")

    async def list_for_user(self, user_id: UUID) -> list[MissionRecord]:
        async with user_session(user_id) as s:
            rows = (
                await s.execute(select(schema.Mission).where(schema.Mission.user_id == user_id))
            ).scalars().all()
            return [_mission_rec(r) for r in rows]


class UserRepository(Protocol):
    async def ensure(self, user_id: UUID, *, auth_provider: str = "supabase", email: str | None = None) -> None: ...
    async def delete(self, user_id: UUID) -> None: ...


class PostgresUserRepository:
    """Upsert the authenticated user on first request (id == the verified token subject)."""

    async def ensure(self, user_id: UUID, *, auth_provider: str = "supabase", email: str | None = None) -> None:
        async with user_session(user_id) as s:
            existing = (
                await s.execute(select(schema.User).where(schema.User.id == user_id))
            ).scalar_one_or_none()
            if existing is None:
                s.add(schema.User(id=user_id, auth_provider=auth_provider, email=email))
                await s.flush()

    async def delete(self, user_id: UUID) -> None:
        # Deletes the user's own row (RLS: id = app_current_user()); FK ON DELETE CASCADE
        # removes all rows they own. This is the account-deletion path.
        async with user_session(user_id) as s:
            await s.execute(delete(schema.User).where(schema.User.id == user_id))
