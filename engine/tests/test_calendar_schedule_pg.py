"""Real-Postgres proof of the calendar 'schedule this' operation graph — the durable mission, the
honest phase events, the receipt, account backfill/binding, and execute-once idempotency. Uses a
FakeCalendarAdapter (which models Google's 409 + organizer/creator account stamping) so the REAL
mission/receipt/RLS/backfill logic is exercised against Postgres without network or OAuth. NOT mocked
at the DB layer. Skips without Postgres.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import calendar_adapter, calendar_schedule, crypto, mission_kernel, oauth_google, schema
from bruce_engine.db import user_session
from bruce_engine.models import CalendarEvent
from bruce_engine.repositories import PostgresUserRepository

KEY = crypto.generate_key()
ACCOUNT = "founder@example.com"
users = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _env(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None), _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    monkeypatch.setenv("BRUCE_ENCRYPTION_KEY", KEY)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _event():
    return CalendarEvent(title="Startup School 2026", start="2026-07-25", end="2026-07-27",
                         location="Chase Center, San Francisco", tentative=False)


async def _seed_user(uid, *, connected=True, account=None):
    await users.ensure(uid, auth_provider="test")             # user committed first (FK parent)
    if connected:
        async with user_session(uid) as s:
            s.add(schema.Integration(
                user_id=uid, provider=oauth_google.PROVIDER,
                provider_account_id=account, scopes=["https://www.googleapis.com/auth/calendar.events"],
                refresh_token_encrypted=crypto.encrypt("rt-secret"),
                selected_calendar_id="primary", status="connected"))


async def _mission(uid, src="pmid-1"):
    return (await mission_kernel.create_handoff_mission(
        uid, capability=calendar_schedule._CAPABILITY, source_message_id=src,
        proposed_goal="add Startup School", short_status="add to calendar")).mission_id


async def _phase_statuses(uid, mid):
    async with user_session(uid) as s:
        rows = (await s.execute(select(schema.MissionPhaseEvent.short_status).where(
            schema.MissionPhaseEvent.mission_id == mid).order_by(
            schema.MissionPhaseEvent.created_at))).scalars().all()
    return list(rows)


async def _receipt(uid, mid):
    async with user_session(uid) as s:
        return (await s.execute(select(schema.Receipt).where(
            schema.Receipt.mission_id == mid))).scalars().all()


def test_verified_write_backfills_account_and_marks_mission_succeeded(clean_db):
    uid = uuid4()
    _run(_seed_user(uid, connected=True, account=None))       # calendar.events-only: account unknown
    mid = _run(_mission(uid))
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)

    res = _run(calendar_schedule.schedule_event(
        uid, mid, _event(), source_message_id="pmid-1", attachment_digest="dig", adapter=adapter))

    assert res.state is calendar_schedule.ScheduleState.verified
    assert res.account == ACCOUNT
    # mission is only 'succeeded' after the read-back verified
    state = _run(mission_kernel.get_mission_state(uid, mid))
    assert state["status"] == "succeeded" and state["phase"] == "succeeded"
    # honest states are all durably recorded, in order
    statuses = _run(_phase_statuses(uid, mid))
    for s in ("prepared", "creation_attempted", "fetched_back", "verified"):
        assert s in statuses
    # account learned from the authoritative event record and bound to the integration
    integ = _run(oauth_google.get_integration(uid))
    assert integ.provider_account_id == ACCOUNT
    # a verified receipt carries the read-back evidence
    receipts = _run(_receipt(uid, mid))
    assert len(receipts) == 1 and receipts[0].outcome == "verified"
    assert receipts[0].evidence["verified"] is True and receipts[0].evidence["account"] == ACCOUNT


def test_redelivery_is_execute_once_no_duplicate(clean_db):
    uid = uuid4()
    _run(_seed_user(uid, connected=True, account=ACCOUNT))
    mid = _run(_mission(uid))
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)

    r1 = _run(calendar_schedule.schedule_event(
        uid, mid, _event(), source_message_id="pmid-1", attachment_digest="dig", adapter=adapter))
    r2 = _run(calendar_schedule.schedule_event(          # same adapter instance == same provider state
        uid, mid, _event(), source_message_id="pmid-1", attachment_digest="dig", adapter=adapter))

    assert r1.state is r2.state is calendar_schedule.ScheduleState.verified
    assert r1.event_id == r2.event_id
    assert len(adapter.events) == 1                      # the 409 held: exactly ONE event exists
    assert adapter.insert_calls == 2                     # both attempts tried; the second was rejected


def test_not_connected_writes_nothing_and_is_honest(clean_db):
    uid = uuid4()
    _run(_seed_user(uid, connected=False))
    mid = _run(_mission(uid))
    adapter = calendar_adapter.FakeCalendarAdapter(account=ACCOUNT)

    res = _run(calendar_schedule.schedule_event(
        uid, mid, _event(), source_message_id="pmid-1", adapter=adapter))

    assert res.state is calendar_schedule.ScheduleState.not_connected
    assert adapter.insert_calls == 0                     # never touched the provider
    state = _run(mission_kernel.get_mission_state(uid, mid))
    assert state["status"] == "running" and state["phase"] == "blocked"
    receipts = _run(_receipt(uid, mid))
    assert receipts[0].outcome == "not_connected"


def test_wrong_account_read_back_is_inconclusive_never_succeeds(clean_db):
    uid = uuid4()
    _run(_seed_user(uid, connected=True, account="bound@example.com"))   # bound to account A
    mid = _run(_mission(uid))
    adapter = calendar_adapter.FakeCalendarAdapter(account="other@example.com")  # provider stamps B

    res = _run(calendar_schedule.schedule_event(
        uid, mid, _event(), source_message_id="pmid-1", adapter=adapter))

    assert res.state is calendar_schedule.ScheduleState.verification_inconclusive
    state = _run(mission_kernel.get_mission_state(uid, mid))
    assert state["status"] != "succeeded"               # a wrong-account write must NEVER be 'done'
    # the bound account is not overwritten by the mismatching one
    integ = _run(oauth_google.get_integration(uid))
    assert integ.provider_account_id == "bound@example.com"
