"""Grounded professor discovery.

Given a student's goal, find REAL professors whose recent work fits — grounded in a
scholarly data source (OpenAlex / Semantic Scholar / etc.), never invented.

Design flow (to be finalized by the grounding research pass, workflow bruce-engine-research):
  1. Turn the goal.topic into search queries against a works/authors API.
  2. Rank authors by topical fit + recency + (optionally) that they currently advise/PI.
  3. For each candidate, pull their most recent papers as PaperRef evidence.
  4. Attach Evidence with real source URLs. Leave contact_email=None here — email
     discovery is a separate, careful step (see verify/email discovery); NEVER guess it.

Implementation is intentionally deferred until the research pass confirms current API
details — writing it from memorized API shapes is exactly how we'd hallucinate.
"""

from __future__ import annotations

from .models import DiscoveryResult, OutreachGoal, StudentProfile


async def discover_professors(student: StudentProfile, goal: OutreachGoal) -> DiscoveryResult:
    raise NotImplementedError(
        "discover_professors: pending grounding research (workflow bruce-engine-research). "
        "Must return only real professors with cited recent work; email left to a separate step."
    )
