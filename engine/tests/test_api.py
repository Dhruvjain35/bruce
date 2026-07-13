"""Offline tests for the FastAPI service (build_outreach_plan patched — no network/keys)."""

import asyncio

from fastapi.testclient import TestClient

import bruce_engine.api as api
from bruce_engine.api import MissionRequest, MissionStatus
from bruce_engine.models import (
    DiscoveryResult,
    ExtractedDeadline,
    ExtractedIntake,
    IntakeSourceKind,
    OutreachGoal,
    OutreachPlan,
    OutreachType,
    StudentLevel,
    StudentProfile,
    Task,
    TaskKind,
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


# --- Phase 1 endpoints ---


def test_intake_endpoint(monkeypatch):
    fake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        title="Science Fair",
        deadlines=[ExtractedDeadline(label="Registration", date="2026-02-28", source_span="x", confidence=0.9)],
    )

    async def fake_extract(text, source_kind=IntakeSourceKind.text):
        return fake

    monkeypatch.setattr(api, "extract_from_text", fake_extract)
    r = client.post("/v1/intake", json={"text": "Registration closes February 28, 2026."})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Science Fair" and len(body["deadlines"]) == 1


def test_opportunities_endpoint(monkeypatch):
    student = StudentProfile(name="T", level=StudentLevel.high_school, background="b")
    intake = ExtractedIntake(source_kind=IntakeSourceKind.text, title="Summer REU")
    task = Task(task_id="t1", kind=TaskKind.application, title="Summer REU")

    async def fake_ingest(text, student, **k):
        return {"intake": intake, "classification": "research", "is_spam": False, "fit": None, "task": task}

    monkeypatch.setattr(api, "ingest_opportunity_text", fake_ingest)
    r = client.post(
        "/v1/opportunities",
        json={"text": "REU at MIT, apply by March 1", "student": student.model_dump(mode="json")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["classification"] == "research" and body["is_spam"] is False
    assert body["task"]["title"] == "Summer REU"


def test_tasks_endpoint():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        title="Science Fair",
        deadlines=[ExtractedDeadline(label="Registration", date="2999-02-28", source_span="x", confidence=0.9)],
    )
    r = client.post("/v1/tasks", json={"intakes": [intake.model_dump(mode="json")]})
    assert r.status_code == 200
    body = r.json()
    assert any(t["title"] == "Registration" for t in body["tasks"])
    assert "later" in body["buckets"] and isinstance(body["counts"], dict)


def test_calendar_endpoint():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        deadlines=[ExtractedDeadline(label="Projects due", date="2026-03-14", source_span="x", confidence=0.9)],
    )
    r = client.post("/v1/calendar", json={"intake": intake.model_dump(mode="json")})
    assert r.status_code == 200
    body = r.json()
    assert "BEGIN:VCALENDAR" in body["ics"] and isinstance(body["conflicts"], list)


def test_brief_endpoint():
    task = Task(task_id="t1", kind=TaskKind.deadline, title="Essay", due="2999-01-01")
    r = client.post("/v1/brief", json={"tasks": [task.model_dump(mode="json")], "kind": "morning"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "morning" and isinstance(body["lines"], list) and body["lines"]


def test_mission_create_has_phase(monkeypatch):
    student = StudentProfile(name="T", level=StudentLevel.high_school, background="b")
    goal = OutreachGoal(outreach_type=OutreachType.research_position, topic="x")

    async def fake_build(s, g, **k):
        return OutreachPlan(student=s, goal=g, discovery=DiscoveryResult(goal=g), drafts=[])

    monkeypatch.setattr(api, "build_outreach_plan", fake_build)
    r = client.post(
        "/v1/missions",
        json={"student": student.model_dump(mode="json"), "goal": goal.model_dump(mode="json")},
    )
    assert r.status_code == 200 and r.json()["phase"] == "created"
