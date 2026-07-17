"""Offline tests for extraction grounding (no network)."""

import pytest

from bruce_engine.extraction import UnsupportedSourceType, _norm, _pdf_to_text, _verify_deadlines
from bruce_engine.models import ExtractedDeadline


def test_verify_drops_hallucinated_span():
    text = "Applications for the summer program are due May 15, 2026. Bring a signed form."
    real = ExtractedDeadline(
        label="Application deadline", date="2026-05-15", source_span="due May 15, 2026", confidence=0.9
    )
    fake = ExtractedDeadline(
        label="Interview day", date="2026-06-01", source_span="interviews held June 1", confidence=0.9
    )
    kept = _verify_deadlines([real, fake], text)
    assert real in kept
    assert fake not in kept  # its source span isn't in the text -> dropped


def test_verify_label_fallback_lowers_confidence():
    text = "The chemistry review session is Friday afternoon."
    d = ExtractedDeadline(
        label="chemistry review session", date=None, source_span="paraphrased not verbatim", confidence=0.95
    )
    kept = _verify_deadlines([d], text)
    assert len(kept) == 1
    assert kept[0].confidence <= 0.5  # label matched but span didn't -> confidence lowered


def test_verify_empty():
    assert _verify_deadlines([], "anything") == []


def test_norm_collapses_whitespace_and_case():
    assert _norm("  Due   MAY 15 ") == "due may 15"


def test_pdf_to_text_rejects_non_pdf():
    """CONTRACT CHANGE (2026-07-17): non-PDF bytes now RAISE instead of returning "".

    The old assertion (`== ""`) encoded the false-completion bug: "" flowed into
    extract_from_text, which returns an empty ExtractedIntake for empty input, so a wrong-type
    upload produced a 200 with zero deadlines — indistinguishable from "Bruce read your file and
    it contained nothing". A failure to READ must never render as "read it, found nothing".
    See tests/test_no_false_completion.py for the full invariant.
    """
    with pytest.raises(UnsupportedSourceType):
        _pdf_to_text(b"not a pdf")
