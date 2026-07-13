"""Core data contracts for the Bruce outreach engine.

The wedge: help an ambitious student turn "I want a research position / internship in X"
into a set of genuinely personalized, grounded outreach emails to the right professors —
researched, drafted, student-reviewed, and sent the right way.

GROUNDING CONTRACT (non-negotiable — this is the product):
  * Every factual claim about a professor or their work MUST trace to an ``Evidence`` object
    with a verifiable source URL.
  * The engine NEVER fabricates a person, a paper, or an email address.
  * Anything uncertain is surfaced in ``uncertainties`` / ``flags`` — never guessed and
    presented as fact.
  * Nothing is ever sent automatically. The student reviews and sends every email.

These are plain Pydantic models so they double as the structured-output types the agent
layer validates against — malformed model output is rejected, not silently accepted.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, HttpUrl


class StudentLevel(str, Enum):
    high_school = "high_school"
    undergrad = "undergrad"
    grad = "grad"
    gap_year = "gap_year"


class OutreachType(str, Enum):
    research_position = "research_position"
    internship = "internship"
    phd_inquiry = "phd_inquiry"
    informational = "informational"


class DraftStatus(str, Enum):
    draft = "draft"
    edited = "edited"
    approved = "approved"
    sent = "sent"
    replied = "replied"
    no_reply = "no_reply"
    follow_up_due = "follow_up_due"
    closed = "closed"


class EvidenceKind(str, Enum):
    paper = "paper"
    profile_page = "profile_page"
    lab_page = "lab_page"
    news = "news"
    grant = "grant"


class Evidence(BaseModel):
    """A single verifiable source backing a claim. The anti-hallucination unit."""

    kind: EvidenceKind
    title: str
    url: HttpUrl
    source: str = Field(description="provenance, e.g. 'openalex', 'semantic_scholar', 'dept_page'")
    snippet: str | None = Field(default=None, description="short quote supporting the claim")
    retrieved_at: datetime | None = None


class PaperRef(BaseModel):
    """A real paper. Every field that is populated must come from a grounded source."""

    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    url: HttpUrl | None = None
    abstract_snippet: str | None = None
    source: str = Field(description="which API/page this was retrieved from")


class StudentProfile(BaseModel):
    name: str
    level: StudentLevel
    school: str | None = None
    field_interests: list[str] = Field(default_factory=list)
    background: str = Field(description="free text: projects, research, coursework the student has done")
    skills: list[str] = Field(default_factory=list)
    links: list[HttpUrl] = Field(default_factory=list, description="portfolio, GitHub, Google Scholar, etc.")
    resume_text: str | None = None
    voice_sample: str | None = Field(
        default=None, description="a sample of the student's own writing, used to match tone"
    )


class OutreachGoal(BaseModel):
    outreach_type: OutreachType
    topic: str = Field(description="what the student wants to work on, in their words")
    fields: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    locations: list[str] = Field(default_factory=list, description="preferred cities/regions, if any")
    institutions: list[str] = Field(default_factory=list, description="specific target schools, if any")
    timeframe: str | None = Field(default=None, description="e.g. 'Summer 2027'")
    funded_required: bool = False
    target_count: int = Field(default=10, ge=1, le=50)


class ProfessorCandidate(BaseModel):
    """A real professor matched to the goal. Nothing here may be invented."""

    name: str
    title: str | None = None
    department: str | None = None
    institution: str
    profile_url: HttpUrl | None = None
    research_summary: str = Field(description="grounded summary of their work, from evidence")
    recent_work: list[PaperRef] = Field(default_factory=list)
    fit_rationale: str = Field(description="why this professor fits the student, tied to evidence")
    fit_score: float = Field(ge=0.0, le=1.0)
    contact_email: str | None = None
    email_source: str | None = Field(
        default=None, description="where the email was found — NEVER guessed; None if not found"
    )
    email_verified: bool = False
    evidence: list[Evidence] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class OutreachDraft(BaseModel):
    candidate_name: str
    institution: str
    subject: str
    body: str
    personalization_points: list[str] = Field(
        default_factory=list, description="each should reference a specific piece of the professor's work"
    )
    word_count: int
    tone: str = Field(default="earnest, concise, specific")
    flags: list[str] = Field(default_factory=list, description="things the student should double-check before sending")
    status: DraftStatus = DraftStatus.draft


class DiscoveryResult(BaseModel):
    goal: OutreachGoal
    candidates: list[ProfessorCandidate] = Field(default_factory=list)
    queries_used: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None


class OutreachPlan(BaseModel):
    """The full result of one run: who to contact and what to send them."""

    student: StudentProfile
    goal: OutreachGoal
    discovery: DiscoveryResult
    drafts: list[OutreachDraft] = Field(default_factory=list)
