"""Grounded intake extraction (#2): forward anything school-related -> ExtractedIntake.

Turns ugly inputs (pasted text, email, PDF, screenshot/image) into deadlines, required items,
cost, location, contacts, links, and eligibility. Grounded: every deadline carries the verbatim
source span it came from, and after the model extracts we deterministically DROP any deadline
whose source span isn't actually present in the source text (anti-hallucination). Ambiguous or
relative dates ("Friday", "next week") are left unresolved and flagged, never guessed.

Routing (providers live behind neutral seams in intake_providers; the domain names no vendor).
PRODUCTION is OpenAI end-to-end — a latency decision (Featherless's serverless tail is multi-minute):

    image / screenshot         -> OpenAI vision transcribe -> OpenAI gpt-5.4-mini extract
    selectable-text PDF        -> local pdfplumber          -> OpenAI gpt-5.4-mini extract
    scanned / image-only PDF   -> OpenAI vision transcribe -> OpenAI gpt-5.4-mini extract
    pasted text / email        ->                              OpenAI gpt-5.4-mini extract

    retry (recorded, bounded): ONE more call to the SAME model on invalid output or all-ungrounded
    deadlines — never a cross-provider swap. Featherless is offline-only (eval/batch), flag-gated,
    and may be injected as `extractor=` for comparison; it never serves a production request.

The load-bearing invariant: a failure to READ is never reported as "read it, found nothing". Every
read failure is a typed, loud ExtractionError (415/422) or ProviderUnavailable (503) — never an
empty-but-successful 200. See tests/test_no_false_completion.py.
"""

from __future__ import annotations

import asyncio
import io

from .intake_metrics import IntakeTelemetry
from .intake_providers import (
    OpenAIVisionTranscriber,
    StructuredExtractor,
    TranscriptResult,
    production_extractor,
)
from .models import ExtractedDeadline, ExtractedIntake, IntakeSourceKind
from .provider_status import ProviderUnavailable

# Image types the vision transcriber accepts; anything else is a typed 415 up front (no tokens spent).
_SUPPORTED_IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/heic"}

# Below this many characters of extracted PDF text, we treat the PDF as scanned/image-only and route
# it to vision rather than surface a near-empty read.
_MIN_PDF_TEXT_CHARS = 24

# --- Latency budget (student-facing intake). Tail latency is the enemy; a bounded, recoverable
# failure beats an unbounded wait. See llm.py for why production is OpenAI-only.
#   mission acknowledgement   : < 1s   (handled by the mission flow: create + return immediately)
#   visible processing state  : immediate (phase persisted before any model call)
#   typical extraction result : target < 10s
#   hard timeout (recoverable): 20s -> raises ProviderUnavailable, client retries
INTAKE_HARD_TIMEOUT_S = 20
INTAKE_TARGET_MS = 10_000
MISSION_ACK_BUDGET_MS = 1_000


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
    text: str,
    source_kind: IntakeSourceKind,
    doc_type: str,
    transcription: "TranscriptResult | None" = None,
    extractor: "StructuredExtractor | None" = None,
    traffic: str = "production",
) -> tuple[ExtractedIntake, IntakeTelemetry]:
    """Normalized text -> grounded ExtractedIntake.

    Production uses the OpenAI extractor (``production_extractor()``); no Featherless, ever, on a
    synchronous path. ``extractor`` may be injected for OFFLINE eval/batch comparison only.

    The only retry is a bounded SECOND CALL TO THE SAME MODEL, on invalid output or all-ungrounded
    deadlines — never a cross-provider swap (that was the Featherless-fallback pattern we removed).
    A provider outage propagates as ProviderUnavailable; ambiguous/unverifiable fields are dropped
    and left for student review rather than papered over by a different model.
    """
    ext = extractor or production_extractor()
    fallback_reason: str | None = None
    retries = 0

    try:
        res = await ext.extract(text, source_kind)
    except ProviderUnavailable:
        raise  # honest outage — never mask it
    except Exception:
        # Unusable output (bad JSON / schema) -> one bounded retry on the SAME model.
        res = await ext.extract(text, source_kind)
        fallback_reason, retries = "invalid_output", 1

    proposed = len(res.intake.deadlines)
    kept = _verify_deadlines(res.intake.deadlines, text)
    grounding = _grounding_result(proposed, len(kept))

    # Bounded grounding retry: model proposed deadlines but none grounded -> one more same-model
    # sample (temperature variance can ground it). Still recorded; still no provider swap.
    if fallback_reason is None and grounding == "ungrounded" and proposed > 0:
        try:
            fb = await ext.extract(text, source_kind)
            fb_kept = _verify_deadlines(fb.intake.deadlines, text)
            fallback_reason, retries = "failed_grounding", 1
            if fb_kept:
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
        traffic=traffic,
    )
    if transcription is not None:
        telem.transcriber_provider = transcription.provider
        telem.transcriber_model = transcription.model
        telem.transcriber_latency_ms = transcription.latency_ms
        telem.transcriber_input_tokens = transcription.input_tokens
        telem.transcriber_output_tokens = transcription.output_tokens
    return res.intake, telem


# --------------------------------------------------------------------------- transcription seams


async def _transcribe_image(image_bytes: bytes, mime: str) -> "TranscriptResult":
    """Transcribe an image verbatim via the vision transcriber (OpenAI gpt-5.4-mini).

    Validates the mime BEFORE calling the provider (a 415 costs no tokens). Raises SourceParseError
    on an empty transcription — returning "" would turn a provider/auth failure into a silently
    empty intake ("read your flyer, found nothing"). It deliberately does NOT extract structure;
    that is the second pass's job, so spans are grounded against real transcribed text.
    """
    if mime not in _SUPPORTED_IMAGE_MIMES:
        raise UnsupportedSourceType(
            f"unsupported image type {mime!r}", detected=mime, supported=sorted(_SUPPORTED_IMAGE_MIMES)
        )
    result = await OpenAIVisionTranscriber().transcribe(image_bytes, mime)  # raises ProviderUnavailable
    if not result.text.strip():
        raise SourceParseError("vision returned an empty transcription", kind="image")
    return result


async def _image_to_text(image_bytes: bytes, mime: str = "image/png") -> str:
    """Thin text-only wrapper over _transcribe_image (back-compat for callers/tests)."""
    return (await _transcribe_image(image_bytes, mime)).text


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
# Each returns (ExtractedIntake, IntakeTelemetry) and enforces the intake latency budget: the whole
# intake (transcription + extraction + any bounded retry) is capped at INTAKE_HARD_TIMEOUT_S. On
# timeout it raises a RECOVERABLE ProviderUnavailable (the client retries) rather than hang the UI.
# Thin intake-only wrappers below preserve the older signatures for existing callers.


async def _budgeted(coro):
    """Run an intake coroutine under the hard latency budget. A timeout is a bounded, recoverable
    failure — never an unbounded wait."""
    try:
        return await asyncio.wait_for(coro, INTAKE_HARD_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise ProviderUnavailable(
            provider="openai",
            model=production_extractor().model,
            reason=f"intake exceeded the {INTAKE_HARD_TIMEOUT_S}s latency budget — retry",
            status_code=504,
        ) from exc


async def extract_from_text_traced(
    text: str,
    source_kind: IntakeSourceKind = IntakeSourceKind.text,
    *,
    extractor: "StructuredExtractor | None" = None,
    traffic: str = "production",
) -> tuple[ExtractedIntake, IntakeTelemetry]:
    text = (text or "").strip()
    if not text:
        # A direct text source that is genuinely empty is "the user sent nothing", not a read
        # failure — return an honest empty intake with zero-cost telemetry.
        return (
            ExtractedIntake(source_kind=source_kind),
            IntakeTelemetry(doc_type="text", provider="local", model="none", traffic=traffic),
        )
    return await _budgeted(
        _run_structured(text, source_kind, doc_type="text", extractor=extractor, traffic=traffic)
    )


async def extract_from_image_traced(
    image_bytes: bytes,
    mime: str = "image/png",
    *,
    extractor: "StructuredExtractor | None" = None,
    traffic: str = "production",
) -> tuple[ExtractedIntake, IntakeTelemetry]:
    async def _work():
        tr = await _transcribe_image(image_bytes, mime)  # raises on unsupported type / empty transcription
        return await _run_structured(
            tr.text, IntakeSourceKind.image, doc_type="image", transcription=tr,
            extractor=extractor, traffic=traffic,
        )

    return await _budgeted(_work())


async def extract_from_pdf_traced(
    data: bytes,
    *,
    extractor: "StructuredExtractor | None" = None,
    traffic: str = "production",
) -> tuple[ExtractedIntake, IntakeTelemetry]:
    async def _work():
        text = _pdf_to_text(data)  # raises on non-PDF / corrupt / missing dep; "" only if no text layer
        if len(text) >= _MIN_PDF_TEXT_CHARS:
            return await _run_structured(
                text, IntakeSourceKind.pdf, doc_type="pdf_text", extractor=extractor, traffic=traffic
            )
        # Scanned / image-only PDF: route to vision rather than surface a near-empty read.
        tr = await OpenAIVisionTranscriber().transcribe(data, "application/pdf")  # raises ProviderUnavailable
        if not tr.text.strip():
            raise SourceParseError("scanned/image-only PDF produced an empty transcription", kind="pdf")
        return await _run_structured(
            tr.text, IntakeSourceKind.pdf, doc_type="pdf_scanned", transcription=tr,
            extractor=extractor, traffic=traffic,
        )

    return await _budgeted(_work())


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
