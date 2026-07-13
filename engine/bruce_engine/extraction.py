"""Grounded intake extraction (#2): forward anything school-related -> ExtractedIntake.

Turns ugly inputs (pasted text, email, PDF, screenshot/image) into deadlines, required items,
cost, location, contacts, links, and eligibility. Grounded: every deadline carries the verbatim
source span it came from, and after the model extracts we deterministically DROP any deadline
whose source span isn't actually present in the source text (anti-hallucination). Ambiguous or
relative dates ("Friday", "next week") are left unresolved and flagged, never guessed.
"""

from __future__ import annotations

import base64
import io

from pydantic_ai import Agent, PromptedOutput

from .llm import extraction_model
from .models import ExtractedDeadline, ExtractedIntake, IntakeSourceKind

_SYSTEM = """You extract structured info from a school-related document (email, flyer, syllabus,
form, announcement, screenshot text). Rules:
- Extract ONLY what is present. NEVER invent a date, cost, requirement, contact, or link.
- For EVERY deadline, copy the EXACT verbatim text it came from into source_span.
- Put a date in ISO 8601 (YYYY-MM-DD) ONLY if it is unambiguous in the text. If a date is relative
  or ambiguous ("Friday", "next week", "the 3rd"), set date to null and add a note to ambiguities.
- Extract all required items (documents, forms, essays, fees, recommendations, tests).
- If a field is absent, leave it null/empty. Do not pad."""


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


async def extract_from_text(
    text: str, source_kind: IntakeSourceKind = IntakeSourceKind.text
) -> ExtractedIntake:
    text = (text or "").strip()
    if not text:
        return ExtractedIntake(source_kind=source_kind)
    agent = Agent(extraction_model(), output_type=PromptedOutput(ExtractedIntake), system_prompt=_SYSTEM)
    intake = (await agent.run(f"SOURCE ({source_kind.value}):\n{text}")).output
    intake.source_kind = source_kind
    intake.raw_source_excerpt = text[:1500]
    intake.deadlines = _verify_deadlines(intake.deadlines, text)
    return intake


def _pdf_to_text(data: bytes) -> str:
    if data[:5] != b"%PDF-":
        return ""
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages[:10])
    except Exception:
        return ""


async def extract_from_pdf(data: bytes) -> ExtractedIntake:
    return await extract_from_text(_pdf_to_text(data), source_kind=IntakeSourceKind.pdf)


async def _image_to_text(image_bytes: bytes, mime: str = "image/png") -> str:
    """OCR/transcribe an image via OpenAI vision, so the same grounded text path can run on it."""
    try:
        from openai import AsyncOpenAI
    except Exception:
        return ""
    b64 = base64.b64encode(image_bytes).decode()
    client = AsyncOpenAI()
    try:
        r = await client.responses.create(
            model="gpt-5.4-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Transcribe ALL text visible in this image verbatim "
                            "(a school flyer/screenshot/email). Output only the transcribed text.",
                        },
                        {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"},
                    ],
                }
            ],
        )
        return r.output_text or ""
    except Exception:
        return ""


async def extract_from_image(image_bytes: bytes, mime: str = "image/png") -> ExtractedIntake:
    text = await _image_to_text(image_bytes, mime)
    # Grounding note: the "source text" here is the OCR transcription, so spans are verified
    # against what vision read, not the raw pixels — lower assurance than text/PDF, by nature.
    return await extract_from_text(text, source_kind=IntakeSourceKind.image)
