"""Full mission end-to-end: build_outreach_plan.

discover -> resolve email -> grounded draft -> verify, for each candidate.
Usage: PYTHONPATH=. python scripts/smoke_mission.py "polariton chemistry" 2
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from bruce_engine.models import OutreachGoal, OutreachType, StudentLevel, StudentProfile
from bruce_engine.pipeline import build_outreach_plan


async def main(topic: str, limit: int) -> None:
    student = StudentProfile(
        name="Dhruv Jain",
        level=StudentLevel.high_school,
        school="Northgate High School",
        field_interests=[topic],
        background=(
            "High-school researcher building ML methods for polariton / cavity-QED chemistry "
            "(a learned third-cumulant closure for light-matter dynamics). Python, PyTorch, "
            "and numerical simulation of open quantum systems."
        ),
    )
    goal = OutreachGoal(outreach_type=OutreachType.research_position, topic=topic, target_count=limit)

    plan = await build_outreach_plan(student, goal, limit=limit)

    for i, (c, d) in enumerate(zip(plan.discovery.candidates, plan.drafts), 1):
        print(f"\n===== {i}. {c.name} — {c.institution}  (fit={c.fit_score}, send={c.recommend_send}) =====")
        print(f"email: {c.contact_email or 'NOT FOUND'}  (verified={c.email_verified})")
        print(f"SUBJECT: {d.subject}")
        print(d.body if d.body else "(no draft — no groundable paper with an abstract)")
        print("flags:", d.flags)


if __name__ == "__main__":
    topic = sys.argv[1] if len(sys.argv) > 1 else "polariton chemistry"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    asyncio.run(main(topic, limit))
