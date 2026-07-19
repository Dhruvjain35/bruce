"""Canvas FAKE adapter — a fully in-memory ``SchoolConnector`` over Canvas-shaped fixtures.

WHY A FAKE, AND WHY NOW: the repo has no Canvas OAuth client, no institution agreement, and no founder
credentials to build against — inventing a real OAuth flow would be confident fiction, and running one
would touch a real student's data. So, exactly like ``FakeChannel`` in the messaging domain, this adapter
models the parts of Canvas that MATTER — its object shapes, its ``html_url`` links, its ``updated_since``
incremental sync, and object create/update/delete — deterministically and offline. It makes the whole
SchoolConnector path testable today and lets a real Canvas adapter drop in later behind the SAME Protocol.

This is the ONLY module in the framework that knows Canvas field names (``html_url``, ``workflow_state``,
``enrollment_term_id``, ``submission_comments`` …). Provider shapes STOP here: every public method returns
CANONICAL objects (``school_connector``), never a Canvas dict. Nothing real happens — no network, no token,
no OAuth. Do NOT describe Canvas as functional until a real read has passed through a real adapter.
"""

from __future__ import annotations

import copy
import datetime

from . import school_connector as sc
from .school_capability import (
    CapabilityDeclaration,
    CapabilityState,
    ProviderCapabilityMatrix,
    SchoolCapability,
)

PROVIDER = "canvas"
# A deliberately fake host — this adapter never talks to it. Links are shaped like real Canvas URLs so a
# student *could* open them against a real instance, but nothing here resolves them.
BASE_URL = "https://canvas.example.edu"
# Deterministic default sync time for reads, so provenance (last_synced_at) is stable in tests.
DEFAULT_SYNCED_AT = datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.timezone.utc)


def _dt(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------------
# fixtures — Canvas-shaped raw dicts. A factory so tests can build isolated datasets.
# ---------------------------------------------------------------------------------------------------


def default_canvas_fixtures() -> dict:
    """A small, realistic Canvas dataset for one student across two current courses (+ one past course).

    Timestamps are chosen so that, evaluated at ``now = 2026-03-01T12:00Z``, the due-buckets are
    unambiguous: 2 upcoming, 1 overdue, 1 undated, 4 unsubmitted, 2 graded, 1 submitted-not-graded.
    """
    setup = "2026-01-15T00:00:00Z"  # course-setup time; created_at/updated_at unless noted
    return {
        "account": {"id": 1, "name": "Northgate Unified School District"},
        "terms": [
            {"id": 1, "name": "Fall 2025", "start_at": "2025-08-20T00:00:00Z", "end_at": "2025-12-19T00:00:00Z", "is_current": False},
            {"id": 2, "name": "Spring 2026", "start_at": "2026-01-12T00:00:00Z", "end_at": "2026-05-22T00:00:00Z", "is_current": True},
        ],
        "courses": [
            {"id": 101, "name": "AP US History", "course_code": "APUSH", "workflow_state": "available",
             "enrollment_term_id": 2, "account_id": 1, "created_at": setup, "updated_at": setup,
             "teachers": [{"id": 5001, "display_name": "Ms. Rivera", "email": "rivera@example.edu", "type": "TeacherEnrollment"}],
             "sections": [{"id": 7001, "name": "Period 3"}]},
            {"id": 102, "name": "AP Calculus BC", "course_code": "CALCBC", "workflow_state": "available",
             "enrollment_term_id": 2, "account_id": 1, "created_at": setup, "updated_at": setup,
             "teachers": [{"id": 5002, "display_name": "Mr. Chen", "email": "chen@example.edu", "type": "TeacherEnrollment"}],
             "sections": [{"id": 7002, "name": "Period 5"}]},
            {"id": 99, "name": "Geometry", "course_code": "GEOM", "workflow_state": "completed",
             "enrollment_term_id": 1, "account_id": 1, "created_at": "2025-08-20T00:00:00Z", "updated_at": "2025-12-19T00:00:00Z",
             "teachers": [{"id": 5003, "display_name": "Mr. Okafor", "email": "okafor@example.edu", "type": "TeacherEnrollment"}],
             "sections": [{"id": 7003, "name": "Period 1"}]},
        ],
        # Each assignment carries a Canvas ``submission`` include (the signed-in student's state).
        "assignments": [
            {"id": 1001, "course_id": 101, "name": "DBQ Essay: Reconstruction",
             "description": "Write a document-based essay on Reconstruction.", "due_at": "2026-03-10T23:59:00Z",
             "unlock_at": "2026-03-01T00:00:00Z", "lock_at": None, "points_possible": 100,
             "submission_types": ["online_upload"], "html_url": f"{BASE_URL}/courses/101/assignments/1001",
             "created_at": setup, "updated_at": setup,
             "submission": {"workflow_state": "unsubmitted", "submitted_at": None, "score": None, "grade": None, "late": False, "missing": False, "attempt": None}},
            {"id": 1002, "course_id": 101, "name": "Reading Quiz 4",
             "description": "Chapter 15 reading quiz.", "due_at": "2026-02-20T23:59:00Z",
             "unlock_at": None, "lock_at": None, "points_possible": 20, "submission_types": ["online_quiz"],
             "html_url": f"{BASE_URL}/courses/101/assignments/1002", "created_at": setup, "updated_at": setup,
             "submission": {"workflow_state": "unsubmitted", "submitted_at": None, "score": None, "grade": None, "late": False, "missing": True, "attempt": None}},
            {"id": 1003, "course_id": 101, "name": "Extra Credit Reflection",
             "description": "Optional reflection — no due date.", "due_at": None, "unlock_at": None, "lock_at": None,
             "points_possible": 10, "submission_types": ["online_text_entry"],
             "html_url": f"{BASE_URL}/courses/101/assignments/1003", "created_at": setup, "updated_at": setup,
             "submission": {"workflow_state": "unsubmitted", "submitted_at": None, "score": None, "grade": None, "late": False, "missing": False, "attempt": None}},
            {"id": 1004, "course_id": 101, "name": "Unit 5 Test",
             "description": "Unit 5 exam.", "due_at": "2026-02-15T23:59:00Z", "unlock_at": None, "lock_at": None,
             "points_possible": 100, "submission_types": ["on_paper"],
             "html_url": f"{BASE_URL}/courses/101/assignments/1004", "created_at": setup, "updated_at": "2026-02-16T10:00:00Z",
             "submission": {"workflow_state": "graded", "submitted_at": "2026-02-15T20:00:00Z", "score": 88.0, "grade": "B+", "late": False, "missing": False, "attempt": 1}},
            {"id": 1005, "course_id": 102, "name": "Problem Set 7",
             "description": "Integration by parts.", "due_at": "2026-03-05T23:59:00Z", "unlock_at": None, "lock_at": None,
             "points_possible": 30, "submission_types": ["online_upload"],
             "html_url": f"{BASE_URL}/courses/102/assignments/1005", "created_at": setup, "updated_at": "2026-03-04T18:00:00Z",
             "submission": {"workflow_state": "submitted", "submitted_at": "2026-03-04T17:30:00Z", "score": None, "grade": None, "late": False, "missing": False, "attempt": 1}},
            {"id": 1006, "course_id": 102, "name": "Derivatives Worksheet",
             "description": "Worksheet on derivatives.", "due_at": "2026-02-25T23:59:00Z", "unlock_at": None, "lock_at": None,
             "points_possible": 25, "submission_types": ["online_upload"],
             "html_url": f"{BASE_URL}/courses/102/assignments/1006", "created_at": setup, "updated_at": "2026-02-26T09:00:00Z",
             "submission": {"workflow_state": "graded", "submitted_at": "2026-02-24T22:00:00Z", "score": 95.0, "grade": "A", "late": False, "missing": False, "attempt": 1}},
            {"id": 1007, "course_id": 102, "name": "Midterm Exam",
             "description": "Covers units 1-6.", "due_at": "2026-03-20T23:59:00Z", "unlock_at": None, "lock_at": None,
             "points_possible": 100, "submission_types": ["on_paper"],
             "html_url": f"{BASE_URL}/courses/102/assignments/1007", "created_at": setup, "updated_at": setup,
             "submission": {"workflow_state": "unsubmitted", "submitted_at": None, "score": None, "grade": None, "late": False, "missing": False, "attempt": None}},
        ],
        # Full submission detail (comments + rubric assessment) for the graded/submitted ones.
        "submission_detail": {
            "1004": {"id": 90004, "assignment_id": 1004, "workflow_state": "graded", "submitted_at": "2026-02-15T20:00:00Z",
                     "score": 88.0, "grade": "B+", "late": False, "missing": False, "attempt": 1,
                     "preview_url": f"{BASE_URL}/courses/101/assignments/1004/submissions/1",
                     "submission_comments": [{"author_name": "Ms. Rivera", "comment": "Strong thesis; tighten your evidence in paragraph 3.", "created_at": "2026-02-16T10:00:00Z"}],
                     "rubric": {"title": "DBQ Rubric", "points_possible": 100,
                                "criteria": [{"description": "Thesis", "points": 20}, {"description": "Evidence", "points": 40}, {"description": "Analysis", "points": 40}]},
                     "rubric_assessment": {"Thesis": 20, "Evidence": 30, "Analysis": 38}},
            "1005": {"id": 90005, "assignment_id": 1005, "workflow_state": "submitted", "submitted_at": "2026-03-04T17:30:00Z",
                     "score": None, "grade": None, "late": False, "missing": False, "attempt": 1,
                     "preview_url": f"{BASE_URL}/courses/102/assignments/1005/submissions/1",
                     "submission_comments": [], "rubric": None, "rubric_assessment": None},
            "1006": {"id": 90006, "assignment_id": 1006, "workflow_state": "graded", "submitted_at": "2026-02-24T22:00:00Z",
                     "score": 95.0, "grade": "A", "late": False, "missing": False, "attempt": 1,
                     "preview_url": f"{BASE_URL}/courses/102/assignments/1006/submissions/1",
                     "submission_comments": [{"author_name": "Mr. Chen", "comment": "Nice work.", "created_at": "2026-02-26T09:00:00Z"}],
                     "rubric": None, "rubric_assessment": None},
        },
        "announcements": [
            {"id": 3001, "course_id": 101, "title": "No school Friday", "message": "Reminder: no school this Friday.",
             "posted_at": "2026-02-27T15:00:00Z", "html_url": f"{BASE_URL}/courses/101/discussion_topics/3001",
             "created_at": "2026-02-27T15:00:00Z", "updated_at": "2026-02-27T15:00:00Z"},
            {"id": 3002, "course_id": 101, "title": "DBQ tips posted", "message": "I posted tips for the DBQ essay under Files.",
             "posted_at": "2026-03-01T09:00:00Z", "html_url": f"{BASE_URL}/courses/101/discussion_topics/3002",
             "created_at": "2026-03-01T09:00:00Z", "updated_at": "2026-03-01T09:00:00Z"},
            {"id": 3003, "course_id": 102, "title": "Midterm review session", "message": "Optional review session Thursday at lunch.",
             "posted_at": "2026-02-28T12:00:00Z", "html_url": f"{BASE_URL}/courses/102/discussion_topics/3003",
             "created_at": "2026-02-28T12:00:00Z", "updated_at": "2026-02-28T12:00:00Z"},
        ],
        "materials": [
            {"id": 4001, "course_id": 101, "title": "Reconstruction reading", "type": "File", "url": f"{BASE_URL}/courses/101/files/4001", "created_at": setup, "updated_at": setup},
            {"id": 4002, "course_id": 101, "title": "Unit 5 study guide", "type": "Page", "url": f"{BASE_URL}/courses/101/pages/unit-5-study-guide", "created_at": setup, "updated_at": setup},
            {"id": 4003, "course_id": 102, "title": "Formula sheet", "type": "File", "url": f"{BASE_URL}/courses/102/files/4003", "created_at": setup, "updated_at": setup},
        ],
        # Tombstones: {resource: [{"id":.., "deleted_at": iso}]} — populated by delete_* mutators.
        "deleted": {"assignments": [], "announcements": [], "courses": []},
    }


class CanvasFakeConnector:
    """In-memory ``SchoolConnector`` for Canvas. Implements the full Protocol against fixture data.

    Read methods normalize Canvas dicts into canonical objects. Mutation methods (``add_/update_/delete_``)
    exist so a test can drive a real create/update/delete and prove ``sync`` classifies it — they are the
    fake's stand-in for a teacher editing Canvas between two syncs.
    """

    provider = PROVIDER

    def __init__(self, fixtures: dict | None = None, *, synced_at: datetime.datetime | None = None) -> None:
        self._fx = copy.deepcopy(fixtures or default_canvas_fixtures())
        self.synced_at = synced_at or DEFAULT_SYNCED_AT

    # ------------------------------------------------------------------ capability matrix (honest)

    def capabilities(self) -> ProviderCapabilityMatrix:
        supported = [
            SchoolCapability.institution, SchoolCapability.terms, SchoolCapability.list_courses,
            SchoolCapability.instructors, SchoolCapability.sections, SchoolCapability.list_assignments,
            SchoolCapability.assignment_detail, SchoolCapability.due_date_range, SchoolCapability.upcoming,
            SchoolCapability.overdue, SchoolCapability.undated, SchoolCapability.unsubmitted,
            SchoolCapability.graded, SchoolCapability.submission_state, SchoolCapability.materials,
            SchoolCapability.announcements, SchoolCapability.rubrics, SchoolCapability.feedback,
            SchoolCapability.original_urls, SchoolCapability.sync_cursors, SchoolCapability.change_detection,
        ]
        decls = [CapabilityDeclaration(c, CapabilityState.supported) for c in supported]
        # HONEST caveats — a student token sees its OWN grades, not the class gradebook; and this adapter
        # does not model the Canvas calendar/ICS feed at all. Neither gap is papered over with fake data.
        decls.append(CapabilityDeclaration(
            SchoolCapability.grades, CapabilityState.limited,
            reason="student-scope only: the signed-in student's own scores, not the class-wide gradebook"))
        decls.append(CapabilityDeclaration(
            SchoolCapability.schedule_events, CapabilityState.unsupported,
            reason="Canvas calendar/ICS feed is not connected in this adapter"))
        return ProviderCapabilityMatrix(PROVIDER, decls)

    def supports(self, capability: SchoolCapability) -> bool:
        return self.capabilities().supports(capability)

    # ------------------------------------------------------------------ normalization helpers

    def _prov(self, object_type: str, provider_id: str, *, url: str | None,
              source_timestamp: datetime.datetime | None, cap: SchoolCapability,
              span_text: str | None = None, span_label: str | None = None,
              state: CapabilityState = CapabilityState.supported) -> sc.Provenance:
        """Build the mandatory provenance block. ``evidence`` anchors to a provider-composite source id
        (rewritten to a durable DB id when the store persists it) plus the original URL and a span."""
        evidence = sc.SourceRef(
            source_id=f"{PROVIDER}:{object_type}:{provider_id}", source_url=url,
            span_text=span_text, span_id=None)
        return sc.Provenance(
            provider=PROVIDER, provider_id=provider_id, source_url=url,
            source_timestamp=source_timestamp, last_synced_at=self.synced_at,
            evidence=evidence, capability_state=state, changes=[])

    def _norm_instructor(self, raw: dict, *, url: str | None, ts: datetime.datetime | None) -> sc.Instructor:
        return sc.Instructor(
            provider_id=str(raw["id"]), name=raw.get("display_name") or raw.get("name") or "Unknown",
            email=raw.get("email"), role=raw.get("type"),
            provenance=self._prov("instructor", str(raw["id"]), url=url, source_timestamp=ts,
                                  cap=SchoolCapability.instructors, span_text=raw.get("display_name")))

    def _norm_course(self, raw: dict) -> sc.Course:
        cid = str(raw["id"])
        url = f"{BASE_URL}/courses/{cid}"
        ts = _dt(raw.get("updated_at"))
        term = next((t for t in self._fx["terms"] if t["id"] == raw.get("enrollment_term_id")), None)
        instructors = [self._norm_instructor(t, url=url, ts=ts) for t in raw.get("teachers", [])]
        sections = [sc.Section(
            provider_id=str(s["id"]), name=s["name"], course_provider_id=cid,
            provenance=self._prov("section", str(s["id"]), url=url, source_timestamp=ts,
                                  cap=SchoolCapability.sections, span_text=s["name"]))
            for s in raw.get("sections", [])]
        return sc.Course(
            provider_id=cid, name=raw["name"], course_code=raw.get("course_code"),
            workflow_state=raw.get("workflow_state"),
            term_provider_id=str(term["id"]) if term else None, term_name=term["name"] if term else None,
            institution_provider_id=str(raw.get("account_id")) if raw.get("account_id") else None,
            instructors=instructors, sections=sections, url=url,
            provenance=self._prov("course", cid, url=url, source_timestamp=ts,
                                  cap=SchoolCapability.list_courses, span_text=raw["name"]))

    def _norm_submission_state(self, sub: dict) -> sc.SubmissionState:
        ws = (sub.get("workflow_state") or "").lower()
        kind = {"graded": sc.SubmissionStateKind.graded, "submitted": sc.SubmissionStateKind.submitted,
                "unsubmitted": sc.SubmissionStateKind.none, "pending_review": sc.SubmissionStateKind.submitted
                }.get(ws, sc.SubmissionStateKind.unknown)
        return sc.SubmissionState(
            kind=kind, submitted_at=_dt(sub.get("submitted_at")), score=sub.get("score"),
            grade=sub.get("grade"), late=bool(sub.get("late")), missing=bool(sub.get("missing")))

    def _norm_assignment(self, raw: dict) -> sc.Assignment:
        aid = str(raw["id"])
        url = raw.get("html_url")
        ts = _dt(raw.get("updated_at"))
        due = raw.get("due_at")
        span = f"{raw['name']} — due {due}" if due else f"{raw['name']} — no due date"
        return sc.Assignment(
            provider_id=aid, course_provider_id=str(raw["course_id"]), name=raw["name"],
            description=raw.get("description"), due_at=_dt(due), unlock_at=_dt(raw.get("unlock_at")),
            lock_at=_dt(raw.get("lock_at")), points_possible=raw.get("points_possible"),
            submission_types=list(raw.get("submission_types") or []), url=url,
            submission=self._norm_submission_state(raw.get("submission") or {}),
            provenance=self._prov("assignment", aid, url=url, source_timestamp=ts,
                                  cap=SchoolCapability.list_assignments, span_text=span, span_label="due_at"))

    def _norm_announcement(self, raw: dict) -> sc.Announcement:
        anid = str(raw["id"])
        url = raw.get("html_url")
        ts = _dt(raw.get("updated_at"))
        return sc.Announcement(
            provider_id=anid, course_provider_id=str(raw["course_id"]), title=raw["title"],
            message=raw.get("message"), posted_at=_dt(raw.get("posted_at")), url=url,
            provenance=self._prov("announcement", anid, url=url, source_timestamp=ts,
                                  cap=SchoolCapability.announcements, span_text=(raw.get("message") or "")[:160]))

    def _norm_material(self, raw: dict) -> sc.Material:
        mid = str(raw["id"])
        url = raw.get("url")
        ts = _dt(raw.get("updated_at"))
        kind = {"file": "file", "page": "page", "externalurl": "link"}.get((raw.get("type") or "").lower(), "file")
        return sc.Material(
            provider_id=mid, course_provider_id=str(raw["course_id"]), title=raw["title"], kind=kind, url=url,
            provenance=self._prov("material", mid, url=url, source_timestamp=ts,
                                  cap=SchoolCapability.materials, span_text=raw["title"]))

    def _live_assignments(self) -> list[dict]:
        dead = {str(d["id"]) for d in self._fx["deleted"]["assignments"]}
        return [a for a in self._fx["assignments"] if str(a["id"]) not in dead]

    def _live_announcements(self) -> list[dict]:
        dead = {str(d["id"]) for d in self._fx["deleted"]["announcements"]}
        return [a for a in self._fx["announcements"] if str(a["id"]) not in dead]

    def _live_courses(self) -> list[dict]:
        dead = {str(d["id"]) for d in self._fx["deleted"]["courses"]}
        return [c for c in self._fx["courses"] if str(c["id"]) not in dead]

    # ------------------------------------------------------------------ read methods (canonical out)

    async def get_institution(self) -> sc.ConnectorResult[sc.Institution]:
        acct = self._fx["account"]
        inst = sc.Institution(
            provider_id=str(acct["id"]), name=acct["name"],
            provenance=self._prov("institution", str(acct["id"]), url=f"{BASE_URL}/accounts/{acct['id']}",
                                  source_timestamp=None, cap=SchoolCapability.institution, span_text=acct["name"]))
        return sc.ConnectorResult.supported(SchoolCapability.institution, inst)

    async def list_terms(self) -> sc.ConnectorResult[list[sc.AcademicTerm]]:
        terms = [sc.AcademicTerm(
            provider_id=str(t["id"]), name=t["name"], start_at=_dt(t.get("start_at")),
            end_at=_dt(t.get("end_at")), is_current=bool(t.get("is_current")),
            provenance=self._prov("term", str(t["id"]), url=None, source_timestamp=None,
                                  cap=SchoolCapability.terms, span_text=t["name"]))
            for t in self._fx["terms"]]
        return sc.ConnectorResult.supported(SchoolCapability.terms, terms)

    async def list_courses(self) -> sc.ConnectorResult[list[sc.Course]]:
        return sc.ConnectorResult.supported(
            SchoolCapability.list_courses, [self._norm_course(c) for c in self._live_courses()])

    async def list_instructors(self, *, course_id: str | None = None) -> sc.ConnectorResult[list[sc.Instructor]]:
        out: list[sc.Instructor] = []
        for c in self._live_courses():
            if course_id is not None and str(c["id"]) != str(course_id):
                continue
            url = f"{BASE_URL}/courses/{c['id']}"
            out.extend(self._norm_instructor(t, url=url, ts=_dt(c.get("updated_at"))) for t in c.get("teachers", []))
        return sc.ConnectorResult.supported(SchoolCapability.instructors, out)

    async def list_assignments(
        self, *, course_id: str | None = None,
        since: datetime.datetime | None = None, until: datetime.datetime | None = None,
    ) -> sc.ConnectorResult[list[sc.Assignment]]:
        out: list[sc.Assignment] = []
        for raw in self._live_assignments():
            if course_id is not None and str(raw["course_id"]) != str(course_id):
                continue
            a = self._norm_assignment(raw)
            # A date-range query filters ON the due date; undated assignments have no date to fall in a
            # range, so they are honestly excluded from a bounded query (and surfaced via the undated bucket).
            if (since is not None or until is not None):
                if a.due_at is None:
                    continue
                if since is not None and a.due_at < since:
                    continue
                if until is not None and a.due_at > until:
                    continue
            out.append(a)
        cap = SchoolCapability.due_date_range if (since or until) else SchoolCapability.list_assignments
        return sc.ConnectorResult.supported(cap, out)

    async def get_assignment(self, assignment_id: str) -> sc.ConnectorResult[sc.Assignment | None]:
        raw = next((a for a in self._live_assignments() if str(a["id"]) == str(assignment_id)), None)
        data = self._norm_assignment(raw) if raw is not None else None
        return sc.ConnectorResult.supported(SchoolCapability.assignment_detail, data)

    async def get_submission(self, assignment_id: str) -> sc.ConnectorResult[sc.Submission | None]:
        assignment = next((a for a in self._live_assignments() if str(a["id"]) == str(assignment_id)), None)
        if assignment is None:
            return sc.ConnectorResult.supported(SchoolCapability.submission_state, None)
        aid = str(assignment_id)
        detail = self._fx["submission_detail"].get(aid)
        if detail is None:
            # No detailed record: fall back to the assignment's include=submission state (honest: an
            # unsubmitted assignment still HAS a submission object in Canvas, just an empty one).
            state = self._norm_submission_state(assignment.get("submission") or {})
            url = assignment.get("html_url")
            prov = self._prov("submission", aid, url=url, source_timestamp=_dt(assignment.get("updated_at")),
                              cap=SchoolCapability.submission_state)
            sub = sc.Submission(provider_id=f"sub-{aid}", assignment_provider_id=aid, state=state.kind,
                                submitted_at=state.submitted_at, late=state.late, missing=state.missing,
                                url=url, provenance=prov)
            return sc.ConnectorResult.supported(SchoolCapability.submission_state, sub)

        url = detail.get("preview_url")
        ts = _dt(detail.get("submitted_at"))
        sid = str(detail["id"])
        state = self._norm_submission_state(detail)
        grade = None
        if detail.get("score") is not None or detail.get("grade") is not None:
            grade = sc.Grade(
                assignment_provider_id=aid, score=detail.get("score"),
                points_possible=assignment.get("points_possible"), grade=detail.get("grade"),
                graded_at=_dt(assignment.get("updated_at")),
                provenance=self._prov("grade", aid, url=url, source_timestamp=ts, cap=SchoolCapability.grades,
                                      state=CapabilityState.limited, span_text=str(detail.get("grade"))))
        rubric = None
        if detail.get("rubric"):
            assessed = detail.get("rubric_assessment") or {}
            criteria = [sc.RubricCriterion(description=c["description"], points=c.get("points"),
                                           awarded=assessed.get(c["description"]))
                        for c in detail["rubric"].get("criteria", [])]
            rubric = sc.Rubric(
                assignment_provider_id=aid, title=detail["rubric"].get("title"),
                points_possible=detail["rubric"].get("points_possible"), criteria=criteria,
                provenance=self._prov("rubric", aid, url=url, source_timestamp=ts, cap=SchoolCapability.rubrics,
                                      span_text=detail["rubric"].get("title")))
        feedback = [sc.Feedback(
            submission_provider_id=sid, author=c.get("author_name"), comment=c["comment"],
            created_at=_dt(c.get("created_at")),
            provenance=self._prov("feedback", f"{sid}:{i}", url=url, source_timestamp=_dt(c.get("created_at")),
                                  cap=SchoolCapability.feedback, span_text=c["comment"]))
            for i, c in enumerate(detail.get("submission_comments", []))]
        sub = sc.Submission(
            provider_id=sid, assignment_provider_id=aid, state=state.kind, submitted_at=ts,
            attempt=detail.get("attempt"), late=bool(detail.get("late")), missing=bool(detail.get("missing")),
            url=url, grade=grade, rubric=rubric, feedback=feedback,
            provenance=self._prov("submission", sid, url=url, source_timestamp=ts,
                                  cap=SchoolCapability.submission_state, span_text=str(detail.get("grade"))))
        return sc.ConnectorResult.supported(SchoolCapability.submission_state, sub)

    async def list_materials(self, *, course_id: str | None = None) -> sc.ConnectorResult[list[sc.Material]]:
        out = [self._norm_material(m) for m in self._fx["materials"]
               if course_id is None or str(m["course_id"]) == str(course_id)]
        return sc.ConnectorResult.supported(SchoolCapability.materials, out)

    async def list_announcements(self, *, course_id: str | None = None) -> sc.ConnectorResult[list[sc.Announcement]]:
        out = [self._norm_announcement(a) for a in self._live_announcements()
               if course_id is None or str(a["course_id"]) == str(course_id)]
        return sc.ConnectorResult.supported(SchoolCapability.announcements, out)

    async def list_schedule_events(
        self, *, since: datetime.datetime | None = None, until: datetime.datetime | None = None,
    ) -> sc.ConnectorResult[list[sc.ScheduleEvent]]:
        # HONEST unsupported: this adapter does not model the Canvas calendar/ICS feed. Return an explicit
        # unsupported result with a reason — never an empty list that would read as "you have no events".
        return sc.ConnectorResult.unsupported(
            SchoolCapability.schedule_events,
            "Canvas calendar/ICS feed is not connected in this adapter")

    # ------------------------------------------------------------------ sync + change detection

    async def sync(self, resource: str, cursor: sc.SyncCursor | None = None) -> sc.ConnectorResult[sc.SyncPage]:
        """Return created/updated/deleted for ``resource`` since ``cursor`` (Canvas ``updated_since`` model).

        created vs updated is decided from the object's own created_at/updated_at relative to the cursor;
        deletes come from the tombstone list. The next cursor is the latest timestamp observed, so a
        subsequent sync resumes exactly where this one stopped — restart-safe by construction.
        """
        if resource not in ("assignments", "announcements", "courses"):
            return sc.ConnectorResult.unsupported(
                SchoolCapability.sync_cursors, f"resource '{resource}' is not syncable by this adapter")

        after = _dt(cursor.cursor_value) if (cursor and cursor.cursor_value) else None
        rows = {"assignments": self._fx["assignments"], "announcements": self._fx["announcements"],
                "courses": self._fx["courses"]}[resource]
        normalizer = {"assignments": self._norm_assignment, "announcements": self._norm_announcement,
                      "courses": self._norm_course}[resource]
        dead_ids = {str(d["id"]) for d in self._fx["deleted"][resource]}

        changes: list[sc.ResourceChange] = []
        latest = after
        for raw in rows:
            if str(raw["id"]) in dead_ids:
                continue
            created = _dt(raw.get("created_at"))
            updated = _dt(raw.get("updated_at")) or created
            if after is not None and updated is not None and updated <= after:
                continue
            change_type = sc.ChangeType.created if (created is not None and (after is None or created > after)) else sc.ChangeType.updated
            obj = normalizer(raw)
            changes.append(sc.ResourceChange(
                resource=resource, provider_id=str(raw["id"]), change_type=change_type,
                object=obj.model_dump(mode="json")))
            if updated is not None and (latest is None or updated > latest):
                latest = updated
        for d in self._fx["deleted"][resource]:
            deleted_at = _dt(d.get("deleted_at"))
            if after is not None and deleted_at is not None and deleted_at <= after:
                continue
            changes.append(sc.ResourceChange(
                resource=resource, provider_id=str(d["id"]), change_type=sc.ChangeType.deleted, object=None))
            if deleted_at is not None and (latest is None or deleted_at > latest):
                latest = deleted_at

        next_cursor = sc.SyncCursor(
            provider=PROVIDER, resource=resource,
            cursor_value=_iso(latest) if latest is not None else (cursor.cursor_value if cursor else None),
            updated_at=self.synced_at)
        page = sc.SyncPage(resource=resource, changes=changes, next_cursor=next_cursor)
        return sc.ConnectorResult.supported(SchoolCapability.sync_cursors, page)

    # ------------------------------------------------------------------ mutators (test-only drivers)

    def add_assignment(self, *, course_id: int, name: str, at: datetime.datetime, provider_id: int,
                       due_at: str | None = None, points_possible: float | None = None) -> None:
        self._fx["assignments"].append({
            "id": provider_id, "course_id": course_id, "name": name, "description": "", "due_at": due_at,
            "unlock_at": None, "lock_at": None, "points_possible": points_possible,
            "submission_types": ["online_upload"], "html_url": f"{BASE_URL}/courses/{course_id}/assignments/{provider_id}",
            "created_at": _iso(at), "updated_at": _iso(at),
            "submission": {"workflow_state": "unsubmitted", "submitted_at": None, "score": None, "grade": None, "late": False, "missing": False, "attempt": None}})

    def update_assignment(self, provider_id: int, *, at: datetime.datetime, **changes) -> None:
        for raw in self._fx["assignments"]:
            if raw["id"] == provider_id:
                raw.update(changes)
                raw["updated_at"] = _iso(at)
                return
        raise KeyError(f"assignment {provider_id} not found")

    def delete_assignment(self, provider_id: int, *, at: datetime.datetime) -> None:
        self._fx["deleted"]["assignments"].append({"id": provider_id, "deleted_at": _iso(at)})
