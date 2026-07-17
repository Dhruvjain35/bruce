"""LLM provider layer.

Provider-neutral by design: all model access goes through here, so swapping providers or
models is a config change, not an architecture change.

PRODUCTION runs entirely on OpenAI. Every synchronous, student-facing step — vision
transcription, structured extraction, drafting, verification — uses gpt-5.4-mini. This is a
deliberate LATENCY decision, not a cost one: live measurement showed the Featherless serverless
path at ~34s steady-state with a 252s cold-start tail, and one random four-minute wait destroys
the "hand it to Bruce and it starts working" promise. Tail latency, not average, is the enemy.

Featherless (open-weight Qwen on a flat-rate plan) is kept ONLY for offline work — batch
evaluations, overnight processing, model comparisons, non-urgent backfills — and is DISABLED by
default. It is never on a synchronous production request path and is never a silent fallback.
Reaching it requires BRUCE_ENABLE_FEATHERLESS to be set AND an explicit accessor; otherwise it
raises. Bruce runs without a Featherless key.

Alibaba Qwen Cloud is not a provider here at all.

Model ids are verified against each provider's live /v1/models list — never hard-trust a
memorized id.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"

# --- PRODUCTION (OpenAI, every synchronous student-facing step) — verified available 2026-07-13.
MODEL_VISION = "gpt-5.4-mini"        # vision transcription (image/scanned-PDF -> verbatim text)
MODEL_EXTRACTION = "gpt-5.4-mini"    # structured task/date extraction
MODEL_DRAFTING = "gpt-5.4-mini"      # grounded drafting
MODEL_VERIFICATION = "gpt-5.4-mini"  # safety-critical entailment gate

# --- OFFLINE ONLY (Featherless open-weight Qwen) — flag-gated, never on a production path.
MODEL_FEATHERLESS_EXTRACTION = "Qwen/Qwen3-32B"
MODEL_FEATHERLESS_DRAFTING = "Qwen/Qwen3-32B"
MODEL_FEATHERLESS_ROUTING = "Qwen/Qwen3-30B-A3B-Instruct-2507"


def featherless_enabled() -> bool:
    """True only when BRUCE_ENABLE_FEATHERLESS is explicitly set. Default OFF — production never
    touches Featherless, so its absence (or a missing key) must not degrade the product."""
    return os.environ.get("BRUCE_ENABLE_FEATHERLESS", "").strip().lower() in {"1", "true", "yes", "on"}


def _require_featherless() -> None:
    if not featherless_enabled():
        raise RuntimeError(
            "Featherless is disabled. It is an offline-only provider (eval/batch/backfill); set "
            "BRUCE_ENABLE_FEATHERLESS=1 to use it there. It must never serve a production request."
        )


def featherless(model_id: str) -> OpenAIChatModel:
    """Featherless-backed model. Reads FEATHERLESS_API_KEY from the environment."""
    api_key = os.environ.get("FEATHERLESS_API_KEY")
    if not api_key:
        raise RuntimeError("FEATHERLESS_API_KEY not set — load engine/.env at your entrypoint.")
    return OpenAIChatModel(
        model_id, provider=OpenAIProvider(base_url=FEATHERLESS_BASE_URL, api_key=api_key)
    )


def openai_model(model_id: str) -> OpenAIChatModel:
    """OpenAI-backed model. Reads OPENAI_API_KEY from the environment."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set — load engine/.env at your entrypoint.")
    return OpenAIChatModel(model_id, provider=OpenAIProvider(api_key=api_key))


def vision_client():
    """Raw AsyncOpenAI client for the vision transcriber (Responses API, image/PDF input).

    Vision transcription uses the Responses API directly rather than a pydantic-ai chat model, so
    it needs the SDK client — but it still lives behind the VisionTranscriber seam, so the domain
    never sees this type. Reads OPENAI_API_KEY from the environment."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set — load engine/.env at your entrypoint.")
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key)


# Role-based accessors — PRODUCTION (all OpenAI). Callers never hard-code provider/model choices.
def extraction_model() -> OpenAIChatModel:
    return openai_model(MODEL_EXTRACTION)


def drafting_model() -> OpenAIChatModel:
    return openai_model(MODEL_DRAFTING)


def verification_model() -> OpenAIChatModel:
    return openai_model(MODEL_VERIFICATION)


# Role-based accessors — OFFLINE ONLY (Featherless). Each raises unless the flag is set, so a
# stray production caller fails loudly instead of silently taking the slow serverless path.
def featherless_extraction_model() -> OpenAIChatModel:
    _require_featherless()
    return featherless(MODEL_FEATHERLESS_EXTRACTION)


def featherless_drafting_model() -> OpenAIChatModel:
    _require_featherless()
    return featherless(MODEL_FEATHERLESS_DRAFTING)
