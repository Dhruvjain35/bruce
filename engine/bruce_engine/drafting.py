"""Grounded outreach drafting (Featherless / open model).

The drafter sees ONLY the candidate's real papers + the student profile. It anchors a
personalized hook on ONE real paper and engages a specific finding from its abstract. The
professor's name is template-injected from the candidate record (not model-written), and a
required placeholder forces the student to write the one genuine sentence AI can't fake.
Nothing that isn't in the evidence is allowed; verify.py fails closed on the rest.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field
from pydantic_ai import Agent, PromptedOutput

from .humanize import humanize_body
from .llm import drafting_model
from .models import DraftStatus, OutreachDraft, ProfessorCandidate, StudentProfile

STUDENT_QUESTION_PLACEHOLDER = (
    "[[ADD YOUR OWN GENUINE QUESTION OR IDEA HERE — Bruce won't write this for you]]"
)

_SYSTEM = """You draft ONE short cold email from a student to a professor about a research opportunity.
Rules:
- Use ONLY facts in the STUDENT profile and the PAPERS list. Never state a fact about the professor
  or their work that is not in a provided abstract. Never invent papers, findings, numbers, or titles.
- Reference exactly ONE paper (by its index) and engage with a SPECIFIC finding from ITS abstract.
- In 'personalization', name that paper by its EXACT title in quotes, exactly as given.
- ~150-180 words total, short paragraphs, one small ask (a ~15-minute chat), no same-day meeting,
  no flattery, no exclamation-point piles, no 'stepping stone to grad/med school' framing.
- Write in the student's voice. Do NOT write a greeting or a signature — those are added separately."""


class _Generated(BaseModel):
    referenced_paper_index: int = Field(description="0-based index into PAPERS the hook is about")
    subject: str
    opening: str = Field(description="1-2 sentences: who the student is (name/school/level) and why writing")
    personalization: str = Field(
        description="2-3 sentences engaging a specific finding; must quote the paper's EXACT title"
    )
    fit: str = Field(description="1-2 sentences on the student's relevant background/skills")
    ask: str = Field(description="one small concrete ask, e.g. a ~15-min chat")


def _last_name(full: str) -> str:
    parts = [p for p in full.replace(".", "").split() if p]
    return parts[-1] if parts else full


_GREETING_RE = re.compile(
    r"^\s*(hi|hello|dear|greetings|good (?:morning|afternoon|evening))\b[^,\n]*,\s*",
    re.IGNORECASE,
)


def _strip_greeting(text: str) -> str:
    """Drop a leading salutation the model may have written despite instructions — we inject
    our own greeting, so a model-written one produces a double greeting (an AI-sloppy tell)."""
    t = _GREETING_RE.sub("", text, count=1).strip()
    return (t[:1].upper() + t[1:]) if t else t


async def draft_one(student: StudentProfile, candidate: ProfessorCandidate) -> OutreachDraft:
    papers = [p for p in candidate.recent_work if (p.abstract_snippet or "").strip()]
    if not papers:
        return OutreachDraft(
            candidate_name=candidate.name,
            institution=candidate.institution,
            subject="",
            body="",
            personalization_points=[],
            word_count=0,
            flags=["No paper with an abstract to ground a genuine hook — don't send a vague email."],
            status=DraftStatus.draft,
        )

    papers_block = "\n\n".join(
        f"[{i}] TITLE: {p.title}\n    YEAR: {p.year or 'n.d.'}\n    ABSTRACT: {p.abstract_snippet}"
        for i, p in enumerate(papers)
    )
    prompt = f"""STUDENT
name: {student.name}
level: {student.level.value}
school: {student.school or 'n/a'}
background: {student.background}
interests: {', '.join(student.field_interests) or 'n/a'}

PROFESSOR: {candidate.name} ({candidate.institution})

PAPERS (reference exactly one by index):
{papers_block}
"""
    agent = Agent(drafting_model(), output_type=PromptedOutput(_Generated), system_prompt=_SYSTEM)
    gen = (await agent.run(prompt)).output

    idx = gen.referenced_paper_index if 0 <= gen.referenced_paper_index < len(papers) else 0
    paper = papers[idx]

    greeting = f"Dear Professor {_last_name(candidate.name)},"
    body = "\n\n".join(
        [
            greeting,
            _strip_greeting(gen.opening),
            gen.personalization.strip(),
            f"{gen.fit.strip()} {STUDENT_QUESTION_PLACEHOLDER}",
            gen.ask.strip(),
            f"Best regards,\n{student.name}",
        ]
    )
    body = humanize_body(body)  # soften AI tells; masks greeting/quoted title/placeholder/signature
    return OutreachDraft(
        candidate_name=candidate.name,
        institution=candidate.institution,
        subject=gen.subject.strip(),
        body=body,
        personalization_points=[f'Referenced paper: "{paper.title}" ({paper.year or "n.d."})'],
        word_count=len(body.split()),
        flags=["Replace the placeholder with your own genuine question before sending."],
        status=DraftStatus.draft,
    )
