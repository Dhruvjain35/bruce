"""FastAPI service — exposes the Bruce engine + Phase-1 intake as an API.

Long-running outreach missions run in the background (create -> poll); the fast Phase-1 endpoints
(intake, opportunities, tasks, calendar, brief) respond inline. Mission state carries an observable
PHASE + short status — the contract the iOS Dynamic Island will render (the Live Activity UI itself
ships with the client). In-memory store (v1), no auth yet — do not deploy exposed. Keys stay
server-side. Run: PYTHONPATH=. python scripts/run_api.py
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import calendar_build
from . import tasks as tasks_mod
from .briefing import compose_brief
from .extraction import extract_from_text
from .models import (
    CalendarEvent,
    DailyBrief,
    ExtractedIntake,
    IntakeSourceKind,
    MissionPhase,
    OutreachGoal,
    OutreachPlan,
    StudentProfile,
    Task,
)
from .opportunity import RankedOpportunity, ingest_opportunity_text
from .pipeline import build_outreach_plan

app = FastAPI(title="Bruce Engine API", version="0.1.0")


# --------------------------------------------------------------------------- missions

class MissionStatus(str, Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


# Human, glanceable status per phase — what the Dynamic Island / Live Activity shows.
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


class MissionCreated(BaseModel):
    mission_id: str
    status: MissionStatus
    phase: MissionPhase


class MissionState(BaseModel):
    mission_id: str
    status: MissionStatus
    phase: MissionPhase = MissionPhase.created
    short_status: str = _PHASE_STATUS[MissionPhase.created]
    error: str | None = None
    plan: OutreachPlan | None = None


_MISSIONS: dict[str, MissionState] = {}


async def _run_mission(mission_id: str, req: MissionRequest) -> None:
    def on_phase(phase: MissionPhase) -> None:
        st = _MISSIONS.get(mission_id)
        if st is not None:
            st.phase = phase
            st.short_status = _PHASE_STATUS.get(phase, st.short_status)

    try:
        plan = await build_outreach_plan(req.student, req.goal, limit=req.limit, on_phase=on_phase)
        _MISSIONS[mission_id] = MissionState(
            mission_id=mission_id,
            status=MissionStatus.succeeded,
            phase=MissionPhase.succeeded,
            short_status=_PHASE_STATUS[MissionPhase.succeeded],
            plan=plan,
        )
    except Exception as exc:
        _MISSIONS[mission_id] = MissionState(
            mission_id=mission_id,
            status=MissionStatus.failed,
            phase=MissionPhase.failed,
            short_status=_PHASE_STATUS[MissionPhase.failed],
            error=f"{type(exc).__name__}: {exc}",
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/missions", response_model=MissionCreated)
async def create_mission(req: MissionRequest) -> MissionCreated:
    mission_id = uuid.uuid4().hex
    _MISSIONS[mission_id] = MissionState(
        mission_id=mission_id, status=MissionStatus.running, phase=MissionPhase.created
    )
    asyncio.create_task(_run_mission(mission_id, req))
    return MissionCreated(mission_id=mission_id, status=MissionStatus.running, phase=MissionPhase.created)


@app.get("/v1/missions/{mission_id}", response_model=MissionState)
async def get_mission(mission_id: str) -> MissionState:
    st = _MISSIONS.get(mission_id)
    if st is None:
        raise HTTPException(status_code=404, detail="mission not found")
    return st


# ------------------------------------------------------- Phase 1: intake / tasks / etc.

class IntakeRequest(BaseModel):
    text: str = Field(min_length=1)
    source_kind: IntakeSourceKind = IntakeSourceKind.text


@app.post("/v1/intake", response_model=ExtractedIntake)
async def intake(req: IntakeRequest) -> ExtractedIntake:
    """#2 — forward anything school-related as text -> grounded structured intake."""
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
async def opportunities(req: OpportunityRequest) -> OpportunityResponse:
    """#1 — an opportunity email/text -> classified, fit-ranked, ready-to-track task."""
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
async def build_tasks(req: TasksRequest) -> TasksResponse:
    """#3 — turn extracted intakes into one canonical, bucketed task list."""
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
async def calendar(req: CalendarRequest) -> CalendarResponse:
    """#4 — build tentative calendar events + a downloadable .ics from an intake."""
    events = calendar_build.intake_to_events(req.intake)
    return CalendarResponse(
        events=events,
        ics=calendar_build.to_ics(events),
        conflicts=[list(pair) for pair in calendar_build.detect_conflicts(events)],
    )


class BriefRequest(BaseModel):
    tasks: list[Task] = Field(default_factory=list)
    kind: str = Field(default="morning", description="morning | afterschool | night")


@app.post("/v1/brief", response_model=DailyBrief)
async def brief(req: BriefRequest) -> DailyBrief:
    """#5 — compose the ~5-line daily brief from the task list."""
    return compose_brief(req.tasks, req.kind, date.today())
