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
import base64
import contextlib
import os
from datetime import date
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select as sa_select
from sqlalchemy import text as sa_text

from . import apple_auth
from . import auth
from . import calendar_build
from . import extraction
from . import intake_store
from . import schema
from . import task_dispatch
from . import tasks as tasks_mod
from .auth import AuthenticatedUser, current_user
from .briefing import compose_brief
from .db import user_session
from .extraction import ExtractionError, extract_from_text
from .intake_jobs import PostgresJobStore
from .provider_status import ProviderUnavailable
from .worker import IntakeWorker
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

# In-process intake worker. OFF by default so TestClient / offline runs don't spawn a poll loop that
# hammers a non-existent DB. Set BRUCE_INPROC_WORKER=1 in a single-process deployment. NOTE (alpha):
# an in-process loop is a convenience — durability lives in the intake_jobs table + lease, so a
# dedicated worker process can replace this with no contract change.
_worker: IntakeWorker | None = None


@contextlib.asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    global _worker
    if os.environ.get("BRUCE_INPROC_WORKER", "").strip().lower() in {"1", "true", "yes", "on"}:
        _worker = IntakeWorker(PostgresJobStore())
        _worker.start()
    yield
    if _worker is not None:
        await _worker.stop()


app = FastAPI(title="Bruce Engine API", version="0.2.0", lifespan=_lifespan)

# Swappable for tests (monkeypatch to in-memory implementations). Production is Postgres-only.
_mission_repo = PostgresMissionRepository()
_user_repo = PostgresUserRepository()
_persist_intake = intake_store.persist_intake  # synchronous service, retained (see intake_store)


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
    # Intake missions only (additive; None for outreach). Populated once extraction lands.
    extracted: dict | None = None  # the ExtractedIntake JSON (deadlines, required_items, …)
    blocking_reason: str | None = None  # why the mission is blocked/failed (TYPE/short cause, no content)
    available_actions: list[str] = Field(default_factory=list)


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
    # Includes the deployed commit + environment so a smoke test can prove WHICH build is live.
    # Stays dependency-free (never touches the DB/providers) — see /ready for dependency checks.
    return {
        "status": "ok",
        "commit": os.environ.get("BRUCE_COMMIT", "unknown"),
        "env": os.environ.get("BRUCE_ENV", "local"),
    }


class AppleSignInRequest(BaseModel):
    identity_token: str = Field(min_length=1)
    raw_nonce: str = Field(min_length=1)  # the client's one-time random value (unhashed)
    full_name: str | None = Field(default=None, max_length=200)  # first sign-in only; optional


class SessionToken(BaseModel):
    token: str
    user_id: UUID
    expires_in: int


@app.post("/v1/auth/apple", response_model=SessionToken)
async def sign_in_with_apple(req: AppleSignInRequest) -> SessionToken:
    """Exchange a verified Sign in with Apple identity token for a Bruce session JWT.

    PUBLIC (it mints auth). The Bruce user is derived from Apple's stable subject — the client never
    supplies a user id. Idempotent: first and returning sign-ins both land the same user_id; a
    duplicate/retried callback just re-issues a token. Email is stored only if Apple sends it (first
    authorization); a returning sign-in without email never clears it.
    """
    try:
        identity = apple_auth.verify_apple_token(req.identity_token, req.raw_nonce)
    except apple_auth.AppleAuthError as exc:
        # Type/short reason only — never the token or any student data.
        raise HTTPException(status_code=401, detail={"error": "apple_auth_failed", "reason": str(exc)})

    await _user_repo.ensure(identity.bruce_user_id, auth_provider="apple", email=identity.email)
    ttl = int(os.environ.get("BRUCE_SESSION_TTL_SECONDS", auth.DEFAULT_SESSION_TTL_SECONDS))
    token = auth.mint_bruce_jwt(identity.bruce_user_id, provider="apple", ttl_seconds=ttl)
    return SessionToken(token=token, user_id=identity.bruce_user_id, expires_in=ttl)


class Readiness(BaseModel):
    ready: bool
    checks: dict[str, str]


@app.get("/ready")
async def ready(response: Response) -> Readiness:
    """PUBLIC readiness probe — are Bruce's MANDATORY runtime dependencies usable?

    Distinct from /health on purpose:
      /health = this process is up and serving (never touches the DB or a provider, so a dependency
                outage can't make the platform think the process is dead and recycle it).
      /ready  = the things Bruce cannot function without are actually usable.

    Mandatory here means: the database (every guarantee Bruce makes — RLS, isolation, idempotency,
    evidence lineage — is enforced BY Postgres) and JWT configuration (without it, either nothing
    authenticates or, worse, something doesn't).

    A model provider is deliberately NOT a readiness condition. If a provider is blocked, intake
    returns a truthful 503 provider_unavailable while missions/decisions/receipts keep working; the
    service should not be pulled from the load balancer. Provider state is reported separately by
    /v1/diagnostics (authenticated). Returns 503 when not ready; the body names which check failed,
    with no secrets.
    """
    checks: dict[str, str] = {}

    try:
        async with user_session(uuid5(NAMESPACE_URL, "bruce.readiness.probe")) as s:
            await s.execute(sa_text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"unavailable ({type(exc).__name__})"

    secret, jwks = os.environ.get("BRUCE_JWT_SECRET"), os.environ.get("BRUCE_JWKS_URL")
    if secret and len(secret) >= 32:
        checks["auth_config"] = "ok"
    elif jwks:
        checks["auth_config"] = "ok"
    elif secret:
        checks["auth_config"] = "weak (BRUCE_JWT_SECRET under 32 bytes)"
    else:
        checks["auth_config"] = "missing (no BRUCE_JWT_SECRET or BRUCE_JWKS_URL)"

    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = 503
    return Readiness(ready=ok, checks=checks)


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


_INTAKE_ACTIONS = {
    "awaiting_approval": ["approve", "dismiss"],
    "blocked": ["retry", "dismiss"],
    "failed": ["retry", "dismiss"],
}


async def _enrich_intake(rec: MissionRecord, user_id: UUID) -> MissionView:
    """For an intake mission, attach the extracted objects + blocking reason + available actions.

    Cross-user access can't happen: everything is read inside user_session(user_id), so RLS returns
    nothing for a mission the caller doesn't own (and get_for_user already 404'd that case)."""
    view = _view(rec)
    if rec.kind != "intake":
        return view
    view.available_actions = _INTAKE_ACTIONS.get(rec.phase, [])
    if rec.phase in ("blocked", "failed"):
        view.blocking_reason = rec.error
    source_id = (rec.goal or {}).get("source_id")
    if source_id:
        async with user_session(user_id) as s:
            src = (await s.execute(
                sa_select(schema.Source).where(schema.Source.id == UUID(source_id), schema.Source.user_id == user_id)
            )).scalar_one_or_none()
            if src is not None and src.extracted is not None:
                view.extracted = src.extracted  # already-persisted extraction; no model re-run
    return view


@app.get("/v1/missions/{mission_id}", response_model=MissionView)
async def get_mission(mission_id: UUID, user: AuthenticatedUser = Depends(current_user)) -> MissionView:
    rec = await _mission_repo.get_for_user(mission_id, user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="mission not found")  # 404, never a revealing 403
    return await _enrich_intake(rec, user.user_id)


class PhaseEvent(BaseModel):
    phase: str
    short_status: str | None = None
    at: str


@app.get("/v1/missions/{mission_id}/events", response_model=list[PhaseEvent])
async def mission_events(mission_id: UUID, user: AuthenticatedUser = Depends(current_user)) -> list[PhaseEvent]:
    """Ordered, append-only phase log for polling/debugging. 404 (never 403) for a mission the
    caller doesn't own — enforced by RLS, not just this check."""
    if await _mission_repo.get_for_user(mission_id, user.user_id) is None:
        raise HTTPException(status_code=404, detail="mission not found")
    async with user_session(user.user_id) as s:
        rows = (await s.execute(
            sa_select(schema.MissionPhaseEvent)
            .where(schema.MissionPhaseEvent.mission_id == mission_id, schema.MissionPhaseEvent.user_id == user.user_id)
            .order_by(schema.MissionPhaseEvent.created_at, schema.MissionPhaseEvent.id)
        )).scalars().all()
    return [PhaseEvent(phase=r.phase, short_status=r.short_status, at=r.created_at.isoformat()) for r in rows]


@app.get("/v1/missions", response_model=list[MissionView])
async def list_missions(user: AuthenticatedUser = Depends(current_user)) -> list[MissionView]:
    return [_view(r) for r in await _mission_repo.list_for_user(user.user_id)]


@app.delete("/v1/account")
async def delete_account(user: AuthenticatedUser = Depends(current_user)) -> dict[str, bool]:
    await _user_repo.delete(user.user_id)  # cascade removes all rows the user owns
    return {"deleted": True}


# ------------------------------------------------------- Phase 1 compute endpoints (auth-gated)

class IntakeRequest(BaseModel):
    """Text OR base64 bytes (image/pdf). The request is ACCEPTED and processed asynchronously."""

    text: str | None = Field(default=None)
    content_base64: str | None = Field(default=None)  # for image/pdf source kinds
    mime: str | None = Field(default=None, max_length=64)
    source_kind: IntakeSourceKind = IntakeSourceKind.text
    # Optional: a retry with the same key is idempotent. Omit it and the key is derived from the
    # content itself, so a double-tap in the app is already safe without client cooperation.
    idempotency_key: str | None = Field(default=None, max_length=intake_store.MAX_CLIENT_KEY)


class IntakeAccepted(BaseModel):
    """202 body: the durable mission the client can poll IMMEDIATELY. No extraction has run yet."""

    mission_id: UUID
    source_id: UUID
    state: str  # canonical mission phase, e.g. "understanding"
    display_status: str  # e.g. "Understanding your flyer…"
    poll: dict[str, str]  # URLs the client can GET for canonical state + phase events


@app.post("/v1/intake", status_code=202, response_model=IntakeAccepted)
async def intake(req: IntakeRequest, user: AuthenticatedUser = Depends(current_user)) -> IntakeAccepted:
    """ACCEPT a raw student input and return a durable mission immediately (202).

    Transcription + extraction happen OUTSIDE this request, in a worker. The request only commits
    the durable records (source + mission + job) in a short transaction — no model call is held over
    the connection — so the student sees "Understanding your flyer…" in well under a second, then
    polls GET /v1/missions/{id} for canonical state. user_id comes only from the verified token;
    client-provided user_id is never accepted. A double-tap is idempotent (same key -> same mission).
    """
    await _user_repo.ensure(user.user_id, auth_provider=user.auth_provider)

    input_bytes: bytes | None = None
    if req.source_kind in (IntakeSourceKind.image, IntakeSourceKind.pdf):
        if not req.content_base64:
            raise HTTPException(status_code=422, detail={"error": "missing_content", "reason": "content_base64 required for image/pdf"})
        if req.source_kind is IntakeSourceKind.image and (req.mime or "") not in extraction._SUPPORTED_IMAGE_MIMES:
            # Reject an unreadable image type up front (415, costs no work) — the one read-failure we
            # can know before the worker runs.
            raise HTTPException(status_code=415, detail={"error": "unsupported_source_type", "supported": sorted(extraction._SUPPORTED_IMAGE_MIMES)})
        try:
            input_bytes = base64.b64decode(req.content_base64, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail={"error": "invalid_base64"})
    elif not (req.text and req.text.strip()):
        raise HTTPException(status_code=422, detail={"error": "empty_input", "reason": "text is required"})

    pending = await intake_store.create_pending_intake(
        user_id=user.user_id,
        source_kind=req.source_kind,
        text=req.text if input_bytes is None else None,
        input_bytes=input_bytes,
        mime=req.mime,
        idempotency_key=req.idempotency_key,
    )
    # Wake the private worker via Cloud Tasks (no-op + no error if dispatch isn't configured — the
    # job is already durable, so the in-proc worker or a later drain still handles it).
    await task_dispatch.enqueue_intake(pending.job_id, user.user_id)
    return IntakeAccepted(
        mission_id=pending.mission_id,
        source_id=pending.source_id,
        state=pending.state,
        display_status=pending.display_status,
        poll={
            "mission": f"/v1/missions/{pending.mission_id}",
            "events": f"/v1/missions/{pending.mission_id}/events",
        },
    )


class IntakeResponse(ExtractedIntake):
    """The extraction PLUS the ids it durably created (source_id -> span_ids -> task_ids)."""

    source_id: UUID
    span_ids: list[UUID] = Field(default_factory=list)
    task_ids: list[UUID] = Field(default_factory=list)


@app.post("/v1/intake/sync", response_model=IntakeResponse)
async def intake_sync(req: IntakeRequest, user: AuthenticatedUser = Depends(current_user)) -> IntakeResponse:
    """INTERNAL / legacy synchronous intake — extract + persist in one request (200).

    The student-facing path is the async POST /v1/intake (202). This endpoint is retained for the
    synchronous persist service and its durability guarantees (atomic source->spans->tasks under
    RLS, idempotent replay). Same false-completion contract: a read failure is a typed 415/422, a
    provider outage a 503 — never a 200 with an empty intake."""
    await _user_repo.ensure(user.user_id, auth_provider=user.auth_provider)
    if not (req.text and req.text.strip()):
        raise HTTPException(status_code=422, detail={"error": "empty_input"})
    try:
        result = await _persist_intake(
            user_id=user.user_id, text=req.text, source_kind=req.source_kind,
            extract=extract_from_text,  # resolved here (not at import) so tests can patch it
            idempotency_key=req.idempotency_key,
        )
    except ExtractionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail())
    except ProviderUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.as_detail())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"extraction failed: {type(exc).__name__}")
    return IntakeResponse(
        **result.intake.model_dump(),
        source_id=result.source_id, span_ids=result.span_ids, task_ids=result.task_ids,
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
