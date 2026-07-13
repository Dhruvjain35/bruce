"""Verification gate for a drafted email — fails closed. This is the moat.

Two checks:
  1. Entity guard (deterministic): the draft must quote one of the professor's REAL papers by
     title, and must contain no DOI that isn't in the evidence.
  2. Entailment (frontier model, OpenAI): every factual claim about the professor's work must be
     SUPPORTED by that paper's abstract — not unsupported or overstated.

A draft that fails either check is not "ready" to send. Grounding-with-verification is exactly
what the auto-writer competitors skip; it's the whole trust promise.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .llm import verification_model
from .models import OutreachDraft, PaperRef, ProfessorCandidate

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"')]+")


class DraftVerdict(BaseModel):
    ready: bool = Field(description="true only if grounded AND entailment == SUPPORTED with no problems")
    entailment: str = Field(description="SUPPORTED | NOT_SUPPORTED | OVERSTATED | NO_EVIDENCE")
    problems: list[str] = Field(default_factory=list)
    unsupported_spans: list[str] = Field(default_factory=list)


class _Entailment(BaseModel):
    entailment: str = Field(description="SUPPORTED, NOT_SUPPORTED, or OVERSTATED")
    unsupported_spans: list[str] = Field(default_factory=list)


_VERIFY_SYSTEM = """You are a strict fact-checker. You are given the ONLY true facts (a paper's title and
abstract) and a draft email. Judge ONLY the sentences that assert something about the professor or their
paper. Reply entailment=SUPPORTED if every such claim is fully supported by the abstract; NOT_SUPPORTED
if any claim is not in the abstract; OVERSTATED if a claim exaggerates beyond the abstract. List the exact
offending spans verbatim. Use no outside knowledge — the abstract is the only allowed source."""


def _find_referenced_paper(draft: OutreachDraft, candidate: ProfessorCandidate) -> PaperRef | None:
    body = draft.body.lower()
    for p in candidate.recent_work:
        title = (p.title or "").strip().lower()
        if title and title[:45] in body:
            return p
    return None


def _entity_guard(draft: OutreachDraft, candidate: ProfessorCandidate) -> list[str]:
    problems: list[str] = []
    text = f"{draft.subject}\n{draft.body}"
    if _find_referenced_paper(draft, candidate) is None:
        problems.append("Draft does not quote any of the professor's real paper titles.")
    known_dois = " ".join((p.doi or "") for p in candidate.recent_work)
    for doi in _DOI_RE.findall(text):
        if doi.rstrip(".,);") not in known_dois:
            problems.append(f"Draft contains a DOI not in the evidence: {doi}")
    return problems


async def verify_draft(draft: OutreachDraft, candidate: ProfessorCandidate) -> DraftVerdict:
    problems = _entity_guard(draft, candidate)
    ref = _find_referenced_paper(draft, candidate)
    abstract = ref.abstract_snippet if ref else None

    if not abstract:
        return DraftVerdict(
            ready=False,
            entailment="NO_EVIDENCE",
            problems=problems + ["No grounded abstract for the referenced paper — cannot verify claims."],
        )

    prompt = f"""EVIDENCE — the ONLY true facts:
Paper title: {ref.title}
Abstract: {abstract}

DRAFT EMAIL:
{draft.body}"""
    agent = Agent(verification_model(), output_type=_Entailment, system_prompt=_VERIFY_SYSTEM)
    ent = (await agent.run(prompt)).output
    entailment = ent.entailment.upper()
    ready = entailment == "SUPPORTED" and not problems
    return DraftVerdict(
        ready=ready,
        entailment=entailment,
        problems=problems,
        unsupported_spans=ent.unsupported_spans,
    )
