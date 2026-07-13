"""Tests for the deterministic verification helpers (no network, no LLM).

The entailment gate calls a frontier model, but the entity guard and paper-matching are pure
and are the fail-closed backbone: a draft must quote one of the professor's REAL paper titles
and must not contain any DOI that isn't in the evidence. These tests exercise only that
deterministic layer with synthetic models.
"""

from __future__ import annotations

from bruce_engine.models import OutreachDraft, PaperRef, ProfessorCandidate
from bruce_engine.verify import _entity_guard, _find_referenced_paper

_REAL_TITLE = "Deep Learning for Quantum Chemistry"
_REAL_DOI = "10.1038/s41586-024-12345-6"


def _paper(title: str, doi: str | None = None) -> PaperRef:
    return PaperRef(title=title, doi=doi, abstract_snippet="We study X and find Y.", source="openalex")


def _candidate(papers: list[PaperRef]) -> ProfessorCandidate:
    return ProfessorCandidate(
        name="Dr. Real Person",
        institution="Real University",
        research_summary="works on quantum chemistry",
        fit_rationale="fits because overlap",
        fit_score=0.8,
        recent_work=papers,
    )


def _draft(body: str, subject: str = "Prospective research student") -> OutreachDraft:
    return OutreachDraft(
        candidate_name="Dr. Real Person",
        institution="Real University",
        subject=subject,
        body=body,
        word_count=len(body.split()),
    )


# ---------- _find_referenced_paper ----------


def test_find_referenced_paper_matches_title_prefix_case_insensitively():
    cand = _candidate([_paper(_REAL_TITLE, _REAL_DOI)])
    draft = _draft(f'I recently read your paper "{_REAL_TITLE.upper()}" and was struck by it.')
    ref = _find_referenced_paper(draft, cand)
    assert ref is not None
    assert ref.title == _REAL_TITLE


def test_find_referenced_paper_returns_none_when_no_title_mentioned():
    cand = _candidate([_paper(_REAL_TITLE, _REAL_DOI)])
    draft = _draft("I am interested in your lab and would love to contribute.")
    assert _find_referenced_paper(draft, cand) is None


def test_find_referenced_paper_picks_the_referenced_one_among_many():
    other = _paper("A Totally Different Study of Birds")
    target = _paper(_REAL_TITLE, _REAL_DOI)
    cand = _candidate([other, target])
    draft = _draft(f"Your work {_REAL_TITLE} shaped how I think about the field.")
    ref = _find_referenced_paper(draft, cand)
    assert ref is target


def test_find_referenced_paper_skips_empty_titles():
    cand = _candidate([_paper("")])
    # an empty title must not spuriously match every draft
    draft = _draft("Some arbitrary body of text here.")
    assert _find_referenced_paper(draft, cand) is None


# ---------- _entity_guard ----------


def test_entity_guard_passes_for_real_title_no_doi():
    cand = _candidate([_paper(_REAL_TITLE, _REAL_DOI)])
    draft = _draft(f'Your paper "{_REAL_TITLE}" changed how I approach simulations.')
    assert _entity_guard(draft, cand) == []


def test_entity_guard_passes_when_real_doi_is_cited():
    cand = _candidate([_paper(_REAL_TITLE, _REAL_DOI)])
    draft = _draft(
        f'I read "{_REAL_TITLE}" (doi: {_REAL_DOI}) and have a question about the method.'
    )
    assert _entity_guard(draft, cand) == []


def test_entity_guard_flags_invented_doi():
    cand = _candidate([_paper(_REAL_TITLE, _REAL_DOI)])
    draft = _draft(
        f'Your paper "{_REAL_TITLE}" — I also saw doi 10.9999/invented.2024 which impressed me.'
    )
    problems = _entity_guard(draft, cand)
    assert any("DOI not in the evidence" in p for p in problems)
    assert "10.9999/invented.2024" in " ".join(problems)
    # the real title IS quoted, so there must be no missing-title complaint
    assert not any("real paper titles" in p for p in problems)


def test_entity_guard_flags_missing_real_title():
    cand = _candidate([_paper(_REAL_TITLE, _REAL_DOI)])
    draft = _draft("I admire your lab broadly and would love to help out this summer.")
    problems = _entity_guard(draft, cand)
    assert any("real paper titles" in p for p in problems)


def test_entity_guard_flags_both_when_no_title_and_invented_doi():
    cand = _candidate([_paper(_REAL_TITLE, _REAL_DOI)])
    draft = _draft("I love your general area; see doi 10.1234/fabricated.1 for context.")
    problems = _entity_guard(draft, cand)
    assert any("real paper titles" in p for p in problems)
    assert any("DOI not in the evidence" in p for p in problems)
