"""FastAPI service — exposes the Bruce engine as an async mission API.

The iOS client (and any surface) calls this. A full outreach mission takes minutes (discovery +
email + drafting + verification), so missions run in the background: the client POSTs a mission,
gets an id immediately, then polls status. In-memory store for now — swap for Postgres when
persistence/auth land (docs/product-roadmap.md, Phase 2). The engine holds all provider keys
server-side; the client never does.

Run:  PYTHONPATH=. python scripts/run_api.py   (loads engine/.env, starts uvicorn)
"""

from __future__ import annotations

import asyncio
import uuid
from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .models import OutreachGoal, OutreachPlan, StudentProfile
from .pipeline import build_outreach_plan

app = FastAPI(title="Bruce Engine API", version="0.1.0")


class MissionStatus(str, Enum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class MissionRequest(BaseModel):
    student: StudentProfile
    goal: OutreachGoal
    limit: int = Field(default=6, ge=1, le=20)


class MissionCreated(BaseModel):
    mission_id: str
    status: MissionStatus


class MissionState(BaseModel):
    mission_id: str
    status: MissionStatus
    error: str | None = None
    plan: OutreachPlan | None = None


# In-memory mission store (v1). Replace with Postgres + tenant isolation before real users.
_MISSIONS: dict[str, MissionState] = {}


async def _run_mission(mission_id: str, req: MissionRequest) -> None:
    try:
        plan = await build_outreach_plan(req.student, req.goal, limit=req.limit)
        _MISSIONS[mission_id] = MissionState(
            mission_id=mission_id, status=MissionStatus.succeeded, plan=plan
        )
    except Exception as exc:  # surface failure as mission state; never crash the server
        _MISSIONS[mission_id] = MissionState(
            mission_id=mission_id, status=MissionStatus.failed, error=f"{type(exc).__name__}: {exc}"
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/missions", response_model=MissionCreated)
async def create_mission(req: MissionRequest) -> MissionCreated:
    mission_id = uuid.uuid4().hex
    _MISSIONS[mission_id] = MissionState(mission_id=mission_id, status=MissionStatus.running)
    asyncio.create_task(_run_mission(mission_id, req))
    return MissionCreated(mission_id=mission_id, status=MissionStatus.running)


@app.get("/v1/missions/{mission_id}", response_model=MissionState)
async def get_mission(mission_id: str) -> MissionState:
    state = _MISSIONS.get(mission_id)
    if state is None:
        raise HTTPException(status_code=404, detail="mission not found")
    return state
