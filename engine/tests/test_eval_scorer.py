"""Offline tests for the eval scorer — validate the scoring logic without any API call.

The scorer is what turns raw intakes into the numbers a routing decision rests on, so its own logic
must be pinned: a hallucinated deadline counts as unsupported, an ambiguous date resolved to a
concrete one counts as unsupported, and a correctly grounded deadline counts as found.
"""

from __future__ import annotations

from bruce_engine.intake_metrics import IntakeTelemetry
from bruce_engine.models import ExtractedDeadline, ExtractedIntake, IntakeSourceKind

from eval.schema import GoldCase, GoldDeadline
from eval.score import aggregate, score_case


def _telem(**kw):
    base = dict(doc_type="flyer", provider="featherless", model="Qwen/Qwen3-32B", latency_ms=800)
    base.update(kw)
    return IntakeTelemetry(**base)


def _gold(expect=(), forbid=(), items=()):
    return GoldCase(
        doc_type="flyer", source="x.png", source_kind="image",
        expect=tuple(expect), forbid=tuple(forbid), expect_required_items=tuple(items),
    )


def test_correct_grounded_deadline_counts_as_found():
    gold = _gold(expect=[GoldDeadline(label_contains="registration", date="2026-05-01")])
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.image,
        deadlines=[ExtractedDeadline(label="Registration deadline", date="2026-05-01", source_span="due May 1", confidence=0.9)],
    )
    s = score_case("c", gold, intake, _telem())
    assert s.found == 1 and s.missed == 0 and s.unsupported == 0
    assert s.grounded_field_accuracy == 1.0


def test_missing_deadline_is_a_miss_not_an_error():
    gold = _gold(expect=[GoldDeadline(label_contains="registration", date="2026-05-01")])
    intake = ExtractedIntake(source_kind=IntakeSourceKind.image, deadlines=[])
    s = score_case("c", gold, intake, _telem())
    assert s.found == 0 and s.missed == 1 and s.unsupported == 0


def test_hallucinated_deadline_counts_as_unsupported():
    gold = _gold(expect=[GoldDeadline(label_contains="registration", date="2026-05-01")])
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.image,
        deadlines=[
            ExtractedDeadline(label="Registration deadline", date="2026-05-01", source_span="due May 1", confidence=0.9),
            ExtractedDeadline(label="Interview day", date="2026-06-01", source_span="fabricated", confidence=0.9),
        ],
    )
    s = score_case("c", gold, intake, _telem())
    assert s.found == 1 and s.unsupported == 1  # the invented interview deadline


def test_resolving_an_ambiguous_date_trips_the_forbid_trap():
    """Gold says 'the following Friday' must stay null; the model pinned 2026-05-08 -> unsupported."""
    gold = _gold(
        expect=[GoldDeadline(label_contains="project", date=None)],
        forbid=["2026-05-08"],
    )
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.image,
        deadlines=[ExtractedDeadline(label="Project due", date="2026-05-08", source_span="the following Friday", confidence=0.9)],
    )
    s = score_case("c", gold, intake, _telem())
    # It matches the gold label (found), but it also tripped the forbidden resolved date (unsupported).
    assert s.found == 1 and s.unsupported == 1


def test_aggregate_groups_by_provider_model_doctype():
    gold = _gold(expect=[GoldDeadline(label_contains="a", date=None)])
    intake = ExtractedIntake(
        source_kind=IntakeSourceKind.image,
        deadlines=[ExtractedDeadline(label="a", date=None, source_span="a", confidence=0.9)],
    )
    scores = [
        score_case("c1", gold, intake, _telem(latency_ms=1000)),
        score_case("c2", gold, intake, _telem(latency_ms=2000)),
    ]
    agg = aggregate(scores)
    key = "featherless/Qwen/Qwen3-32B/flyer"
    assert agg[key]["cases"] == 2
    assert agg[key]["grounded_field_accuracy"] == 1.0
    assert agg[key]["avg_latency_ms"] == 1500.0
