"""Anti-spam guard — Bruce's "name-swap test".

Bruce's whole promise is that every email reads as if that professor is the ONLY person the
student wrote to. The classic cold-outreach failure is the opposite: one template blasted to a
whole department with only the salutation changed. This module catches that pattern BEFORE the
student sends, in two deterministic (offline, no-LLM) checks:

  1. Near-duplicate detection. Two drafts are flagged when their CORE text — the body with the
     greeting line, the signature, the ``STUDENT_QUESTION_PLACEHOLDER``, and the professor
     name/institution stripped out — is near-identical by shingle (n=3) Jaccard similarity.
  2. Per-institution volume. More than ~2 drafts aimed at one institution reads as a mass
     mailing to a single department, regardless of wording.

WHY A HIGH THRESHOLD (and why we up-weight the paper-specific personalization):
  Every draft for one student legitimately shares a lot of scaffolding — the same "who I am"
  opening, the same background/fit sentence, the same "~15-minute chat" ask, all in the
  student's own voice. Two *genuinely personalized* drafts (each engaging a DIFFERENT paper)
  therefore already overlap moderately (~0.4-0.6 Jaccard) on that scaffolding alone. A low
  threshold would flag them as spam — a false positive that would gut the product. So we
  (a) set the threshold HIGH (0.85), well above the natural same-student overlap, and
  (b) up-weight, in a *weighted* Jaccard, the shingles that come from the referenced paper (the
  personalization signature). Up-weighting can only LOWER similarity when the papers differ
  (those shingles land in the union but not the intersection); when two drafts are identical the
  intersection equals the union, so weighting leaves the score at exactly 1.0. The guard
  therefore suppresses false positives on personalized drafts WITHOUT ever hiding a true
  name-swap duplicate (an identical body always scores 1.0, weighting or not).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .drafting import STUDENT_QUESTION_PLACEHOLDER
from .models import OutreachDraft

__all__ = [
    "DEFAULT_SIMILARITY_THRESHOLD",
    "MAX_PER_INSTITUTION",
    "draft_similarity",
    "flag_institution_volume",
    "flag_near_duplicates",
    "flag_spam",
]

# 0.85: chosen deliberately above the ~0.4-0.6 core-text overlap that two GENUINELY personalized
# drafts from the same student already share (identical opening/fit/ask scaffolding), but far
# below the ~0.95-1.0 of a template whose only change is the professor's name. See module docstring.
DEFAULT_SIMILARITY_THRESHOLD = 0.85

# Weight applied to shingles drawn from the referenced-paper personalization signature. >1 so that
# a difference in the cited paper drags similarity down hard; it can never inflate the score of a
# true (identical-body) duplicate, so it adds no false negatives. See module docstring.
PERSONALIZATION_WEIGHT = 3.0

# "limit ~2 per department" — a third draft into the same institution reads as blanketing an office.
MAX_PER_INSTITUTION = 2

SHINGLE_N = 3

_NEAR_DUP_PREFIX = "ANTI-SPAM (name-swap): "
_VOLUME_PREFIX = "ANTI-SPAM (volume): "

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HONORIFICS = frozenset({"professor", "prof", "dr", "mr", "mrs", "ms", "mx"})

_GREETING_RE = re.compile(
    r"^\s*(hi|hello|dear|greetings|good (?:morning|afternoon|evening))\b",
    re.IGNORECASE,
)
_SIGNOFF_RE = re.compile(
    r"^\s*(best regards|kind regards|warm regards|best wishes|many thanks|thank you|thanks|"
    r"sincerely|regards|best|cheers|respectfully|yours (?:sincerely|truly|faithfully))"
    r"\b[,.!]?\s*$",
    re.IGNORECASE,
)


Shingle = tuple[str, ...]


@dataclass(frozen=True)
class _Prepared:
    """Pre-computed comparison material for one draft (built once, compared many times)."""

    tokens: tuple[str, ...]  # core-body tokens; honorifics dropped, name/institution NOT yet dropped
    name_inst: frozenset[str]  # professor name + institution tokens (dropped pairwise, symmetrically)
    pers_shingles: frozenset[Shingle]  # shingles of the referenced-paper personalization signature


def _shingles(tokens: list[str] | tuple[str, ...], n: int = SHINGLE_N) -> set[Shingle]:
    """Word-level n-gram shingles. Fewer than ``n`` tokens collapse to a single whole-text shingle."""
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _strip_greeting_and_signature(body: str) -> str:
    """Drop the leading salutation line and the trailing sign-off + signature name lines.

    The greeting (``Dear Professor <name>,``) and signature (``Best regards,\\n<student>``) are
    template-injected by the drafter, so they are identical noise across a student's drafts and
    must not count toward (or against) similarity.
    """
    lines = body.splitlines()
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start < len(lines) and _GREETING_RE.match(lines[start]):
        start += 1
    end = len(lines)
    for i in range(start, len(lines)):
        if _SIGNOFF_RE.match(lines[i]):
            end = i  # drop the sign-off line and everything after it (the signature name)
            break
    return "\n".join(lines[start:end])


def _prepare(draft: OutreachDraft) -> _Prepared:
    body = _strip_greeting_and_signature(draft.body).replace(STUDENT_QUESTION_PLACEHOLDER, " ")
    tokens = tuple(t for t in _TOKEN_RE.findall(body.lower()) if t not in _HONORIFICS)

    name_inst = frozenset(
        _TOKEN_RE.findall(f"{draft.candidate_name} {draft.institution}".lower())
    )

    pers_text = " ".join(draft.personalization_points).lower()
    pers_tokens = [
        t for t in _TOKEN_RE.findall(pers_text) if t not in _HONORIFICS and t not in name_inst
    ]
    pers_shingles = frozenset(_shingles(pers_tokens))

    return _Prepared(tokens=tokens, name_inst=name_inst, pers_shingles=pers_shingles)


def _prepared_similarity(a: _Prepared, b: _Prepared) -> float:
    # Drop the union of both drafts' name/institution tokens from BOTH, so a name swap (or two
    # different institutions containing common words like "of") never skews the comparison.
    drop = a.name_inst | b.name_inst
    body_a = _shingles([t for t in a.tokens if t not in drop])
    body_b = _shingles([t for t in b.tokens if t not in drop])

    dom_a = body_a | a.pers_shingles
    dom_b = body_b | b.pers_shingles
    if not dom_a or not dom_b:
        return 0.0

    pers_union = a.pers_shingles | b.pers_shingles

    def weight(shingle: Shingle) -> float:
        return PERSONALIZATION_WEIGHT if shingle in pers_union else 1.0

    numerator = sum(weight(s) for s in (dom_a & dom_b))
    denominator = sum(weight(s) for s in (dom_a | dom_b))
    return numerator / denominator if denominator else 0.0


def draft_similarity(a: OutreachDraft, b: OutreachDraft) -> float:
    """Weighted shingle (n=3) Jaccard similarity of two drafts' CORE text, in ``[0.0, 1.0]``.

    ``1.0`` means the drafts differ only in stripped material (name/greeting/signature/placeholder)
    — the pure name-swap spam pattern. Genuinely personalized drafts score well below the
    ``DEFAULT_SIMILARITY_THRESHOLD``.
    """
    return _prepared_similarity(_prepare(a), _prepare(b))


def _add_flag(draft: OutreachDraft, flag: str) -> None:
    if flag not in draft.flags:  # idempotent: safe to run the guard more than once
        draft.flags.append(flag)


def flag_near_duplicates(
    drafts: list[OutreachDraft], *, threshold: float = DEFAULT_SIMILARITY_THRESHOLD
) -> list[tuple[int, int, float]]:
    """Flag near-duplicate ("change only the name") drafts. Mutates ``flags`` on BOTH drafts of
    each flagged pair; returns ``(i, j, similarity)`` for every pair at or above ``threshold``.
    """
    prepared = [_prepare(d) for d in drafts]
    pairs: list[tuple[int, int, float]] = []
    for i in range(len(drafts)):
        if not drafts[i].body.strip():
            continue  # an empty draft (no groundable paper) can't be a name-swap of anything
        for j in range(i + 1, len(drafts)):
            if not drafts[j].body.strip():
                continue
            sim = _prepared_similarity(prepared[i], prepared[j])
            if sim >= threshold:
                pairs.append((i, j, sim))
                _add_flag(
                    drafts[i],
                    f"{_NEAR_DUP_PREFIX}reads {sim:.0%} identical to your draft to "
                    f"{drafts[j].candidate_name} — looks like one template with the name swapped. "
                    f"Rewrite the opening and ask so they are specific to this professor.",
                )
                _add_flag(
                    drafts[j],
                    f"{_NEAR_DUP_PREFIX}reads {sim:.0%} identical to your draft to "
                    f"{drafts[i].candidate_name} — looks like one template with the name swapped. "
                    f"Rewrite the opening and ask so they are specific to this professor.",
                )
    return pairs


def flag_institution_volume(
    drafts: list[OutreachDraft], *, max_per_institution: int = MAX_PER_INSTITUTION
) -> list[tuple[str, int]]:
    """Flag every draft belonging to an institution targeted by more than ``max_per_institution``
    drafts ("limit ~2 per department"). Returns ``(institution, count)`` per over-limit group.
    """
    groups: dict[str, list[OutreachDraft]] = defaultdict(list)
    for draft in drafts:
        key = " ".join(draft.institution.lower().split())
        if key:
            groups[key].append(draft)

    flagged: list[tuple[str, int]] = []
    for members in groups.values():
        if len(members) > max_per_institution:
            count = len(members)
            institution = members[0].institution
            flagged.append((institution, count))
            for draft in members:
                _add_flag(
                    draft,
                    f"{_VOLUME_PREFIX}{count} of your drafts target {draft.institution} — "
                    f"limit ~{max_per_institution} per department so it doesn't read as a mass "
                    f"mailing to one office.",
                )
    return flagged


def flag_spam(
    drafts: list[OutreachDraft],
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    max_per_institution: int = MAX_PER_INSTITUTION,
) -> list[OutreachDraft]:
    """Run both anti-spam checks over a plan's drafts, mutating their ``flags`` in place.

    Convenience entry point for the pipeline. Returns the same list for chaining.
    """
    flag_near_duplicates(drafts, threshold=threshold)
    flag_institution_volume(drafts, max_per_institution=max_per_institution)
    return drafts
