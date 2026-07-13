"""End-to-end smoke: discover -> draft -> verify. Prints the grounded, verified draft.

Usage: PYTHONPATH=. python scripts/smoke_draft.py "polariton chemistry"
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from bruce_engine.discovery import discover_professors
from bruce_engine.drafting import draft_one
from bruce_engine.models import OutreachGoal, OutreachType, StudentLevel, StudentProfile
from bruce_engine.verify import verify_draft


async def main(topic: str) -> None:
    student = StudentProfile(
        name="Dhruv Jain",
        level=StudentLevel.high_school,
        school="Northgate High School",
        field_interests=[topic],
        background=(
            "High-school researcher building ML methods for polariton / cavity-QED chemistry "
            "(a learned third-cumulant closure for light-matter dynamics). Comfortable with "
            "Python, PyTorch, and numerical simulation of open quantum systems."
        ),
    )
    goal = OutreachGoal(outreach_type=OutreachType.research_position, topic=topic, target_count=4)

    res = await discover_professors(student, goal, limit=4)
    if not res.candidates:
        print("no candidates found")
        return
    cand = res.candidates[0]
    n_abs = sum(1 for p in cand.recent_work if p.abstract_snippet)
    print(f"TOP CANDIDATE: {cand.name} — {cand.institution}")
    print(f"papers with abstracts: {n_abs}/{len(cand.recent_work)}\n")

    draft = await draft_one(student, cand)
    print("SUBJECT:", draft.subject)
    print("-" * 66)
    print(draft.body)
    print("-" * 66)
    print("grounding:", draft.personalization_points)

    verdict = await verify_draft(draft, cand)
    print("\nVERIFICATION:")
    print("  ready:      ", verdict.ready)
    print("  entailment: ", verdict.entailment)
    print("  problems:   ", verdict.problems)
    print("  unsupported:", verdict.unsupported_spans)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "polariton chemistry"))
