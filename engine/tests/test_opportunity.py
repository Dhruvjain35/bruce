"""Offline tests for the opportunity engine (#1). Deterministic, no network, no LLM.

We build ``ExtractedIntake`` objects directly and assert classification, spam detection, dedupe,
ranking (fit ordering + free-preferred + past-deadline down-rank + spam sinks), and the
intake -> Task conversion. ``ingest_opportunity_text`` is intentionally NOT exercised (it hits the
LLM); its deterministic building blocks are what we cover here.
"""

from datetime import date

from bruce_engine.models import (
    ExtractedDeadline,
    ExtractedIntake,
    IntakeSourceKind,
    RequiredItem,
    StudentLevel,
    StudentProfile,
    TaskKind,
)
from bruce_engine.opportunity import (
    OPPORTUNITY_KINDS,
    classify_opportunity,
    dedupe_opportunities,
    is_spam,
    opportunity_to_task,
    rank_opportunities,
)

TODAY = date(2026, 7, 13)


def _intake(
    title=None,
    *,
    summary=None,
    eligibility=None,
    cost=None,
    deadlines=None,
    required_items=None,
    links=None,
    contacts=None,
    raw=None,
) -> ExtractedIntake:
    return ExtractedIntake(
        source_kind=IntakeSourceKind.text,
        title=title,
        summary=summary,
        eligibility=eligibility,
        cost=cost,
        deadlines=deadlines or [],
        required_items=required_items or [],
        links=links or [],
        contacts=contacts or [],
        raw_source_excerpt=raw,
    )


def _deadline(iso_date: str, label: str = "Application deadline") -> ExtractedDeadline:
    return ExtractedDeadline(
        label=label, date=iso_date, source_span=f"due {iso_date}", confidence=0.9
    )


def _student(interests) -> StudentProfile:
    return StudentProfile(
        name="Test Student",
        level=StudentLevel.high_school,
        field_interests=interests,
        background="Some projects.",
    )


# --- classify ---------------------------------------------------------------------------------


def test_classify_covers_every_category():
    cases = {
        "National Merit Scholarship": "scholarship",
        "Summer Software Engineering Internship": "internship",
        "Regional Science Olympiad": "competition",
        "Summer Coding Bootcamp": "program",
        "Undergraduate Research Assistant Position": "research",
        "MIT Hackathon 2026": "hackathon",
        "Weekend Volunteer Opportunity at the Food Bank": "volunteering",
        "Google Fellowship for Undergraduates": "fellowship",
        "Robotics Club Callout Meeting": "club",
        "Cafeteria Menu Update": "other",
    }
    for title, expected in cases.items():
        got = classify_opportunity(_intake(title))
        assert got == expected, f"{title!r} -> {got!r}, expected {expected!r}"
        assert got in OPPORTUNITY_KINDS


def test_classify_priority_specific_beats_generic():
    # both "research" and "fellowship" present -> fellowship is more specific and wins
    assert classify_opportunity(_intake("Research Fellowship Program")) == "fellowship"
    # "internship" beats the generic "program" bucket
    assert classify_opportunity(_intake("Summer Internship Program")) == "internship"


def test_classify_word_boundary_avoids_false_positive():
    # "campus" must NOT trigger the "camp" (program) keyword
    assert classify_opportunity(_intake("On-Campus Info Session")) == "other"


def test_classify_reads_summary_and_eligibility():
    intake = _intake("Opportunity", summary="A paid summer internship for high schoolers.")
    assert classify_opportunity(intake) == "internship"


# --- is_spam ----------------------------------------------------------------------------------


def test_is_spam_hard_scam_markers():
    intake = _intake(
        "You Won!",
        summary="Congratulations! You have won a $1000 gift card. Click here to claim your prize.",
        deadlines=[_deadline("2026-09-01")],  # even with a deadline, scam markers win
        eligibility="Anyone",
    )
    assert is_spam(intake) is True


def test_is_spam_promotional_only_no_deadline_no_eligibility():
    intake = _intake(
        "Amazing Exclusive Offer",
        summary="Don't miss out! Sign up now for this exclusive offer, limited time only!",
    )
    assert is_spam(intake) is True


def test_is_spam_legit_opportunity_not_flagged():
    intake = _intake(
        "National Merit Scholarship",
        summary="A merit scholarship for graduating seniors.",
        eligibility="U.S. high school seniors with a qualifying PSAT score.",
        deadlines=[_deadline("2026-10-15")],
        required_items=[RequiredItem(name="Transcript", kind="doc")],
    )
    assert is_spam(intake) is False


def test_is_spam_legit_competition_with_prize_words_not_flagged():
    # "prize" / "winner" appear, but there is a real deadline + eligibility -> not spam
    intake = _intake(
        "State Science Fair",
        summary="Compete for the grand prize; the winner advances to nationals.",
        eligibility="Grades 9-12.",
        deadlines=[_deadline("2026-11-01")],
    )
    assert is_spam(intake) is False


def test_is_spam_sparse_but_not_promotional_is_not_spam():
    # no legitimacy signal, but also no promotional markers -> just sparse, not spam
    intake = _intake("Study Group", summary="We meet on Tuesdays to review calculus.")
    assert is_spam(intake) is False


# --- dedupe -----------------------------------------------------------------------------------


def test_dedupe_collapses_same_title_case_and_whitespace():
    a = _intake("Summer Research Program", deadlines=[_deadline("2026-06-01")])
    b = _intake("  summer   RESEARCH program ", deadlines=[_deadline("2026-06-01")])
    out = dedupe_opportunities([a, b])
    assert out == [a]  # first occurrence kept


def test_dedupe_keeps_same_title_different_deadline():
    a = _intake("Summer Research Program", deadlines=[_deadline("2026-06-01")])
    b = _intake("Summer Research Program", deadlines=[_deadline("2027-06-01")])
    out = dedupe_opportunities([a, b])
    assert len(out) == 2


def test_dedupe_never_merges_titleless_intakes():
    a = _intake(None, summary="something")
    b = _intake(None, summary="different")
    out = dedupe_opportunities([a, b])
    assert len(out) == 2


# --- rank -------------------------------------------------------------------------------------


def test_rank_orders_by_interest_overlap():
    student = _student(["machine learning", "robotics"])
    both = _intake("Machine Learning and Robotics Summer Research", cost="Free",
                   deadlines=[_deadline("2026-09-01")])
    one = _intake("Robotics Workshop Series", cost="Free", deadlines=[_deadline("2026-09-01")])
    none = _intake("Poetry Writing Retreat", cost="Free", deadlines=[_deadline("2026-09-01")])

    ranked = rank_opportunities([none, one, both], student, today=TODAY)
    assert [r.intake for r in ranked] == [both, one, none]
    assert all(0.0 <= r.fit_score <= 1.0 for r in ranked)
    assert "machine learning and robotics" in ranked[0].fit_reason.lower()


def test_rank_prefers_free_over_paid():
    student = _student(["robotics"])
    free = _intake("Robotics Workshop", cost="Free", deadlines=[_deadline("2026-09-01")])
    paid = _intake("Robotics Workshop", cost="$200 registration fee",
                   deadlines=[_deadline("2026-09-01")])

    ranked = rank_opportunities([paid, free], student, today=TODAY)
    assert ranked[0].intake is free
    assert ranked[0].fit_score > ranked[1].fit_score
    assert "free to participate" in ranked[0].fit_reason


def test_rank_downranks_past_deadline():
    student = _student(["robotics"])
    future = _intake("Robotics Workshop", cost="Free", deadlines=[_deadline("2026-09-01")])
    past = _intake("Robotics Workshop", cost="Free", deadlines=[_deadline("2026-01-01")])

    ranked = rank_opportunities([past, future], student, today=TODAY)
    assert ranked[0].intake is future
    assert ranked[1].intake is past
    assert "still open" in ranked[0].fit_reason
    assert "already passed" in ranked[1].fit_reason


def test_rank_pushes_spam_to_bottom():
    student = _student(["robotics"])
    legit = _intake("Robotics Workshop", cost="Free", deadlines=[_deadline("2026-09-01")])
    spam = _intake(
        "Robotics Prize!!!",
        summary="Congratulations! You have won! Click here to claim your prize. Sign up now!",
    )
    ranked = rank_opportunities([spam, legit], student, today=TODAY)
    assert ranked[0].intake is legit
    assert ranked[-1].intake is spam
    assert ranked[-1].fit_score < ranked[0].fit_score
    assert "spam" in ranked[-1].fit_reason.lower()


# --- opportunity_to_task ----------------------------------------------------------------------


def test_opportunity_to_task_maps_fields_and_earliest_due():
    intake = _intake(
        "National Merit Scholarship",
        summary="A merit scholarship.",
        deadlines=[_deadline("2026-10-15"), _deadline("2026-05-15")],
        required_items=[RequiredItem(name="Transcript", kind="doc"),
                        RequiredItem(name="Essay", kind="essay")],
        links=["https://example.org/apply", "https://example.org/info"],
    )
    task = opportunity_to_task(intake)

    assert task.kind == TaskKind.application  # scholarship is an apply-to kind
    assert task.title == "National Merit Scholarship"
    assert task.due == "2026-05-15"  # earliest of the two deadlines
    assert [ri.name for ri in task.required_items] == ["Transcript", "Essay"]
    assert task.source == "https://example.org/apply"  # first link
    assert task.notes == "A merit scholarship."


def test_opportunity_to_task_required_items_are_copies():
    intake = _intake("Contest", deadlines=[_deadline("2026-09-01")],
                     required_items=[RequiredItem(name="Form", kind="form")])
    task = opportunity_to_task(intake)
    assert task.required_items[0] is not intake.required_items[0]
    task.required_items[0].provided = True
    assert intake.required_items[0].provided is False  # source untouched


def test_opportunity_to_task_kind_defaults_and_override():
    comp = _intake("Robotics Competition", deadlines=[_deadline("2026-09-01")])
    assert opportunity_to_task(comp).kind == TaskKind.opportunity  # non-apply kind
    # explicit override wins
    assert opportunity_to_task(comp, kind=TaskKind.application).kind == TaskKind.application


def test_opportunity_to_task_id_is_deterministic():
    intake = _intake("Summer Program", deadlines=[_deadline("2026-06-01")])
    assert opportunity_to_task(intake).task_id == opportunity_to_task(intake).task_id


def test_opportunity_to_task_falls_back_to_summary_title():
    intake = _intake(None, summary="First line becomes the title\nsecond line ignored",
                     deadlines=[_deadline("2026-06-01")])
    task = opportunity_to_task(intake)
    assert task.title == "First line becomes the title"
    assert task.due == "2026-06-01"
