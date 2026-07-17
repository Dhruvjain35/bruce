"""Score one intake against its gold case.

Three accuracy numbers, deliberately separated because they fail differently:

  * grounded field accuracy — of the deadlines that SHOULD be found, how many were found correctly
    (label matches, date matches when the gold pins one) AND survived the source-span grounding gate.
  * missed deadlines        — gold deadlines that were not surfaced at all (recall failures).
  * unsupported claims      — surfaced deadlines that match NO gold deadline, or that resolved a
    date the gold marked ambiguous. This is the safety metric; a hallucinated deadline is worse
    than a missed one because the student acts on it.

Latency and cost come straight off the IntakeTelemetry the pipeline already produced.
"""

from __future__ import annotations

import dataclasses

from bruce_engine.intake_metrics import IntakeTelemetry
from bruce_engine.models import ExtractedIntake

from .schema import GoldCase, GoldDeadline


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().split())


def _matches(gold: GoldDeadline, d) -> bool:
    """A surfaced deadline satisfies a gold deadline if the label substring matches and, when the
    gold pins a date, the date matches."""
    if _norm(gold.label_contains) not in _norm(getattr(d, "label", "")):
        return False
    if gold.date is not None and str(getattr(d, "date", "")) != gold.date:
        return False
    return True


@dataclasses.dataclass
class CaseScore:
    case: str
    doc_type: str
    provider: str
    model: str
    found: int  # gold deadlines correctly + groundedly surfaced
    total_expected: int
    missed: int  # gold deadlines not surfaced
    unsupported: int  # surfaced deadlines matching no gold, or resolving a forbidden ambiguous date
    required_items_hit: int
    required_items_expected: int
    latency_ms: int
    est_cost_usd: float
    fallback_reason: str | None
    grounding_result: str

    @property
    def grounded_field_accuracy(self) -> float:
        return (self.found / self.total_expected) if self.total_expected else 1.0


def score_case(
    name: str, gold: GoldCase, intake: ExtractedIntake, telem: IntakeTelemetry
) -> CaseScore:
    deadlines = list(intake.deadlines)

    found = sum(1 for g in gold.expect if any(_matches(g, d) for d in deadlines))
    missed = len(gold.expect) - found

    # Unsupported = surfaced deadlines that satisfy no gold deadline, PLUS any surfaced deadline
    # whose label/date contains a forbidden (trap) substring.
    forbid = [_norm(f) for f in gold.forbid]
    unsupported = 0
    for d in deadlines:
        supported = any(_matches(g, d) for g in gold.expect)
        blob = f"{_norm(getattr(d, 'label', ''))} {_norm(str(getattr(d, 'date', '')))}"
        tripped = any(f and f in blob for f in forbid)
        if tripped or not supported:
            unsupported += 1

    ritems = _norm(" | ".join(getattr(x, "label", str(x)) for x in getattr(intake, "required_items", [])))
    ritems_hit = sum(1 for want in gold.expect_required_items if _norm(want) in ritems)

    return CaseScore(
        case=name,
        doc_type=telem.doc_type,
        provider=telem.provider,
        model=telem.model,
        found=found,
        total_expected=len(gold.expect),
        missed=missed,
        unsupported=unsupported,
        required_items_hit=ritems_hit,
        required_items_expected=len(gold.expect_required_items),
        latency_ms=telem.latency_ms,
        est_cost_usd=telem.est_cost_usd,
        fallback_reason=telem.fallback_reason,
        grounding_result=telem.grounding_result,
    )


def aggregate(scores: list[CaseScore]) -> dict:
    """Roll per-case scores up by (provider, model, doc_type) — the axes routing decisions turn on."""
    buckets: dict[tuple[str, str, str], list[CaseScore]] = {}
    for s in scores:
        buckets.setdefault((s.provider, s.model, s.doc_type), []).append(s)

    out = {}
    for (prov, model, dt), group in sorted(buckets.items()):
        n = len(group)
        exp = sum(s.total_expected for s in group)
        out[f"{prov}/{model}/{dt}"] = {
            "cases": n,
            "grounded_field_accuracy": round(sum(s.found for s in group) / exp, 3) if exp else None,
            "missed_deadlines": sum(s.missed for s in group),
            "unsupported_claims": sum(s.unsupported for s in group),
            "avg_latency_ms": round(sum(s.latency_ms for s in group) / n, 1),
            "total_est_cost_usd": round(sum(s.est_cost_usd for s in group), 6),
            "fallbacks": sum(1 for s in group if s.fallback_reason),
        }
    return out
