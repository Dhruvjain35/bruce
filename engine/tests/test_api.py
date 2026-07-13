"""Offline tests for the FastAPI service (build_outreach_plan patched — no network/keys)."""

import asyncio

from fastapi.testclient import TestClient

import bruce_engine.api as api
from bruce_engine.api import MissionRequest, MissionStatus
from bruce_engine.models import (
    DiscoveryResult,
    OutreachGoal,
    OutreachPlan,
    OutreachType,
    StudentLevel,
    StudentProfile,
)

client = TestClient(api.app)


def _student_goal():
    student = StudentProfile(name="Test", level=StudentLevel.high_school, background="b")
    goal = OutreachGoal(outreach_type=OutreachType.research_position, topic="polariton chemistry")
    return student, goal


def _fake_plan(student, goal) -> OutreachPlan:
    return OutreachPlan(student=student, goal=goal, discovery=DiscoveryResult(goal=goal), drafts=[])


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_get_unknown_mission_404():
    assert client.get("/v1/missions/does-not-exist").status_code == 404


def test_create_mission_returns_running(monkeypatch):
    student, goal = _student_goal()
    # patch so even if the background task runs, it's offline
    async def fake_build(s, g, **k):
        return _fake_plan(s, g)

    monkeypatch.setattr(api, "build_outreach_plan", fake_build)
    r = client.post(
        "/v1/missions",
        json={"student": student.model_dump(mode="json"), "goal": goal.model_dump(mode="json"), "limit": 3},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running" and body["mission_id"]
    assert body["mission_id"] in api._MISSIONS


def test_run_mission_succeeds(monkeypatch):
    student, goal = _student_goal()

    async def fake_build(s, g, **k):
        return _fake_plan(s, g)

    monkeypatch.setattr(api, "build_outreach_plan", fake_build)
    asyncio.run(api._run_mission("m-ok", MissionRequest(student=student, goal=goal, limit=3)))
    state = api._MISSIONS["m-ok"]
    assert state.status == MissionStatus.succeeded and state.plan is not None


def test_run_mission_failure_is_captured(monkeypatch):
    student, goal = _student_goal()

    async def boom(s, g, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(api, "build_outreach_plan", boom)
    asyncio.run(api._run_mission("m-fail", MissionRequest(student=student, goal=goal, limit=3)))
    state = api._MISSIONS["m-fail"]
    assert state.status == MissionStatus.failed and "kaboom" in (state.error or "")
