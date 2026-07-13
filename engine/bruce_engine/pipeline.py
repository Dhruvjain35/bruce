"""Engine orchestration: the deterministic spine of one outreach mission.

    discover  ->  (per candidate) resolve email  +  grounded draft  ->  verify

Discovery finds real professors + grounded papers. For each candidate we resolve an email
(out of band, never guessed), draft a grounded email, and run the fails-closed verification
gate. Nothing is sent — the student reviews, adds their own question, and sends.
"""

from __future__ import annotations

from collections.abc import Callable

from . import antispam, discovery, drafting, email_resolver, verify
from .models import MissionPhase, OutreachGoal, OutreachPlan, StudentProfile


async def build_outreach_plan(
    student: StudentProfile,
    goal: OutreachGoal,
    *,
    limit: int = 8,
    resolve_emails: bool = True,
    verify_drafts: bool = True,
    on_phase: Callable[[MissionPhase], None] | None = None,
) -> OutreachPlan:
    emit = on_phase or (lambda _p: None)

    emit(MissionPhase.understanding)
    result = await discovery.discover_professors(student, goal, limit=limit)

    emit(MissionPhase.executing)
    drafts = []
    for candidate in result.candidates:
        if resolve_emails:
            await email_resolver.resolve_email(candidate)  # mutates candidate; leaves None if not grounded

        draft = await drafting.draft_one(student, candidate)

        if verify_drafts and draft.body:
            verdict = await verify.verify_draft(draft, candidate)
            if verdict.ready:
                draft.flags.append("Verification passed: claims grounded in the cited paper.")
            else:
                detail = "; ".join(verdict.problems) if verdict.problems else verdict.entailment
                draft.flags.append(f"BLOCKED by verification ({verdict.entailment}): {detail}")

        drafts.append(draft)

    antispam.flag_spam(drafts)  # name-swap + >2-per-institution flags, appended in place

    return OutreachPlan(student=student, goal=goal, discovery=result, drafts=drafts)
