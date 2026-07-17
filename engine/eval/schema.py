"""Gold-label format for one evaluation document.

A case names a source file, its type, and what a correct intake MUST and MUST NOT contain. The
must-not (``forbid``) list is the point: Bruce's whole claim is that it does not invent deadlines or
resolve an ambiguous date it was never given, so the eval scores hallucinations as hard as misses.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class GoldDeadline:
    """One deadline the correct intake must surface."""

    label_contains: str  # normalized substring the extracted label must contain
    date: str | None = None  # ISO date the extractor must resolve, or None if the source is ambiguous
    span_contains: str | None = None  # substring that must appear in the extracted source_span


@dataclasses.dataclass(frozen=True)
class GoldCase:
    doc_type: str  # flyer | screenshot | pdf_text | pdf_scanned  (maps to intake doc_type family)
    source: str  # path relative to the case file, OR inline text (see `inline_text`)
    source_kind: str  # image | pdf | text
    mime: str = "image/png"
    inline_text: str | None = None  # if set, `source` is ignored and this text is fed directly
    expect: tuple[GoldDeadline, ...] = ()  # deadlines that MUST be found (grounded)
    forbid: tuple[str, ...] = ()  # normalized substrings that must NOT appear in any surfaced
    # deadline label/date — hallucination traps (e.g. a concrete date for "the following Friday")
    expect_required_items: tuple[str, ...] = ()  # substrings that should appear among required items
    notes: str = ""

    @staticmethod
    def load(path: Path) -> "GoldCase":
        raw = json.loads(Path(path).read_text())
        raw["expect"] = tuple(GoldDeadline(**d) for d in raw.get("expect", []))
        raw["forbid"] = tuple(raw.get("forbid", []))
        raw["expect_required_items"] = tuple(raw.get("expect_required_items", []))
        return GoldCase(**raw)


def load_cases(cases_dir: Path) -> list[tuple[Path, GoldCase]]:
    """Load every *.json case in a directory, sorted by name for a stable report order."""
    return [(p, GoldCase.load(p)) for p in sorted(Path(cases_dir).glob("*.json"))]
