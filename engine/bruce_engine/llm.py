"""LLM provider layer.

Provider-neutral by design: all model access goes through here, so swapping providers or
models is a config change, not an architecture change. Two providers, by role:

  * Featherless (open models, flat-rate) — cheap/high-volume TEXT steps (routing, structured
    extraction, drafting). Confirmed working via pydantic-ai OpenAIChatModel + PromptedOutput
    mode (default tool-calling 500s on some Featherless open models; PromptedOutput is robust).
  * OpenAI (frontier, metered) — three roles: (1) vision transcription (pixels -> verbatim text,
    the multimodal intake brain), (2) the safety-critical verification/entailment gate, and
    (3) a bounded structured-extraction fallback the intake orchestrator invokes ONLY after the
    Featherless primary produces invalid output or fails grounding. Never a silent per-request
    swap — a fallback is always recorded with its reason.

Alibaba Qwen Cloud is intentionally NOT a provider here. The open-weight Qwen models above run on
Featherless (flat-rate); the vendor "Qwen Cloud (DashScope)" is not used.

Model ids are verified against each provider's live /v1/models list — never hard-trust a
memorized id.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"

# Featherless (open) — checked against /v1/models 2026-07-13.
MODEL_ROUTING = "Qwen/Qwen3-30B-A3B-Instruct-2507"  # fast MoE for cheap classify/parse
MODEL_EXTRACTION = "Qwen/Qwen3-32B"                 # dense, reliable structured extraction
MODEL_DRAFTING = "Qwen/Qwen3-32B"                   # grounded drafting

# OpenAI (frontier) — verified available 2026-07-13.
MODEL_VERIFICATION = "gpt-5.4-mini"  # safety-critical entailment gate
MODEL_VISION = "gpt-5.4-mini"        # vision transcription (image/scanned-PDF -> verbatim text)
MODEL_FALLBACK = "gpt-5.4-mini"      # bounded structured-extraction fallback (recorded, not silent)


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


# Role-based accessors so callers don't hard-code provider/model choices.
def drafting_model() -> OpenAIChatModel:
    return featherless(MODEL_DRAFTING)


def extraction_model() -> OpenAIChatModel:
    return featherless(MODEL_EXTRACTION)


def fallback_model() -> OpenAIChatModel:
    """OpenAI structured-extraction fallback. Invoked only after the primary fails; recorded."""
    return openai_model(MODEL_FALLBACK)


def verification_model() -> OpenAIChatModel:
    return openai_model(MODEL_VERIFICATION)
