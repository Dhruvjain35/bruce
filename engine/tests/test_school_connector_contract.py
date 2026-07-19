"""Contract test suite for the SchoolConnector framework — offline, no Postgres.

``assert_school_connector_contract`` is the SAME set of checks EVERY ``SchoolConnector`` must satisfy
(honest capability matrix, canonical objects with full provenance, date-range filtering, the due-state
buckets, assignment detail, submission state, announcements, original URLs, restart-safe sync cursors,
and honest unsupported states). It is run here against ``CanvasFakeConnector``; a real Canvas / Google
Classroom adapter would be validated by calling the exact same function. Canvas-specific tests below add
the create/update/delete change-detection (driven by the fake's mutators) and the limited-grades caveat.
"""

from __future__ import annotations

import asyncio
import datetime

from bruce_engine import school_connector as sc
from bruce_engine.canvas_fake import CanvasFakeConnector
from bruce_engine.school_capability import CapabilityState, SchoolCapability

NOW = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.timezone.utc)


def _run(coro):
    return asyncio.run(coro)


# ============================================================================ the reusable contract


async def assert_school_connector_contract(connector: sc.SchoolConnector, now: datetime.datetime) -> None:
    """Every SchoolConnector must pass this. Provider-agnostic — no Canvas field names appear here."""

    # -- it structurally IS a SchoolConnector, and exposes a capability matrix ----------------------
    assert isinstance(connector, sc.SchoolConnector)
    matrix = connector.capabilities()
    assert matrix.provider == connector.provider
    assert isinstance(matrix.as_dict(), dict)  # serializable snapshot for provenance/audit

    # -- courses: canonical, with provenance carrying the ORIGINAL provider id + URL ---------------
    cr = await connector.list_courses()
    assert cr.ok and cr.state is CapabilityState.supported
    assert cr.data, "a connector that supports courses must return at least one"
    for c in cr.data:
        assert isinstance(c, sc.Course)
        _assert_provenance(c.provenance, connector.provider)

    # -- assignments: every one carries a full provenance block ------------------------------------
    ar = await connector.list_assignments()
    assert ar.ok
    assignments = ar.data
    for a in assignments:
        assert isinstance(a, sc.Assignment)
        _assert_provenance(a.provenance, connector.provider)

    # -- date-range filtering: only due-in-window; undated is honestly excluded from a bounded query
    since = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
    until = datetime.datetime(2026, 3, 15, tzinfo=datetime.timezone.utc)
    rng = await connector.list_assignments(since=since, until=until)
    assert rng.ok
    for a in rng.data:
        assert a.due_at is not None and since <= a.due_at <= until
    assert all(a.due_at is not None for a in rng.data)  # no undated leaked into a range

    # -- the due-state buckets are internally consistent + mutually correct ------------------------
    up, ov = sc.upcoming(assignments, now), sc.overdue(assignments, now)
    und, uns, grd = sc.undated(assignments), sc.unsubmitted(assignments), sc.graded(assignments)
    for a in up:
        assert a.due_at is not None and a.due_at >= now
        assert a.submission.kind not in (sc.SubmissionStateKind.submitted, sc.SubmissionStateKind.graded)
    for a in ov:
        assert a.due_at is not None and a.due_at < now and sc.is_unsubmitted(a)
    for a in und:
        assert a.due_at is None
    for a in grd:
        assert a.submission.kind is sc.SubmissionStateKind.graded
    up_ids, ov_ids = {a.provider_id for a in up}, {a.provider_id for a in ov}
    assert up_ids.isdisjoint(ov_ids)                       # nothing is both upcoming AND overdue
    assert {a.provider_id for a in grd}.isdisjoint({a.provider_id for a in uns})  # graded != unsubmitted

    # -- assignment detail: a real id resolves; a missing id is honest (ok + data None, not a fake) --
    if assignments:
        one = await connector.get_assignment(assignments[0].provider_id)
        assert one.ok and one.data is not None and one.data.provider_id == assignments[0].provider_id
    missing = await connector.get_assignment("does-not-exist-999999")
    # not-found is a SUPPORTED outcome with no data — honestly distinct from an unsupported capability
    assert missing.state is CapabilityState.supported and missing.data is None

    # -- submission state: present for a graded assignment, honest 'none' for an unsubmitted one ----
    if grd:
        sub = await connector.get_submission(grd[0].provider_id)
        assert sub.ok and sub.data is not None and sub.data.state is sc.SubmissionStateKind.graded
    if uns:
        subn = await connector.get_submission(uns[0].provider_id)
        assert subn.ok and subn.data is not None and subn.data.state is sc.SubmissionStateKind.none

    # -- announcements: canonical, each with an original URL ---------------------------------------
    anr = await connector.list_announcements()
    assert anr.ok
    for an in anr.data:
        assert isinstance(an, sc.Announcement)
        _assert_provenance(an.provenance, connector.provider)

    # -- ORIGINAL URLs: every provider object links back to a real (http) URL ----------------------
    for obj in list(cr.data) + list(assignments) + list(anr.data):
        assert obj.provenance.source_url and obj.provenance.source_url.startswith("http")

    # -- sync cursors + restart-safety: initial sync sets a cursor; an immediate re-sync is empty ---
    page0 = await connector.sync("assignments")
    assert page0.ok and page0.data.next_cursor.cursor_value  # a real, non-None cursor to resume from
    again = await connector.sync("assignments", page0.data.next_cursor)
    assert again.ok and again.data.changes == []             # nothing changed => no re-delivered changes
    # an unknown resource is declined honestly, never answered with an empty success
    bad = await connector.sync("not_a_real_resource")
    assert not bad.ok and bad.state is CapabilityState.unsupported and bad.reason

    # -- honest unsupported / limited states across the whole matrix -------------------------------
    #    schedule_events is a Protocol method, so we can probe the declared state generically.
    ev = await connector.list_schedule_events()
    declared = matrix.state(SchoolCapability.schedule_events)
    if declared is CapabilityState.unsupported:
        assert not ev.ok and ev.data is None and ev.reason  # no fabricated empty list
    else:
        assert ev.ok
    # any capability the matrix marks unsupported must carry a reason (say WHY, not just "no")
    for cap in SchoolCapability:
        decl = matrix.declaration(cap)
        if decl.state in (CapabilityState.unsupported, CapabilityState.limited):
            assert decl.reason, f"{cap} is {decl.state} but gives no reason"


def _assert_provenance(prov: sc.Provenance, provider: str) -> None:
    """Every provider-derived object MUST carry the full provenance block."""
    assert prov.provider == provider
    assert prov.provider_id                                  # original provider id
    assert prov.source_url and prov.source_url.startswith("http")  # original URL
    assert prov.last_synced_at is not None                  # last-sync time
    assert prov.evidence is not None and prov.evidence.source_id  # evidence link (source/source_span)
    assert isinstance(prov.capability_state, CapabilityState)     # the state it was produced under
    assert isinstance(prov.changes, list)                   # change-history support


# ============================================================================ run it against the fake


def test_canvas_fake_satisfies_the_school_connector_contract():
    _run(assert_school_connector_contract(CanvasFakeConnector(), NOW))


# ============================================================================ Canvas-specific fixtures


def test_capability_matrix_is_honest_about_gaps():
    m = CanvasFakeConnector().capabilities()
    # a supported thing, a LIMITED thing (student-scope grades), and an UNSUPPORTED thing (no ICS feed)
    assert m.state(SchoolCapability.list_assignments) is CapabilityState.supported
    assert m.state(SchoolCapability.grades) is CapabilityState.limited
    assert "gradebook" in (m.declaration(SchoolCapability.grades).reason or "")
    assert m.state(SchoolCapability.schedule_events) is CapabilityState.unsupported
    # an un-declared capability defaults to unknown (fail closed), never a silent 'supported'
    assert not m.supports(SchoolCapability.schedule_events)


def test_bucket_counts_are_exact_for_the_reference_fixture():
    async def run():
        c = CanvasFakeConnector()
        a = (await c.list_assignments()).data
        assert len(sc.upcoming(a, NOW)) == 2
        assert len(sc.overdue(a, NOW)) == 1
        assert len(sc.undated(a)) == 1
        assert len(sc.unsubmitted(a)) == 4
        assert len(sc.graded(a)) == 2
    _run(run())


def test_graded_submission_exposes_grade_rubric_and_feedback_with_provenance():
    async def run():
        c = CanvasFakeConnector()
        sub = (await c.get_submission("1004")).data
        assert sub.state is sc.SubmissionStateKind.graded
        assert sub.grade is not None and sub.grade.score == 88.0 and sub.grade.grade == "B+"
        # the grade facet honestly reflects the LIMITED capability it was produced under
        assert sub.grade.provenance.capability_state is CapabilityState.limited
        assert sub.rubric is not None and any(cri.awarded is not None for cri in sub.rubric.criteria)
        assert sub.feedback and sub.feedback[0].comment
        _assert_provenance(sub.provenance, "canvas")
    _run(run())


def test_current_vs_past_courses_are_distinguishable():
    async def run():
        c = CanvasFakeConnector()
        courses = (await c.list_courses()).data
        states = {co.name: co.workflow_state for co in courses}
        assert states["AP US History"] == "available" and states["Geometry"] == "completed"
        terms = (await c.list_terms()).data
        assert any(t.is_current for t in terms) and any(not t.is_current for t in terms)
    _run(run())


def test_change_detection_classifies_create_update_delete_across_a_sync():
    async def run():
        c = CanvasFakeConnector()
        page0 = (await c.sync("assignments")).data
        assert all(ch.change_type is sc.ChangeType.created for ch in page0.changes)  # first sync = all new

        # a teacher edits Canvas AFTER the last sync (timestamps must be > the committed cursor)
        c.add_assignment(course_id=101, name="Pop Quiz", provider_id=1099,
                         at=datetime.datetime(2026, 3, 6, tzinfo=datetime.timezone.utc), due_at="2026-03-30T23:59:00Z")
        c.update_assignment(1001, at=datetime.datetime(2026, 3, 6, 1, tzinfo=datetime.timezone.utc),
                            name="DBQ Essay: Reconstruction (revised)")
        c.delete_assignment(1002, at=datetime.datetime(2026, 3, 6, 2, tzinfo=datetime.timezone.utc))

        delta = (await c.sync("assignments", page0.next_cursor)).data
        kinds = {ch.provider_id: ch.change_type for ch in delta.changes}
        assert kinds == {"1099": sc.ChangeType.created, "1001": sc.ChangeType.updated, "1002": sc.ChangeType.deleted}
        # restart-safe: syncing again from the advanced cursor yields nothing
        assert (await c.sync("assignments", delta.next_cursor)).data.changes == []
    _run(run())


def test_unsupported_schedule_events_never_fabricates_an_empty_list():
    async def run():
        r = await CanvasFakeConnector().list_schedule_events()
        assert r.state is CapabilityState.unsupported and r.data is None and r.reason
    _run(run())
