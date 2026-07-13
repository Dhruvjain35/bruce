"""Verification — the anti-hallucination gate.

Before any candidate reaches the drafting step, confirm the person and their cited work
actually exist and the claims are supported by the attached evidence. A candidate that
fails verification is dropped, not shown. This is what makes "grounded" real instead of
a marketing word.

Checks (final set pending the grounding research pass):
  * Every PaperRef resolves to a real record (DOI / source URL reachable, title matches).
  * The professor's profile/affiliation is corroborated by at least one Evidence item.
  * contact_email, if present, has a real email_source and is NOT a guessed/pattern-filled
    address. If it can't be grounded, email_verified stays False and the client asks the
    student to confirm it.
"""

from __future__ import annotations

from .models import DiscoveryResult


async def verify_candidates(result: DiscoveryResult) -> DiscoveryResult:
    raise NotImplementedError(
        "verify_candidates: pending grounding research (workflow bruce-engine-research). "
        "Drop any candidate whose person or cited papers cannot be confirmed against a real source."
    )
