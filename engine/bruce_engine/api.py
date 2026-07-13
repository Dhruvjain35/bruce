"""FastAPI service — authenticated, persistence-backed.

Every endpoint derives the user from a verified JWT (never from client input). Missions are
persisted in Postgres and scoped to the authenticated user (404 on wrong owner); the background
worker runs under explicit user context. Phase-1 compute endpoints (intake/opportunities/tasks/
calendar/brief) are stateless today but still require auth. In-memory store (missions) is gone —
missions survive restart. Run: PYTHONPATH=. python scripts/run_api.py
"""

from __future__ import annotations

import asyncio
from datetime import date
from enum import Enum
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import calendar_build
from . import tasks as tasks_mod
from .auth import AuthenticatedUser, current_user
from .briefing import compose_brief
from .extraction import extract_from_text
from .models import (
    CalendarEvent,
    DailyBrief,
    ExtractedIntake,
    IntakeSourceKind,
    MissionPhase,
    OutreachGoal,
    StudentProfile,
    Task,
)
from .opportunity import RankedOpportunity, ingest_opportunity_text
from .pipeline import build_outreach_plan
from .records import MissionRecord
from .repositories import PostgresMissionRepository, PostgresUserRepository

app = FastAPI(title="Bruce Engine API", version="0.2.0")

# Swappable for tests (monkeypatch to in-memory implementations).
_mission_repo = PostgresMissionRepository()
_user_repo = PostgresUserRepository()


class MissionStatus(str, Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


_PHASE_STATUS: dict[MissionPhase, str] = {
    MissionPhase.created: "Starting…",
    MissionPhase.understanding: "Understanding your request",
    MissionPhase.extracting: "Reading the details",
    MissionPhase.awaiting_approval: "One decision needed",
    MissionPhase.executing: "Finding people and drafting",
    MissionPhase.waiting_external: "Waiting on an external service",
    MissionPhase.verifying: "Verifying the results",
    MissionPhase.succeeded: "Done",
    MissionPhase.blocked: "Blocked — needs you",
    MissionPhase.failed: "Couldn't finish",
}


class MissionRequest(BaseModel):
    student: StudentProfile
    goal: OutreachGoal
    limit: int = Field(default=6, ge=1, le=20)
    idempotency_key: str | None = None


class MissionCreated(BaseModel):
    mission_id: UUID
    status: str
    phase: str


class MissionView(BaseModel):
    mission_id: UUID
    status: str
    phase: str
    short_status: str
    error: str | None = None
    plan: dict | None = None
    version: int


def _view(rec: MissionRecord) -> MissionView:
    return MissionView(
        mission_id=rec.id, status=rec.status, phase=rec.phase, short_status=rec.short_status,
        error=rec.error, plan=rec.plan, version=rec.version,
    )


async def _run_mission(mission_id: UUID, user_id: UUID, req: MissionRequest) -> None:
    """Background worker — runs under EXPLICIT user context (repo uses user_session(user_id))."""
    try:
        plan = await build_outreach_plan(req.student, req.goal, limit=req.limit)
        await _mission_repo.finish(
            mission_id, user_id, expected_version=1, status=MissionStatus.succeeded.value,
            phase=MissionPhase.succeeded.value, short_status=_PHASE_STATUS[MissionPhase.succeeded],
            plan=plan.model_dump(mode="json"),
        )
    except Exception as exc:
        try:
            await _mission_repo.finish(
                mission_id, user_id, expected_version=1, status=MissionStatus.failed.value,
                phase=MissionPhase.failed.value, short_status=_PHASE_STATUS[MissionPhase.failed],
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass  # never let the worker crash the event loop


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/missions", response_model=MissionCreated)
async def create_mission(req: MissionRequest, user: AuthenticatedUser = Depends(current_user)) -> MissionCreated:
    await _user_repo.ensure(user.user_id, auth_provider=user.auth_provider)
    rec = await _mission_repo.create(
        MissionRecord(
            user_id=user.user_id, goal=req.goal.model_dump(mode="json"),
            status=MissionStatus.running.value, phase=MissionPhase.created.value,
            short_status=_PHASE_STATUS[MissionPhase.created], idempotency_key=req.idempotency_key,
        )
    )
    asyncio.create_task(_run_mission(rec.id, user.user_id, req))
    return MissionCreated(mission_id=rec.id, status=rec.status, phase=rec.phase)


@app.get("/v1/missions/{mission_id}", response_model=MissionView)
async def get_mission(mission_id: UUID, user: AuthenticatedUser = Depends(current_user)) -> MissionView:
    rec = await _mission_repo.get_for_user(mission_id, user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="mission not found")  # 404, never a revealing 403
    return _view(rec)


@app.get("/v1/missions", response_model=list[MissionView])
async def list_missions(user: AuthenticatedUser = Depends(current_user)) -> list[MissionView]:
    return [_view(r) for r in await _mission_repo.list_for_user(user.user_id)]


@app.delete("/v1/account")
async def delete_account(user: AuthenticatedUser = Depends(current_user)) -> dict[str, bool]:
    await _user_repo.delete(user.user_id)  # cascade removes all rows the user owns
    return {"deleted": True}


# ------------------------------------------------------- Phase 1 compute endpoints (auth-gated)

class IntakeRequest(BaseModel):
    text: str = Field(min_length=1)
    source_kind: IntakeSourceKind = IntakeSourceKind.text


@app.post("/v1/intake", response_model=ExtractedIntake)
async def intake(req: IntakeRequest, user: AuthenticatedUser = Depends(current_user)) -> ExtractedIntake:
    try:
        return await extract_from_text(req.text, source_kind=req.source_kind)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"extraction failed: {type(exc).__name__}")


class OpportunityRequest(BaseModel):
    text: str = Field(min_length=1)
    student: StudentProfile


class OpportunityResponse(BaseModel):
    intake: ExtractedIntake
    classification: str
    is_spam: bool
    fit: RankedOpportunity | None = None
    task: Task


@app.post("/v1/opportunities", response_model=OpportunityResponse)
async def opportunities(req: OpportunityRequest, user: AuthenticatedUser = Depends(current_user)) -> OpportunityResponse:
    try:
        result = await ingest_opportunity_text(req.text, req.student)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"opportunity ingest failed: {type(exc).__name__}")
    return OpportunityResponse(**result)


class TasksRequest(BaseModel):
    intakes: list[ExtractedIntake] = Field(default_factory=list)


class TasksResponse(BaseModel):
    tasks: list[Task]
    buckets: dict[str, list[Task]]
    counts: dict[str, int]


@app.post("/v1/tasks", response_model=TasksResponse)
async def build_tasks(req: TasksRequest, user: AuthenticatedUser = Depends(current_user)) -> TasksResponse:
    all_tasks: list[Task] = []
    for it in req.intakes:
        all_tasks.extend(tasks_mod.intake_to_tasks(it))
    return TasksResponse(
        tasks=all_tasks,
        buckets=tasks_mod.bucketize(all_tasks, date.today()),
        counts=tasks_mod.status_counts(all_tasks),
    )


class CalendarRequest(BaseModel):
    intake: ExtractedIntake


class CalendarResponse(BaseModel):
    events: list[CalendarEvent]
    ics: str
    conflicts: list[list[int]]


@app.post("/v1/calendar", response_model=CalendarResponse)
async def calendar(req: CalendarRequest, user: AuthenticatedUser = Depends(current_user)) -> CalendarResponse:
    events = calendar_build.intake_to_events(req.intake)
    return CalendarResponse(
        events=events,
        ics=calendar_build.to_ics(events),
        conflicts=[list(pair) for pair in calendar_build.detect_conflicts(events)],
    )


class BriefRequest(BaseModel):
    tasks: list[Task] = Field(default_factory=list)
    kind: str = Field(default="morning")


@app.post("/v1/brief", response_model=DailyBrief)
async def brief(req: BriefRequest, user: AuthenticatedUser = Depends(current_user)) -> DailyBrief:
    return compose_brief(req.tasks, req.kind, date.today())
