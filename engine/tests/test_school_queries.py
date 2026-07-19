"""The four student queries over the PERSISTED canonical graph, against REAL Postgres (under RLS).

Syncs the Canvas fake into the tenant-scoped tables for one user, then exercises "what's due / what
changed / what am I missing / what should I do next" and asserts that EVERY returned item carries its full
provenance (provider id, original URL, source + last-sync timestamps, evidence link, change history,
capability state). Skips cleanly when Postgres isn't configured (via ``pg_test_db``).
"""

from __future__ import annotations

import asyncio
import datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import school_queries, school_store
from bruce_engine import school_connector as sc
from bruce_engine.canvas_fake import CanvasFakeConnector
from bruce_engine.repositories import PostgresUserRepository

NOW = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.timezone.utc)
users_repo = PostgresUserRepository()


@pytest.fixture(autouse=True)
def _null_pool_engine(pg_test_db, monkeypatch):
    def _factory(url, **kw):
        kw.pop("poolclass", None)
        return _real_create_async_engine(url, poolclass=NullPool, **kw)

    monkeypatch.setattr(db, "create_async_engine", _factory)
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(coro):
    return asyncio.run(coro)


async def _seed(uid) -> school_store.SyncSummary:
    await users_repo.ensure(uid)
    return await school_store.sync_provider(CanvasFakeConnector(), uid)


def _assert_full_provenance(prov: sc.Provenance) -> None:
    """The mandatory provenance contract for every item a query returns."""
    assert prov.provider == "canvas"
    assert prov.provider_id                                   # provider ID
    assert prov.source_url and prov.source_url.startswith("http")  # original URL
    assert prov.source_timestamp is not None                 # source timestamp
    assert prov.last_synced_at is not None                   # last-sync time
    assert prov.evidence is not None and prov.evidence.source_id  # evidence (source/source_span link)
    assert prov.evidence.span_text                            # a verbatim grounding span was preserved
    assert prov.capability_state is not None                 # capability state it was produced under
    assert isinstance(prov.changes, list) and prov.changes   # change history (>=1: at least the create)


# --------------------------------------------------------------------------- sync populates the graph


def test_sync_populates_the_canonical_graph_idempotently(clean_db):
    async def run():
        uid = uuid4()
        s1 = await _seed(uid)
        assert s1.created > 0 and s1.deleted == 0
        assert "schedule_events" in s1.unsupported  # honest: Canvas ICS feed not connected
        courses = await school_store.list_courses(uid)
        assignments = await school_store.list_assignments(uid)
        assert len(courses) == 3 and len(assignments) == 7
        # A second sync with no provider changes creates/updates/deletes NOTHING — even when the sync
        # CLOCK has moved (a real adapter stamps last_synced_at = now each run). This guards against
        # folded sub-object provenance (a course's instructors, a submission's rubric) leaking the sync
        # clock into the content hash and spuriously marking everything 'updated' every sync.
        later = CanvasFakeConnector(synced_at=datetime.datetime(2026, 3, 2, 9, 0, tzinfo=datetime.timezone.utc))
        s2 = await school_store.sync_provider(later, uid)
        assert s2.created == 0 and s2.updated == 0 and s2.deleted == 0
        assert len(await school_store.list_assignments(uid)) == 7
    _run(run())


# --------------------------------------------------------------------------- 1. what's due


def test_what_is_due_returns_upcoming_with_full_provenance(clean_db):
    async def run():
        uid = uuid4()
        await _seed(uid)
        due = await school_queries.what_is_due(uid, now=NOW)
        names = sorted(i.assignment.name for i in due)
        assert names == ["DBQ Essay: Reconstruction", "Midterm Exam"]  # the 2 upcoming, unsubmitted
        for item in due:
            assert item.bucket == "upcoming" and item.reason
            _assert_full_provenance(item.provenance)
            assert item.assignment.due_at is not None and item.assignment.due_at >= NOW
    _run(run())


# --------------------------------------------------------------------------- 2. what changed


def test_what_changed_reports_creates_updates_deletes_with_provenance(clean_db):
    async def run():
        uid = uuid4()
        await _seed(uid)
        first = await school_queries.what_changed(uid)
        assert first and all(i.change_type is sc.ChangeType.created for i in first)  # initial load = all new
        for i in first:
            assert i.reason and i.source_url and i.provenance is not None
            _assert_full_provenance(i.provenance)

        # a teacher edits Canvas after the sync, then we re-sync
        conn = CanvasFakeConnector()
        conn.add_assignment(course_id=101, name="Pop Quiz", provider_id=1099,
                            at=datetime.datetime(2026, 3, 6, tzinfo=datetime.timezone.utc), due_at="2026-03-30T23:59:00Z")
        conn.update_assignment(1001, at=datetime.datetime(2026, 3, 6, 1, tzinfo=datetime.timezone.utc),
                               name="DBQ Essay: Reconstruction (revised)")
        conn.delete_assignment(1002, at=datetime.datetime(2026, 3, 6, 2, tzinfo=datetime.timezone.utc))
        await school_store.sync_provider(conn, uid)

        recent = await school_queries.what_changed(uid)
        by_pid: dict[tuple[str, str], list] = {}
        for i in recent:
            by_pid.setdefault((i.object_type, i.provider_id), []).append(i)
        # the newly-added assignment appears as a create, resolving to a live object with provenance
        created = [i for i in by_pid[("assignment", "1099")] if i.change_type is sc.ChangeType.created]
        assert created and created[0].object is not None
        _assert_full_provenance(created[0].provenance)
        # the edited assignment has an UPDATE record naming the changed field (full history is kept)
        upd = [i for i in by_pid[("assignment", "1001")] if i.change_type is sc.ChangeType.updated]
        assert upd and "name" in upd[0].changed_fields
        # the deleted object is honest: no live object, but its id + original URL + delete record remain
        deleted = [i for i in by_pid[("assignment", "1002")] if i.change_type is sc.ChangeType.deleted]
        assert deleted and deleted[0].object is None and deleted[0].source_url
    _run(run())


# --------------------------------------------------------------------------- 3. what am I missing


def test_what_am_i_missing_is_overdue_plus_undated_unsubmitted(clean_db):
    async def run():
        uid = uuid4()
        await _seed(uid)
        missing = await school_queries.what_am_i_missing(uid, now=NOW)
        buckets = {i.assignment.name: i.bucket for i in missing}
        assert buckets.get("Reading Quiz 4") == "overdue"          # past-due + unsubmitted
        assert buckets.get("Extra Credit Reflection") == "undated"  # no due date + unsubmitted
        # graded/submitted work is NEVER "missing"
        assert "Unit 5 Test" not in buckets and "Problem Set 7" not in buckets
        for item in missing:
            _assert_full_provenance(item.provenance)
    _run(run())


# --------------------------------------------------------------------------- 4. what should I do next


def test_what_should_i_do_next_is_prioritized_and_grounded(clean_db):
    async def run():
        uid = uuid4()
        await _seed(uid)
        nxt = await school_queries.what_should_i_do_next(uid, now=NOW)
        assert nxt, "there is actionable work in the fixture"
        # overdue work is ranked first
        assert nxt[0].assignment.name == "Reading Quiz 4"
        for item in nxt:
            assert item.reason
            _assert_full_provenance(item.provenance)
    _run(run())


# --------------------------------------------------------------------------- unsupported note


def test_unsupported_capabilities_are_surfaced_not_hidden(clean_db):
    async def run():
        uid = uuid4()
        summary = await _seed(uid)
        note = school_queries.unsupported_note(summary)
        assert note is not None and "schedule_events" in note
    _run(run())
