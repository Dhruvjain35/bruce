"""Tests for the anti-spam guard (Bruce's name-swap test). Deterministic, no network.

We build ``OutreachDraft`` objects directly and assert:
  (a) two near-identical ("change only the name") drafts ARE flagged as near-duplicates,
  (b) two distinctly-personalized drafts (each engaging a DIFFERENT paper) are NOT flagged,
  (c) more than two drafts into one institution trip the per-department volume cap.
"""

from bruce_engine.antispam import (
    DEFAULT_SIMILARITY_THRESHOLD,
    _NEAR_DUP_PREFIX,
    _VOLUME_PREFIX,
    draft_similarity,
    flag_institution_volume,
    flag_near_duplicates,
    flag_spam,
)
from bruce_engine.drafting import STUDENT_QUESTION_PLACEHOLDER
from bruce_engine.models import OutreachDraft

# Shared scaffolding every draft for one student legitimately reuses: same opening, same
# fit sentence, same 15-minute ask, same signature — all in the student's own voice.
_OPENING = (
    "My name is Dhruv Jain, a high school researcher working on machine learning, "
    "and I am reaching out about research opportunities in your group this coming summer."
)
_FIT = "I have built several projects in Python and trained a few small models on my own."
_ASK = "Would you have fifteen minutes for a short chat sometime in the coming weeks?"


def _make_draft(
    candidate_name: str,
    institution: str,
    *,
    last: str,
    personalization: str,
    personalization_points: list[str],
) -> OutreachDraft:
    body = (
        f"Dear Professor {last},\n\n"
        f"{_OPENING}\n\n"
        f"{personalization}\n\n"
        f"{_FIT} {STUDENT_QUESTION_PLACEHOLDER}\n\n"
        f"{_ASK}\n\n"
        "Best regards,\nDhruv Jain"
    )
    return OutreachDraft(
        candidate_name=candidate_name,
        institution=institution,
        subject="Research opportunity inquiry",
        body=body,
        personalization_points=personalization_points,
        word_count=len(body.split()),
    )


# --- (a) near-identical drafts (only the name changed) ARE flagged -----------------------------

# Generic template: no paper engaged at all, just a vague "your area" line — the purest spam.
_GENERIC = (
    "I have been reading broadly about your area and find the direction of your lab "
    "compelling and closely aligned with exactly what I want to pursue next."
)


def test_name_swap_duplicates_are_flagged():
    a = _make_draft(
        "Dr. Alice Smith", "MIT", last="Smith", personalization=_GENERIC, personalization_points=[]
    )
    b = _make_draft(
        "Dr. Bob Jones", "Stanford", last="Jones", personalization=_GENERIC, personalization_points=[]
    )

    # Only the (stripped) greeting name differs -> the cores are identical.
    assert draft_similarity(a, b) >= DEFAULT_SIMILARITY_THRESHOLD
    assert draft_similarity(a, b) == 1.0

    pairs = flag_near_duplicates([a, b])
    assert pairs == [(0, 1, 1.0)]
    assert any(f.startswith(_NEAR_DUP_PREFIX) for f in a.flags)
    assert any(f.startswith(_NEAR_DUP_PREFIX) for f in b.flags)
    # the flag names the OTHER professor, so the student knows which pair to fix
    assert any("Bob Jones" in f for f in a.flags)
    assert any("Alice Smith" in f for f in b.flags)


# --- (b) genuinely personalized drafts (different papers) are NOT flagged ----------------------

_PAPER_A = (
    'I was struck by your paper "Sparse Attention Routing for Long-Context Transformers", '
    "where you show that gating attention heads by learned sparsity cuts memory use sharply "
    "while preserving retrieval accuracy on long documents."
)
_POINTS_A = ['Referenced paper: "Sparse Attention Routing for Long-Context Transformers" (2023)']

_PAPER_B = (
    'Your paper "Contrastive Pretraining of Molecular Graphs for Property Prediction" caught '
    "my attention, especially the finding that contrastive objectives over graph augmentations "
    "improve out-of-distribution generalization for toxicity prediction."
)
_POINTS_B = ['Referenced paper: "Contrastive Pretraining of Molecular Graphs for Property Prediction" (2022)']


def test_distinctly_personalized_drafts_not_flagged():
    a = _make_draft(
        "Dr. Alice Smith", "MIT", last="Smith", personalization=_PAPER_A, personalization_points=_POINTS_A
    )
    b = _make_draft(
        "Dr. Bob Jones", "Stanford", last="Jones", personalization=_PAPER_B, personalization_points=_POINTS_B
    )

    # Same student scaffolding, but each engages a genuinely different paper -> well below threshold.
    sim = draft_similarity(a, b)
    assert sim < DEFAULT_SIMILARITY_THRESHOLD

    pairs = flag_near_duplicates([a, b])
    assert pairs == []
    assert not any(f.startswith(_NEAR_DUP_PREFIX) for f in a.flags)
    assert not any(f.startswith(_NEAR_DUP_PREFIX) for f in b.flags)


# --- (c) more than two drafts into one institution trip the volume cap -------------------------


def test_institution_volume_cap_flags_over_limit():
    mit1 = _make_draft(
        "Dr. Alice Smith", "MIT", last="Smith", personalization=_PAPER_A, personalization_points=_POINTS_A
    )
    mit2 = _make_draft(
        "Dr. Bob Jones", "MIT", last="Jones", personalization=_PAPER_B, personalization_points=_POINTS_B
    )
    mit3 = _make_draft(
        "Dr. Carol Lee", "MIT", last="Lee", personalization=_GENERIC, personalization_points=[]
    )
    stanford = _make_draft(
        "Dr. Dan Park", "Stanford", last="Park", personalization=_PAPER_A, personalization_points=_POINTS_A
    )

    flagged = flag_institution_volume([mit1, mit2, mit3, stanford])
    assert flagged == [("MIT", 3)]

    # all three MIT drafts get the volume flag...
    for draft in (mit1, mit2, mit3):
        assert any(f.startswith(_VOLUME_PREFIX) for f in draft.flags)
    # ...and the lone Stanford draft does not
    assert not any(f.startswith(_VOLUME_PREFIX) for f in stanford.flags)


def test_two_per_institution_is_within_the_cap():
    a = _make_draft(
        "Dr. Alice Smith", "MIT", last="Smith", personalization=_PAPER_A, personalization_points=_POINTS_A
    )
    b = _make_draft(
        "Dr. Bob Jones", "MIT", last="Jones", personalization=_PAPER_B, personalization_points=_POINTS_B
    )
    assert flag_institution_volume([a, b]) == []
    assert not any(f.startswith(_VOLUME_PREFIX) for f in a.flags)
    assert not any(f.startswith(_VOLUME_PREFIX) for f in b.flags)


# --- extra guards -----------------------------------------------------------------------------


def test_empty_body_drafts_are_not_near_duplicates():
    # A candidate with no groundable paper yields an empty body (see drafting.draft_one).
    empty = [
        OutreachDraft(
            candidate_name=name,
            institution="MIT",
            subject="",
            body="",
            personalization_points=[],
            word_count=0,
        )
        for name in ("Dr. A", "Dr. B")
    ]
    assert flag_near_duplicates(empty) == []
    assert all(not any(f.startswith(_NEAR_DUP_PREFIX) for f in d.flags) for d in empty)


def test_flag_spam_runs_both_checks_and_is_idempotent():
    a = _make_draft(
        "Dr. Alice Smith", "MIT", last="Smith", personalization=_GENERIC, personalization_points=[]
    )
    b = _make_draft(
        "Dr. Bob Jones", "MIT", last="Jones", personalization=_GENERIC, personalization_points=[]
    )
    c = _make_draft(
        "Dr. Carol Lee", "MIT", last="Lee", personalization=_GENERIC, personalization_points=[]
    )
    drafts = [a, b, c]

    flag_spam(drafts)
    near = [f for f in a.flags if f.startswith(_NEAR_DUP_PREFIX)]
    vol = [f for f in a.flags if f.startswith(_VOLUME_PREFIX)]
    assert near and vol  # both a name-swap duplicate AND over the MIT volume cap

    # running it again must not duplicate flags
    flag_spam(drafts)
    assert [f for f in a.flags if f.startswith(_NEAR_DUP_PREFIX)] == near
    assert [f for f in a.flags if f.startswith(_VOLUME_PREFIX)] == vol
