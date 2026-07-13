"""Tests for the engine data contracts.

These lock in the grounding contract at the type level: evidence needs a real URL,
candidates need an institution, and a candidate with no grounded email must not claim
its email is verified.
"""

import pytest
from pydantic import ValidationError

from bruce_engine.models import (
    Evidence,
    EvidenceKind,
    OutreachGoal,
    OutreachType,
    ProfessorCandidate,
    StudentLevel,
    StudentProfile,
)


def test_student_profile_minimal():
    s = StudentProfile(name="Test Student", level=StudentLevel.high_school, background="Did X, Y, Z.")
    assert s.level is StudentLevel.high_school
    assert s.field_interests == []


def test_goal_target_count_bounds():
    with pytest.raises(ValidationError):
        OutreachGoal(outreach_type=OutreachType.research_position, topic="ML", target_count=0)
    with pytest.raises(ValidationError):
        OutreachGoal(outreach_type=OutreachType.research_position, topic="ML", target_count=999)


def test_evidence_requires_valid_url():
    with pytest.raises(ValidationError):
        Evidence(kind=EvidenceKind.paper, title="A paper", url="not-a-url", source="openalex")

    ev = Evidence(
        kind=EvidenceKind.paper,
        title="A paper",
        url="https://openalex.org/W123",
        source="openalex",
    )
    assert str(ev.url).startswith("https://")


def test_candidate_requires_institution():
    with pytest.raises(ValidationError):
        ProfessorCandidate(
            name="Dr. Real",
            research_summary="works on X",
            fit_rationale="fits because Y",
            fit_score=0.8,
        )  # missing institution


def test_candidate_email_defaults_unverified():
    c = ProfessorCandidate(
        name="Dr. Real",
        institution="Real University",
        research_summary="works on X",
        fit_rationale="fits because Y",
        fit_score=0.8,
    )
    assert c.contact_email is None
    assert c.email_verified is False
    assert c.email_source is None


def test_fit_score_bounds():
    with pytest.raises(ValidationError):
        ProfessorCandidate(
            name="Dr. Real",
            institution="Real University",
            research_summary="x",
            fit_rationale="y",
            fit_score=1.5,
        )
