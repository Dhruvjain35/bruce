"""Engine orchestration: the deterministic spine of one outreach mission.

    discover  ->  (per candidate) resolve email  +  grounded draft  ->  verify

Discovery finds real professors + grounded papers. For each candidate we resolve an email
(out of band, never guessed), draft a grounded email, and run the fails-closed verification
gate. Nothing is sent — the student reviews, adds their own question, and sends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from . import antispam, discovery, drafting, email_resolver, verify
from .models import DraftStatus, MissionPhase, OutreachDraft, OutreachGoal, OutreachPlan, StudentProfile


async def build_outreach_plan(
    student: StudentProfile,
    goal: OutreachGoal,
    *,
    limit: int = 8,
    resolve_emails: bool = True,
    verify_drafts: bool = True,
    on_phase: Callable[[MissionPhase], Awaitable[None]] | None = None,
) -> OutreachPlan:
    async def emit(phase: MissionPhase) -> None:
        if on_phase is not None:
            await on_phase(phase)

    await emit(MissionPhase.understanding)
    result = await discovery.discover_professors(student, goal, limit=limit)

    await emit(MissionPhase.executing)

    async def process(candidate) -> OutreachDraft:
        # Per-candidate: resolve email + draft + verify. Wrapped so one failure never sinks the mission.
        try:
            if resolve_emails:
                await email_resolver.resolve_email(candidate)
            draft = await drafting.draft_one(student, candidate)
            if verify_drafts and draft.body:
                verdict = await verify.verify_draft(draft, candidate)
                if verdict.ready:
                    draft.flags.append("Verification passed: claims grounded in the cited paper.")
                else:
                    detail = "; ".join(verdict.problems) if verdict.problems else verdict.entailment
                    draft.flags.append(f"BLOCKED by verification ({verdict.entailment}): {detail}")
            return draft
        except Exception as exc:
            return OutreachDraft(
                candidate_name=candidate.name, institution=candidate.institution,
                subject="", body="", personalization_points=[], word_count=0,
                flags=[f"Could not prepare this one ({type(exc).__name__}) — skipped."],
                status=DraftStatus.draft,
            )

    # Candidates processed CONCURRENTLY — wall-clock ~= slowest single candidate, not the sum.
    drafts = list(await asyncio.gather(*(process(c) for c in result.candidates)))

    await emit(MissionPhase.verifying)
    antispam.flag_spam(drafts)  # name-swap + >2-per-institution flags, appended in place

    return OutreachPlan(student=student, goal=goal, discovery=result, drafts=drafts)
