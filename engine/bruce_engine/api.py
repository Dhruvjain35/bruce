"""FastAPI service — authenticated, persistence-backed.

Every endpoint derives the user from a verified JWT (never from client input). Missions are
persisted in Postgres and scoped to the authenticated user (404 on wrong owner); the background
worker runs under explicit user context. /v1/intake is persistence-backed too: it durably writes
source -> spans -> tasks under RLS, atomically and idempotently (see intake_store).

STILL STATELESS (honest status): /v1/opportunities, /v1/tasks, /v1/calendar and /v1/brief compute
from request input and persist nothing — /v1/tasks and /v1/brief still require the CLIENT to hand
back the state on every call. They are being migrated one at a time, in that order; do not read
their auth-gating as persistence. Run: PYTHONPATH=. python scripts/run_api.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import date
from enum import Enum
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from sqlalchemy import text as sa_text

from .db import user_session

from . import calendar_build
from . import intake_store
from . import llm
from . import provider_status
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

# Swappable for tests (monkeypatch to in-memory implementations). Production is Postgres-only.
_mission_repo = PostgresMissionRepository()
_user_repo = PostgresUserRepository()
_persist_intake = intake_store.persist_intake


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
    """Background worker — runs under EXPLICIT user context; persists each phase so the UI shows progress."""
    version = 1  # created row is v1

    async def on_phase(phase: MissionPhase) -> None:
        nonlocal version
        rec = await _mission_repo.update_phase(
            mission_id, user_id, version, phase.value, _PHASE_STATUS.get(phase, "Working…")
        )
        version = rec.version

    try:
        plan = await build_outreach_plan(req.student, req.goal, limit=req.limit, on_phase=on_phase)
        await _mission_repo.finish(
            mission_id, user_id, version, status=MissionStatus.succeeded.value,
            phase=MissionPhase.succeeded.value, short_status=_PHASE_STATUS[MissionPhase.succeeded],
            plan=plan.model_dump(mode="json"),
        )
    except Exception as exc:
        try:
            await _mission_repo.finish(
                mission_id, user_id, version, status=MissionStatus.failed.value,
                phase=MissionPhase.failed.value, short_status=_PHASE_STATUS[MissionPhase.failed],
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass  # never let the worker crash the event loop


@app.get("/health")
async def health() -> dict[str, str]:
    """PUBLIC liveness probe — no auth, no secrets, no user data.

    Deliberately dumb: it proves the process is up and serving, nothing more. It must never touch
    the database or a provider, or a provider outage would make the platform think the service is
    dead and recycle it. Deeper checks live behind auth on /v1/diagnostics.

    BRUCE_COMMIT is injected at build time so a deployed URL can be tied to an exact commit — the
    hackathon submission has to prove which code is running.
    """
    return {
        "status": "ok",
        "service": "bruce-engine",
        "commit": os.environ.get("BRUCE_COMMIT", "unknown"),
        "region": os.environ.get("BRUCE_REGION", "unknown"),
    }


class ProviderReport(BaseModel):
    provider: str
    model: str
    configured: bool
    live: bool
    detail: str


class Diagnostics(BaseModel):
    commit: str
    region: str
    database: str
    intake_provider: ProviderReport


@app.get("/v1/diagnostics", response_model=Diagnostics)
async def diagnostics(user: AuthenticatedUser = Depends(current_user)) -> Diagnostics:
    """AUTHENTICATED deployment verification: is this deployment actually wired to anything real?

    Requires a valid JWT — it reports infrastructure state, which is not public information. It
    returns NO student data and NO secrets: provider/model names and reachability only.

    `live` is the honest bit. It is True only if a real inference call to the configured provider
    succeeds right now. While Qwen Cloud is account-blocked this returns live=false with the real
    reason, and it must stay that way until a genuine call succeeds.
    """
    db_state = "unknown"
    try:
        async with user_session(user.user_id) as s:
            await s.execute(sa_text("SELECT 1"))
        db_state = "connected"
    except Exception as exc:
        db_state = f"unavailable ({type(exc).__name__})"

    provider, model = llm.intake_provider(), llm.qwen_intake_model_id()
    live, detail = False, ""
    try:
        agent = Agent(llm.intake_model())
        await agent.run("ping")
        live, detail = True, "a real inference call succeeded"
    except Exception as exc:
        unavailable = provider_status.classify(exc, provider=provider, model=model)
        detail = unavailable.reason if unavailable else f"unexpected: {type(exc).__name__}"

    return Diagnostics(
        commit=os.environ.get("BRUCE_COMMIT", "unknown"),
        region=os.environ.get("BRUCE_REGION", "unknown"),
        database=db_state,
        intake_provider=ProviderReport(
            provider=provider,
            model=model,
            configured=bool(os.environ.get("DASHSCOPE_API_KEY")),
            live=live,
            detail=detail,
        ),
    )


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
    # Optional: a retry with the same key is idempotent. Omit it and the key is derived from the
    # content itself, so a double-tap in the app is already safe without client cooperation.
    idempotency_key: str | None = Field(default=None, max_length=intake_store.MAX_CLIENT_KEY)


class IntakeResponse(ExtractedIntake):
    """The extraction (unchanged, every field at its original path) PLUS the ids it durably created.

    Additive by design: existing clients reading title/deadlines/required_items are unaffected; the
    Swift client gets stable ids it can fetch back. source_id -> span_ids -> task_ids is the real
    lineage, not a display convenience.
    """

    source_id: UUID
    span_ids: list[UUID] = Field(default_factory=list)
    task_ids: list[UUID] = Field(default_factory=list)


@app.post("/v1/intake", response_model=IntakeResponse)
async def intake(req: IntakeRequest, user: AuthenticatedUser = Depends(current_user)) -> IntakeResponse:
    """Extract a raw student input AND durably persist source -> spans -> tasks for that user.

    user_id comes only from the verified token. Persistence is atomic: a failed extraction leaves
    no source behind. Retries return the original ids and the original extraction.
    """
    await _user_repo.ensure(user.user_id, auth_provider=user.auth_provider)
    try:
        result = await _persist_intake(
            user_id=user.user_id,
            text=req.text,
            source_kind=req.source_kind,
            extract=extract_from_text,  # resolved here (not at import) so tests can patch it
            idempotency_key=req.idempotency_key,
        )
    except Exception as exc:
        # A provider outage is reported as exactly that — 503 provider_unavailable, naming the
        # provider and the real cause. It is NOT retried against a different provider: silently
        # answering with OpenAI while claiming a Qwen-powered workflow would be a lie. And it is
        # never turned into an empty-but-successful intake, which would read to the student as
        # "Bruce read your flyer and found nothing".
        unavailable = provider_status.classify(
            exc, provider=llm.intake_provider(), model=llm.qwen_intake_model_id()
        )
        if unavailable is not None:
            raise HTTPException(status_code=503, detail=unavailable.as_detail())
        # Type only — never the message: it can quote the student's raw content.
        raise HTTPException(status_code=502, detail=f"extraction failed: {type(exc).__name__}")
    return IntakeResponse(
        **result.intake.model_dump(),
        source_id=result.source_id,
        span_ids=result.span_ids,
        task_ids=result.task_ids,
    )


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
