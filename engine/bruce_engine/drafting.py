"""Personalized outreach drafting.

For each verified professor, produce ONE genuinely personalized email that reads like the
student spent an hour on it: references the professor's specific recent work, makes a real
fit case from the student's background, and sounds like the student (voice-matched).

Hard rules (enforced in the prompt + validated on output):
  * Every specific claim in the email must be supported by the candidate's evidence.
    No invented papers, results, or affiliations.
  * No mass-template feel. Two drafts for two professors must not be near-identical.
  * Match the student's voice_sample when provided; otherwise a clean earnest register.
  * status starts at DraftStatus.draft. The engine never sends. The student sends.
  * Surface anything unsure in ``flags`` (e.g. "confirm this is still their focus").

Prompt strategy and grounding enforcement land with the grounding research pass; the
signatures below are the stable contract the client + pipeline build against.
"""

from __future__ import annotations

from .models import OutreachDraft, OutreachGoal, ProfessorCandidate, StudentProfile


async def draft_one(
    student: StudentProfile, goal: OutreachGoal, candidate: ProfessorCandidate
) -> OutreachDraft:
    raise NotImplementedError(
        "draft_one: pending grounding research (workflow bruce-engine-research). "
        "Every claim must trace to candidate.evidence; voice-matched; never auto-sent."
    )


async def draft_all(
    student: StudentProfile, goal: OutreachGoal, candidates: list[ProfessorCandidate]
) -> list[OutreachDraft]:
    """Draft for each candidate. Drafts must be individually personalized, not templated."""
    raise NotImplementedError(
        "draft_all: pending grounding research (workflow bruce-engine-research)."
    )
