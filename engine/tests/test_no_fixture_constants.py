"""CI guard (#2): the Startup School fixture must NEVER leak into production routing/extraction/planning/
execution/presentation/verification logic. Fixtures live in tests + live-acceptance docs only. A
successful fixture must validate GENERAL behavior, not define it."""

from __future__ import annotations

import pathlib

FORBIDDEN = [
    "startup school", "y combinator", "chase center", "dhruv jain", "admit one",
    "july 25", "july 26", "2026-07-25", "2026-07-26", "2026-07-27",
]


def test_no_fixture_constants_in_production_code():
    root = pathlib.Path(__file__).resolve().parent.parent / "bruce_engine"
    hits = []
    for f in sorted(root.rglob("*.py")):
        low = f.read_text(encoding="utf-8").lower()
        for term in FORBIDDEN:
            if term in low:
                hits.append(f"{f.relative_to(root.parent)}: {term!r}")
    assert not hits, (
        "fixture-specific constants found in PRODUCTION code (move to tests/fixtures):\n  "
        + "\n  ".join(hits))
