"""Engine orchestration: the deterministic spine.

    discover  ->  verify  ->  draft

The LLM proposes (discovery ranking, drafting); grounding + verification gate what reaches
the student. This ordering is the whole safety story: nothing is drafted about a professor
whose existence and cited work haven't been confirmed.
"""

from __future__ import annotations

from . import discovery, drafting, verify
from .models import OutreachGoal, OutreachPlan, StudentProfile


async def build_outreach_plan(student: StudentProfile, goal: OutreachGoal) -> OutreachPlan:
    # 1. Grounded discovery of real professors matching the goal.
    result = await discovery.discover_professors(student, goal)

    # 2. Verify every candidate + their cited work actually exist (anti-hallucination gate).
    result = await verify.verify_candidates(result)

    # 3. Draft one genuinely personalized email per verified candidate. Never sent here.
    drafts = await drafting.draft_all(student, goal, result.candidates)

    return OutreachPlan(student=student, goal=goal, discovery=result, drafts=drafts)
