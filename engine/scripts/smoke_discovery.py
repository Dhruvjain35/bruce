"""Manual smoke test for grounded discovery.

Runs the OpenAlex-backed discovery on a real research interest and prints the real
professors + their real recent papers it finds. No LLM, no API key required (keyless
OpenAlex works at a reduced budget). Proves the grounding backbone returns real people.

Usage:
    PYTHONPATH=. python scripts/smoke_discovery.py "polariton chemistry"
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load engine/.env so OPENALEX_API_KEY (and later keys) are available to the client.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from bruce_engine.discovery import discover_professors
from bruce_engine.models import OutreachGoal, OutreachType, StudentLevel, StudentProfile


async def main(topic: str) -> None:
    student = StudentProfile(
        name="Test Student",
        level=StudentLevel.high_school,
        school="Test High School",
        field_interests=[topic],
        background="Independent ML + physics projects; some Python and research experience.",
    )
    goal = OutreachGoal(outreach_type=OutreachType.research_position, topic=topic, target_count=6)

    result = await discover_professors(student, goal, limit=6)

    print(f"\nTopic query: {topic!r}")
    print(f"Resolved via: {result.queries_used}")
    print(f"Candidates found: {len(result.candidates)}\n")
    for i, c in enumerate(result.candidates, 1):
        print(f"{i}. {c.name} — {c.institution}")
        print(f"   fit_score={c.fit_score:.2f} | {c.research_summary}")
        print(f"   profile: {c.profile_url}")
        for p in c.recent_work[:2]:
            yr = p.year or "?"
            print(f"     - [{yr}] {p.title}")
            if p.doi:
                print(f"       {p.doi}")
        print(f"   email: {c.contact_email or 'not resolved (never guessed)'}")
        print()


if __name__ == "__main__":
    topic = sys.argv[1] if len(sys.argv) > 1 else "polariton chemistry"
    asyncio.run(main(topic))
