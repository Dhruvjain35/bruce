"""A1 mission kernel against REAL Postgres — the durable-agency merge bar.

Proves: mission + first phase event created in ONE transaction (both present, both owner-scoped);
idempotent on (owner, source message, capability) so a redelivery/re-handoff REFERENCES not duplicates;
source/attachment/evidence/autonomy/risk/goal linked in goal JSONB; tenant isolation under RLS; and the
persisted state read that backs "what are u doing with that?". Skips cleanly without Postgres.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import mission_kernel, schema
from bruce_engine.db import user_session

import pytest


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


async def _user():
    uid = uuid4()
    async with user_session(uid) as s:
        s.add(schema.User(id=uid, auth_provider="apple"))
    return uid


async def _mission_count(uid):
    async with user_session(uid) as s:
        return (await s.execute(select(func.count()).select_from(schema.Mission).where(
            schema.Mission.user_id == uid))).scalar_one()


async def _event_count(uid, mission_id):
    async with user_session(uid) as s:
        return (await s.execute(select(func.count()).select_from(schema.MissionPhaseEvent).where(
            schema.MissionPhaseEvent.mission_id == mission_id))).scalar_one()


def test_create_makes_one_mission_and_one_phase_event_atomically():
    uid = _run(_user())
    r = _run(mission_kernel.create_handoff_mission(
        uid, capability="student_task_capture", source_message_id="m1",
        proposed_goal="register for the hackathon", short_status="tracking: register for the hackathon",
        attachment_refs=[{"media_type": "image/heic", "filename": "flyer.heic"}],
        evidence={"reply_to_message_id": "m0"}))
    assert r.created is True and r.phase == "understanding"
    assert _run(_mission_count(uid)) == 1
    assert _run(_event_count(uid, r.mission_id)) == 1               # first phase event committed WITH the mission


def test_links_source_attachment_evidence_autonomy_risk_goal():
    uid = _run(_user())
    r = _run(mission_kernel.create_handoff_mission(
        uid, capability="student_task_capture", source_message_id="m7",
        proposed_goal="do the field trip form", short_status="tracking: field trip form",
        autonomy="A1", risk="low",
        attachment_refs=[{"media_type": "application/pdf", "filename": "form.pdf"}],
        evidence={"reply_to_message_id": "m6", "has_referenced_context": True}))
    state = _run(mission_kernel.get_mission_state(uid, r.mission_id))
    g = state["goal"]
    assert g["capability"] == "student_task_capture" and g["proposed_goal"] == "do the field trip form"
    assert g["source_message_ids"] == ["m7"] and g["source_attachment_refs"][0]["filename"] == "form.pdf"
    assert g["autonomy"] == "A1" and g["risk"] == "low"
    assert g["evidence"]["reply_to_message_id"] == "m6"
    assert state["kind"] == "handoff" and state["phase"] == "understanding"


def test_idempotent_same_source_and_capability_references_not_duplicates():
    uid = _run(_user())
    a = _run(mission_kernel.create_handoff_mission(
        uid, capability="student_task_capture", source_message_id="dup1",
        proposed_goal="x", short_status="tracking: x"))
    b = _run(mission_kernel.create_handoff_mission(
        uid, capability="student_task_capture", source_message_id="dup1",
        proposed_goal="x", short_status="tracking: x"))
    assert a.created is True and b.created is False               # 2nd call REFERENCED the 1st
    assert a.mission_id == b.mission_id
    assert _run(_mission_count(uid)) == 1                          # no duplicate
    assert _run(_event_count(uid, a.mission_id)) == 1             # no duplicate phase event


def test_different_capability_same_source_is_a_distinct_mission():
    uid = _run(_user())
    a = _run(mission_kernel.create_handoff_mission(
        uid, capability="student_task_capture", source_message_id="s1", proposed_goal="x", short_status="x"))
    b = _run(mission_kernel.create_handoff_mission(
        uid, capability="calendar_capture", source_message_id="s1", proposed_goal="x", short_status="x"))
    assert a.mission_id != b.mission_id and _run(_mission_count(uid)) == 2


def test_tenant_isolation_other_user_cannot_read_the_mission():
    owner = _run(_user())
    other = _run(_user())
    r = _run(mission_kernel.create_handoff_mission(
        owner, capability="student_task_capture", source_message_id="m1", proposed_goal="x", short_status="x"))
    assert _run(mission_kernel.get_mission_state(owner, r.mission_id)) is not None
    assert _run(mission_kernel.get_mission_state(other, r.mission_id)) is None   # RLS blocks cross-tenant


def test_get_mission_state_returns_persisted_phase_events():
    uid = _run(_user())
    r = _run(mission_kernel.create_handoff_mission(
        uid, capability="student_task_capture", source_message_id="m1",
        proposed_goal="the science fair reg", short_status="tracking: the science fair reg"))
    state = _run(mission_kernel.get_mission_state(uid, r.mission_id))
    assert state["status"] == "running" and state["phase"] == "understanding"
    assert len(state["phase_events"]) == 1 and state["phase_events"][0]["phase"] == "understanding"
