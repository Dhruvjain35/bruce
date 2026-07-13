"""Opportunity engine (#1): turn already-extracted opportunities into ranked, actionable tasks.

Everything here is DETERMINISTIC and offline — it operates on ``ExtractedIntake`` objects that
extraction (#2) has already produced. That keeps the judgement layer (classify / spam / dedupe /
rank) fully testable without a network or an LLM: the only LLM-touching entry point is the async
``ingest_opportunity_text`` convenience, which just wires ``extraction.extract_from_text`` into the
same deterministic path.

Grounding still applies: we never invent a deadline, cost, or requirement — we only read what
extraction already grounded. Ranking prefers free, in-window opportunities that overlap the
student's stated interests, and pushes spam to the bottom rather than hiding it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from pydantic import BaseModel, Field

from .models import (
    ExtractedIntake,
    IntakeSourceKind,
    StudentProfile,
    Task,
    TaskKind,
)

__all__ = [
    "OPPORTUNITY_KINDS",
    "RankedOpportunity",
    "classify_opportunity",
    "is_spam",
    "dedupe_opportunities",
    "rank_opportunities",
    "opportunity_to_task",
    "ingest_opportunity_text",
]

# The classification vocabulary, exactly the strings this module may return.
OPPORTUNITY_KINDS = (
    "scholarship",
    "internship",
    "competition",
    "program",
    "research",
    "hackathon",
    "volunteering",
    "fellowship",
    "club",
    "other",
)

# Ordered most-specific -> least-specific. The FIRST bucket whose keyword appears wins, so more
# distinctive kinds (hackathon, scholarship) are checked before broad catch-alls (program). Keywords
# are matched on word boundaries against a whitespace-normalized, lowercased haystack, so "camp"
# never fires on "campus" and "reu" only fires as its own token.
_CLASSIFIER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hackathon", ("hackathon", "hackathons", "datathon", "makeathon", "ideathon",
                   "code jam", "codejam", "hackfest", "hack day", "hack night")),
    ("scholarship", ("scholarship", "scholarships", "bursary", "bursaries")),
    ("fellowship", ("fellowship", "fellowships", "fellows program", "fellow program")),
    ("internship", ("internship", "internships", "intern", "interns", "co-op", "summer intern")),
    ("research", ("research", "reu", "research assistant", "lab position",
                  "research experience for undergraduates")),
    ("competition", ("competition", "competitions", "contest", "contests", "olympiad",
                     "olympiads", "challenge", "challenges", "tournament", "tournaments",
                     "science fair", "hacks")),
    ("volunteering", ("volunteer", "volunteers", "volunteering", "community service",
                      "service opportunity", "service project")),
    ("club", ("club", "clubs", "society", "student organization", "student org", "chapter")),
    ("program", ("program", "programs", "bootcamp", "boot camp", "camp", "cohort", "academy",
                 "institute", "workshop", "course", "seminar", "masterclass", "pre-college")),
)

# Kinds that are really "apply to this" (vs. show-up events) become application Tasks by default.
_APPLICATION_KINDS = frozenset({"scholarship", "internship", "fellowship", "research", "program"})

# Hard scam markers — presence of any is spam on its own, regardless of other fields.
_SCAM_MARKERS = (
    "you have won", "you've won", "you have been selected as a winner", "click here to claim",
    "claim your prize", "claim your reward", "you are a winner", "you're a winner",
    "verify your account", "wire transfer", "no purchase necessary", "risk-free", "risk free",
    "100% free", "act now to claim", "gift card claim",
)

# Softer promotional markers — spam ONLY when the intake carries no legitimacy signal at all
# (no deadline, eligibility, required items, or contacts). A real competition mentioning "prize"
# still has a deadline/eligibility, so it is never caught here.
_PROMO_MARKERS = (
    "click here", "claim", "prize", "winner", "you won", "free money", "act now", "limited time",
    "hurry", "don't miss", "sign up now", "exclusive offer", "guaranteed", "gift card", "cash",
    "reward", "congratulations", "buy now", "order now", "special offer",
)

# Ranking weights (sum to 1.0, so a raw score already lands in [0, 1] before the spam penalty).
_WEIGHT_INTEREST = 0.6
_WEIGHT_COST = 0.2
_WEIGHT_DEADLINE = 0.2
_SPAM_MULTIPLIER = 0.1  # spam is down-ranked, never dropped, so the student can still see it


class RankedOpportunity(BaseModel):
    """One scored opportunity: the intake, a 0..1 fit score, and a one-sentence reason."""

    intake: ExtractedIntake
    fit_score: float = Field(ge=0.0, le=1.0)
    fit_reason: str


# ---------------------------------------------------------------------------
# small deterministic helpers
# ---------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    """Lowercase and collapse whitespace (same convention as extraction._norm)."""
    return " ".join((s or "").lower().split())


def _haystack(intake: ExtractedIntake) -> str:
    """The normalized text classify/rank read: title + summary + eligibility."""
    return _norm(" ".join(p for p in (intake.title, intake.summary, intake.eligibility) if p))


def _spam_haystack(intake: ExtractedIntake) -> str:
    """Wider text for spam detection: also folds in links and the raw excerpt."""
    parts = [intake.title, intake.summary, intake.eligibility, intake.raw_source_excerpt]
    parts.extend(intake.links)
    return _norm(" ".join(p for p in parts if p))


def _earliest_deadline_date(intake: ExtractedIntake) -> str | None:
    """Earliest ISO date across the intake's deadlines, or None if none carry a resolved date.

    ISO ``YYYY-MM-DD`` strings sort lexicographically the same as chronologically, so ``min`` is safe.
    """
    dates = [d.date for d in intake.deadlines if d.date]
    return min(dates) if dates else None


def _cost_signal(cost: str | None) -> str:
    """Classify a free-text cost into ``"free"`` | ``"paid"`` | ``"unknown"`` (free checked first)."""
    c = _norm(cost)
    if not c:
        return "unknown"
    free_markers = (
        "free", "no cost", "no fee", "no charge", "fully funded", "funded", "stipend",
        "paid position", "paid internship", "all expenses paid", "$0", "0 dollars",
    )
    if any(m in c for m in free_markers):
        return "free"
    # an explicit dollar amount (> 0) or fee/tuition wording means it costs the student money
    if re.search(r"\$\s*[1-9]", c) or re.search(r"\b[1-9][0-9]*\s*(?:dollars|usd)\b", c):
        return "paid"
    if any(m in c for m in ("fee", "tuition", "cost", "payment", "price", "$")):
        return "paid"
    return "unknown"


# ---------------------------------------------------------------------------
# public deterministic API
# ---------------------------------------------------------------------------


def classify_opportunity(intake: ExtractedIntake) -> str:
    """Classify an opportunity into one of :data:`OPPORTUNITY_KINDS` via keyword heuristics.

    Reads title + summary + eligibility. Returns the first matching bucket in most-specific order,
    or ``"other"`` when nothing matches.
    """
    hay = _haystack(intake)
    if not hay:
        return "other"
    for kind, keywords in _CLASSIFIER_RULES:
        pattern = r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b"
        if re.search(pattern, hay):
            return kind
    return "other"


def is_spam(intake: ExtractedIntake) -> bool:
    """Heuristically decide whether an intake is promotional spam rather than a real opportunity.

    True when either (a) a hard scam marker is present ("you have won", "click here to claim", ...),
    or (b) the intake is promotional in tone AND carries no legitimacy signal at all — no deadline,
    no eligibility, no required items, and no contacts.
    """
    hay = _spam_haystack(intake)
    if not hay:
        return False
    if any(m in hay for m in _SCAM_MARKERS):
        return True
    has_legit_signal = bool(
        intake.deadlines
        or (intake.eligibility and intake.eligibility.strip())
        or intake.required_items
        or intake.contacts
    )
    if has_legit_signal:
        return False
    return any(m in hay for m in _PROMO_MARKERS)


def dedupe_opportunities(intakes: list[ExtractedIntake]) -> list[ExtractedIntake]:
    """Drop near-duplicates, keyed by normalized title (+ earliest deadline date when present).

    Order is preserved and the first occurrence of each key is kept. Intakes with no title can't be
    keyed reliably, so they are always kept (we never merge two title-less items).
    """
    seen: set[tuple[str, str]] = set()
    out: list[ExtractedIntake] = []
    for intake in intakes:
        norm_title = _norm(intake.title)
        if not norm_title:
            out.append(intake)  # nothing to key on -> keep, don't risk a false merge
            continue
        key = (norm_title, _earliest_deadline_date(intake) or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(intake)
    return out


def _join_interests(items: list[str]) -> str:
    """Human list join: 'A', 'A and B', 'A, B, and C'."""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _fit_reason(matched: list[str], has_interests: bool, cost: str, deadline_status: str) -> str:
    """One-sentence explanation of the score (spam handled by the caller)."""
    clauses: list[str] = []
    if matched:
        clauses.append(f"matches your interest in {_join_interests(matched)}")
    elif has_interests:
        clauses.append("has no direct overlap with your listed interests")
    else:
        clauses.append("has no interests on file to match against")

    if cost == "free":
        clauses.append("is free to participate")
    elif cost == "paid":
        clauses.append("has a participation cost")

    if deadline_status == "future":
        clauses.append("and its deadline is still open")
    elif deadline_status == "past":
        clauses.append("but its deadline has already passed")

    return "This opportunity " + ", ".join(clauses) + "."


def rank_opportunities(
    intakes: list[ExtractedIntake],
    student: StudentProfile,
    *,
    today: date | None = None,
) -> list[RankedOpportunity]:
    """Score and sort opportunities by fit for ``student``, best first.

    Score = 0.6 * interest overlap + 0.2 * free-preference + 0.2 * deadline-in-window, each in
    ``[0, 1]``. Spam is multiplied by 0.1 so it sinks to the bottom without being hidden. ``today``
    is injected for deterministic testing; when omitted it defaults to the real current date.
    """
    today = today or date.today()  # only reached on the untested (live) path
    results: list[RankedOpportunity] = []

    for intake in intakes:
        hay = _haystack(intake)
        matched = [i for i in student.field_interests if _norm(i) and _norm(i) in hay]
        if student.field_interests:
            interest_score = len(matched) / len(student.field_interests)
        else:
            interest_score = 0.5  # neutral: nothing to match against

        cost = _cost_signal(intake.cost)
        cost_score = {"free": 1.0, "unknown": 0.6, "paid": 0.2}[cost]

        earliest = _earliest_deadline_date(intake)
        if not earliest:
            deadline_score, deadline_status = 0.6, "none"
        else:
            try:
                due = date.fromisoformat(earliest)
            except ValueError:
                deadline_score, deadline_status = 0.6, "none"
            else:
                if due < today:
                    deadline_score, deadline_status = 0.0, "past"
                else:
                    deadline_score, deadline_status = 1.0, "future"

        score = (
            _WEIGHT_INTEREST * interest_score
            + _WEIGHT_COST * cost_score
            + _WEIGHT_DEADLINE * deadline_score
        )

        spam = is_spam(intake)
        if spam:
            score *= _SPAM_MULTIPLIER
            reason = "This opportunity looks promotional or spam, so it is ranked at the bottom."
        else:
            reason = _fit_reason(matched, bool(student.field_interests), cost, deadline_status)

        score = max(0.0, min(1.0, round(score, 4)))
        results.append(RankedOpportunity(intake=intake, fit_score=score, fit_reason=reason))

    # stable sort: ties keep input order, highest score first
    results.sort(key=lambda r: r.fit_score, reverse=True)
    return results


def _slug_id(intake: ExtractedIntake) -> str:
    """Deterministic task id from normalized title + earliest deadline (stable across runs)."""
    basis = f"{_norm(intake.title)}|{_earliest_deadline_date(intake) or ''}"
    return "opp-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def opportunity_to_task(
    intake: ExtractedIntake,
    *,
    task_id: str | None = None,
    kind: TaskKind | None = None,
) -> Task:
    """Convert an opportunity intake into a canonical :class:`Task`.

    ``kind`` defaults to :attr:`TaskKind.application` for apply-to opportunities (scholarship /
    internship / fellowship / research / program) and :attr:`TaskKind.opportunity` otherwise.
    ``due`` is the earliest resolved deadline date; required items are deep-copied so mutating the
    task never touches the source intake; ``source`` is the first link if any.
    """
    if kind is None:
        kind = TaskKind.application if classify_opportunity(intake) in _APPLICATION_KINDS else TaskKind.opportunity

    title = (intake.title or "").strip()
    if not title:
        summary_line = (intake.summary or "").strip().splitlines()
        title = summary_line[0][:80] if summary_line else "Untitled opportunity"

    return Task(
        task_id=task_id or _slug_id(intake),
        kind=kind,
        title=title,
        due=_earliest_deadline_date(intake),
        required_items=[item.model_copy(deep=True) for item in intake.required_items],
        source=intake.links[0] if intake.links else None,
        notes=intake.summary,
    )


async def ingest_opportunity_text(
    text: str,
    student: StudentProfile,
    *,
    source_kind: IntakeSourceKind = IntakeSourceKind.text,
) -> dict:
    """Convenience end-to-end: extract an opportunity from raw text, then classify + taskify it.

    NOT unit-tested — this hits the LLM via ``extraction.extract_from_text``. The deterministic
    guts it calls (classify / is_spam / rank / opportunity_to_task) are what the tests cover.
    """
    from . import extraction  # local import: keep the LLM dependency off the deterministic path

    intake = await extraction.extract_from_text(text, source_kind=source_kind)
    ranked = rank_opportunities([intake], student)
    return {
        "intake": intake,
        "classification": classify_opportunity(intake),
        "is_spam": is_spam(intake),
        "fit": ranked[0] if ranked else None,
        "task": opportunity_to_task(intake),
    }
