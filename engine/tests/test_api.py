"""API tests — authenticated + persistence-backed (in-memory repos injected; no network)."""

import time
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

import bruce_engine.api as api
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
from bruce_engine.intake_store import PersistedIntake
from bruce_engine.repositories import InMemoryMissionRepository, InMemoryStore

SECRET = "test-secret"
client = TestClient(api.app)


class _NoopUserRepo:
    async def ensure(self, user_id, **k):
        return None

    async def delete(self, user_id):
        return None


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setenv("BRUCE_JWT_SECRET", SECRET)
    monkeypatch.delenv("BRUCE_JWKS_URL", raising=False)
    monkeypatch.delenv("BRUCE_JWT_AUDIENCE", raising=False)
    monkeypatch.setattr(api, "_mission_repo", InMemoryMissionRepository(InMemoryStore()))
    monkeypatch.setattr(api, "_user_repo", _NoopUserRepo())

    async def fake_build(student, goal, **k):
        return OutreachPlan(student=student, goal=goal, discovery=DiscoveryResult(goal=goal), drafts=[])

    monkeypatch.setattr(api, "build_outreach_plan", fake_build)


def _auth(uid):
    tok = jwt.encode({"sub": str(uid), "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {tok}"}


def _student():
    return StudentProfile(name="T", level=StudentLevel.high_school, background="b")


def _goal():
    return OutreachGoal(outreach_type=OutreachType.research_position, topic="polariton chemistry")


def test_health_is_public():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "commit" in body and "env" in body   # deployed build identity for smoke tests


def test_mission_requires_auth():
    body = {"student": _student().model_dump(mode="json"), "goal": _goal().model_dump(mode="json")}
    assert client.post("/v1/missions", json=body).status_code == 401


def test_mission_create_get_and_isolation():
    a, b = uuid4(), uuid4()
    body = {"student": _student().model_dump(mode="json"), "goal": _goal().model_dump(mode="json")}
    r = client.post("/v1/missions", json=body, headers=_auth(a))
    assert r.status_code == 200 and r.json()["phase"] == "created"
    mid = r.json()["mission_id"]

    assert client.get(f"/v1/missions/{mid}", headers=_auth(a)).status_code == 200
    assert client.get(f"/v1/missions/{mid}", headers=_auth(b)).status_code == 404  # 404, not 403
    assert client.get(f"/v1/missions/{mid}").status_code == 401  # no token
    assert len(client.get("/v1/missions", headers=_auth(a)).json()) == 1
    assert client.get("/v1/missions", headers=_auth(b)).json() == []


def test_account_delete_requires_auth():
    assert client.delete("/v1/account").status_code == 401
    assert client.delete("/v1/account", headers=_auth(uuid4())).json() == {"deleted": True}


def test_intake_endpoint(monkeypatch):
    """Offline contract check: response SHAPE only. Real persistence is covered against real
    Postgres in test_intake_persistence.py — this suite must not need a database."""
    fake = ExtractedIntake(
        source_kind=IntakeSourceKind.text, title="Science Fair",
        deadlines=[ExtractedDeadline(label="Registration", date="2026-02-28", source_span="x", confidence=0.9)],
    )

    async def fake_extract(text, source_kind=IntakeSourceKind.text):
        return fake

    async def fake_persist(*, user_id, text, source_kind, extract, idempotency_key=None):
        return PersistedIntake(
            intake=await extract(text, source_kind),
            source_id=uuid4(), span_ids=[uuid4()], task_ids=[uuid4()],
        )

    monkeypatch.setattr(api, "extract_from_text", fake_extract)
    monkeypatch.setattr(api, "_persist_intake", fake_persist)
    r = client.post("/v1/intake/sync", json={"text": "Registration closes Feb 28, 2026."}, headers=_auth(uuid4()))
    assert r.status_code == 200 and r.json()["title"] == "Science Fair"
    # additive contract: the extraction still sits at its original paths, ids are new
    assert r.json()["deadlines"][0]["label"] == "Registration"
    assert r.json()["source_id"] and len(r.json()["task_ids"]) == 1
    assert client.post("/v1/intake/sync", json={"text": "x"}).status_code == 401  # auth required


def test_intake_failure_leaks_no_content(monkeypatch):
    """A failed extraction must surface the exception TYPE only — never the student's raw text."""
    secret = "SECRET essay text and parent phone 555-0100"

    async def boom(*a, **k):
        raise ValueError(secret)

    monkeypatch.setattr(api, "_persist_intake", boom)
    r = client.post("/v1/intake/sync", json={"text": secret}, headers=_auth(uuid4()))
    assert r.status_code == 502
    assert secret not in r.text and "555-0100" not in r.text
    assert r.json()["detail"] == "extraction failed: ValueError"


def test_opportunities_endpoint(monkeypatch):
    intake = ExtractedIntake(source_kind=IntakeSourceKind.text, title="Summer REU")
    task = Task(task_id="t1", kind=TaskKind.application, title="Summer REU")

    async def fake_ingest(text, student, **k):
        return {"intake": intake, "classification": "research", "is_spam": False, "fit": None, "task": task}

    monkeypatch.setattr(api, "ingest_opportunity_text", fake_ingest)
    r = client.post(
        "/v1/opportunities",
        json={"text": "REU at MIT", "student": _student().model_dump(mode="json")},
        headers=_auth(uuid4()),
    )
    assert r.status_code == 200 and r.json()["classification"] == "research"


def test_tasks_endpoint():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text, title="Fair",
        deadlines=[ExtractedDeadline(label="Registration", date="2999-02-28", source_span="x", confidence=0.9)],
    )
    r = client.post("/v1/tasks", json={"intakes": [intake.model_dump(mode="json")]}, headers=_auth(uuid4()))
    assert r.status_code == 200 and any(t["title"] == "Registration" for t in r.json()["tasks"])


def test_calendar_endpoint():
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        deadlines=[ExtractedDeadline(label="Projects due", date="2026-03-14", source_span="x", confidence=0.9)],
    )
    r = client.post("/v1/calendar", json={"intake": intake.model_dump(mode="json")}, headers=_auth(uuid4()))
    assert r.status_code == 200 and "BEGIN:VCALENDAR" in r.json()["ics"]


def test_brief_endpoint():
    task = Task(task_id="t1", kind=TaskKind.deadline, title="Essay", due="2999-01-01")
    r = client.post("/v1/brief", json={"tasks": [task.model_dump(mode="json")], "kind": "morning"}, headers=_auth(uuid4()))
    assert r.status_code == 200 and r.json()["kind"] == "morning" and r.json()["lines"]
