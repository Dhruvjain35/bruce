"""Grounded intake extraction (#2): forward anything school-related -> ExtractedIntake.

Turns ugly inputs (pasted text, email, PDF, screenshot/image) into deadlines, required items,
cost, location, contacts, links, and eligibility. Grounded: every deadline carries the verbatim
source span it came from, and after the model extracts we deterministically DROP any deadline
whose source span isn't actually present in the source text (anti-hallucination). Ambiguous or
relative dates ("Friday", "next week") are left unresolved and flagged, never guessed.

Routing (providers live behind neutral seams in intake_providers; the domain names no vendor):

    image / screenshot         -> OpenAI vision transcribe -> Featherless Qwen3-32B extract
    selectable-text PDF        -> local pdfplumber          -> Featherless Qwen3-32B extract
    scanned / image-only PDF   -> OpenAI vision transcribe -> Featherless Qwen3-32B extract
    pasted text / email        ->                              Featherless Qwen3-32B extract

    fallback (recorded, bounded): OpenAI structured extract, ONLY after the Featherless primary
    produces invalid output or fails grounding. Never a silent per-request swap.

The load-bearing invariant: a failure to READ is never reported as "read it, found nothing". Every
read failure is a typed, loud ExtractionError (415/422) or ProviderUnavailable (503) — never an
empty-but-successful 200. See tests/test_no_false_completion.py.
"""

from __future__ import annotations

import io

from .intake_metrics import IntakeTelemetry
from .intake_providers import (
    FeatherlessExtractor,
    OpenAIExtractor,
    OpenAIVisionTranscriber,
)
from .models import ExtractedDeadline, ExtractedIntake, IntakeSourceKind
from .provider_status import ProviderUnavailable

# Image types the vision transcriber accepts; anything else is a typed 415 up front (no tokens spent).
_SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/heic"}

# Below this many characters of extracted PDF text, we treat the PDF as scanned/image-only and route
# it to vision rather than surface a near-empty read.
_MIN_PDF_TEXT_CHARS = 24


# --------------------------------------------------------------------------- typed read failures


class ExtractionError(Exception):
    """Base: intake could not be read. NEVER swallowed into an empty-but-successful result.

    A failure to READ something must never be reported as "read it, found nothing". Those are
    different facts, and only one is honest when the parser broke. Subclasses carry enough structure
    for the API to map to a precise status code without leaking any student content.
    """

    status_code = 422

    def as_detail(self) -> dict:
        return {"error": "source_parse_failed", "reason": str(self)}


class UnsupportedSourceType(ExtractionError):
    """The bytes are not a type Bruce can read at all -> 415, a client error."""

    status_code = 415

    def __init__(self, reason: str, *, detected: str | None = None, supported: list[str] | None = None):
        super().__init__(reason)
        self.detected = detected
        self.supported = supported or []

    def as_detail(self) -> dict:
        return {
            "error": "unsupported_source_type",
            "reason": str(self),
            "detected": self.detected,
            "supported": self.supported,
        }


class SourceParseError(ExtractionError):
    """The type is supported but this instance could not be read -> 422, unprocessable."""

    status_code = 422

    def __init__(self, reason: str, *, kind: str | None = None):
        super().__init__(reason)
        self.kind = kind


# --------------------------------------------------------------------------- grounding helpers


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _verify_deadlines(deadlines: list[ExtractedDeadline], text: str) -> list[ExtractedDeadline]:
    """Keep only deadlines whose source_span (or, as a fallback, label) is really in the text."""
    ntext = _norm(text)
    kept: list[ExtractedDeadline] = []
    for d in deadlines:
        span = _norm(d.source_span)
        if span and span in ntext:
            kept.append(d)
        elif d.label and _norm(d.label) in ntext:
            d.confidence = min(d.confidence, 0.5)  # label matched but span didn't — lower confidence
            kept.append(d)
        # else: unverifiable against the source -> drop (never surface a hallucinated deadline)
    return kept


def _grounding_result(proposed: int, kept: int) -> str:
    if proposed == 0:
        return "grounded"  # nothing proposed, nothing to ground — an honest empty read
    if kept == 0:
        return "ungrounded"
    return "partial" if kept < proposed else "grounded"


# --------------------------------------------------------------------------- structured extraction


async def _run_structured(
    text: str, source_kind: IntakeSourceKind, doc_type: str
) -> tuple[ExtractedIntake, IntakeTelemetry]:
    """Normalized text -> grounded ExtractedIntake, with a bounded, recorded OpenAI fallback.

    Featherless Qwen3-32B is the primary. OpenAI is used ONLY when the primary produces invalid
    output (any non-provider error) or when it proposed deadlines that all failed grounding. The
    fallback is never silent: telemetry carries fallback_reason and the model that actually answered.
    """
    fallback_reason: str | None = None
    retries = 0

    try:
        res = await FeatherlessExtractor().extract(text, source_kind)
    except ProviderUnavailable:
        raise  # honest outage — never mask it with a different provider
    except Exception:
        # Primary produced unusable output -> one bounded OpenAI fallback (may itself 503).
        res = await OpenAIExtractor().extract(text, source_kind)
        fallback_reason, retries = "invalid_output", 1

    proposed = len(res.intake.deadlines)
    kept = _verify_deadlines(res.intake.deadlines, text)
    grounding = _grounding_result(proposed, len(kept))

    # Bounded grounding fallback: the primary saw deadlines but none grounded -> one OpenAI look.
    if fallback_reason is None and grounding == "ungrounded" and proposed > 0:
        try:
            fb = await OpenAIExtractor().extract(text, source_kind)
            fb_kept = _verify_deadlines(fb.intake.deadlines, text)
            fallback_reason, retries = "failed_grounding", 1
            if fb_kept:  # only adopt the fallback if it actually grounded something
                res, kept = fb, fb_kept
                grounding = _grounding_result(len(fb.intake.deadlines), len(fb_kept))
        except ProviderUnavailable:
            pass  # keep the honest primary result (0 unverifiable deadlines surfaced)

    res.intake.deadlines = kept
    res.intake.source_kind = source_kind
    res.intake.raw_source_excerpt = text[:1500]
    telem = IntakeTelemetry(
        doc_type=doc_type,
        provider=res.provider,
        model=res.model,
        latency_ms=res.latency_ms,
        input_tokens=res.input_tokens,
        output_tokens=res.output_tokens,
        retries=retries,
        grounding_result=grounding,
        fallback_reason=fallback_reason,
    )
    return res.intake, telem


# --------------------------------------------------------------------------- transcription seams


async def _image_to_text(image_bytes: bytes, mime: str = "image/png") -> str:
    """Transcribe an image verbatim via the vision transcriber (OpenAI gpt-5.4-mini).

    Validates the mime BEFORE calling the provider (a 415 costs no tokens). Raises SourceParseError
    on an empty transcription — the previous implementation returned "", turning a provider/auth
    failure into a silently empty intake ("read your flyer, found nothing"). It deliberately does
    NOT extract structure; that is the second pass's job, so spans are grounded against real text.
    """
    if mime not in _SUPPORTED_IMAGE_MIMES:
        raise UnsupportedSourceType(
            f"unsupported image type {mime!r}", detected=mime, supported=sorted(_SUPPORTED_IMAGE_MIMES)
        )
    result = await OpenAIVisionTranscriber().transcribe(image_bytes, mime)  # raises ProviderUnavailable
    if not result.text.strip():
        raise SourceParseError("vision returned an empty transcription", kind="image")
    return result.text


def _pdf_to_text(data: bytes) -> str:
    """Extract the selectable text layer of a PDF via local pdfplumber. No model call.

    Raises (never returns "" on failure) so a corrupt or non-PDF upload is a typed error, not an
    empty read. Returns "" ONLY when the PDF is valid but has no text layer (scanned/image-only) —
    the caller routes that to vision. That is the one legitimate empty, and it is never surfaced to
    the student as a completed read.
    """
    if data[:5] != b"%PDF-":
        raise UnsupportedSourceType(
            "not a PDF (missing %PDF- header)", detected="unknown", supported=["application/pdf"]
        )
    try:
        import pdfplumber
    except ImportError as exc:
        raise SourceParseError("pdfplumber is not installed on this deployment", kind="pdf") from exc
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages[:10]).strip()
    except Exception as exc:
        raise SourceParseError(f"could not parse the PDF ({type(exc).__name__})", kind="pdf") from exc


# --------------------------------------------------------------------------- public entry points
# Each returns (ExtractedIntake, IntakeTelemetry). Thin non-traced wrappers below preserve the
# older intake-only signatures for existing callers.


async def extract_from_text_traced(
    text: str, source_kind: IntakeSourceKind = IntakeSourceKind.text
) -> tuple[ExtractedIntake, IntakeTelemetry]:
    text = (text or "").strip()
    if not text:
        # A direct text source that is genuinely empty is "the user sent nothing", not a read
        # failure — return an honest empty intake with zero-cost telemetry.
        return (
            ExtractedIntake(source_kind=source_kind),
            IntakeTelemetry(doc_type="text", provider="local", model="none"),
        )
    return await _run_structured(text, source_kind, doc_type="text")


async def extract_from_image_traced(
    image_bytes: bytes, mime: str = "image/png"
) -> tuple[ExtractedIntake, IntakeTelemetry]:
    text = await _image_to_text(image_bytes, mime)  # raises on unsupported type / empty transcription
    intake, telem = await _run_structured(text, IntakeSourceKind.image, doc_type="image")
    return intake, telem


async def extract_from_pdf_traced(data: bytes) -> tuple[ExtractedIntake, IntakeTelemetry]:
    text = _pdf_to_text(data)  # raises on non-PDF / corrupt / missing dep; "" only if no text layer
    if len(text) >= _MIN_PDF_TEXT_CHARS:
        return await _run_structured(text, IntakeSourceKind.pdf, doc_type="pdf_text")
    # Scanned / image-only PDF: route to vision rather than surface a near-empty read.
    result = await OpenAIVisionTranscriber().transcribe(data, "application/pdf")  # raises ProviderUnavailable
    if not result.text.strip():
        raise SourceParseError("scanned/image-only PDF produced an empty transcription", kind="pdf")
    return await _run_structured(result.text, IntakeSourceKind.pdf, doc_type="pdf_scanned")


# Intake-only wrappers (back-compat for existing callers that don't want telemetry).
async def extract_from_text(
    text: str, source_kind: IntakeSourceKind = IntakeSourceKind.text
) -> ExtractedIntake:
    intake, _ = await extract_from_text_traced(text, source_kind)
    return intake


async def extract_from_image(image_bytes: bytes, mime: str = "image/png") -> ExtractedIntake:
    intake, _ = await extract_from_image_traced(image_bytes, mime)
    return intake


async def extract_from_pdf(data: bytes) -> ExtractedIntake:
    intake, _ = await extract_from_pdf_traced(data)
    return intake
