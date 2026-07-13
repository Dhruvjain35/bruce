"""LLM provider layer.

Provider-neutral by design: all model access goes through here, so swapping providers or
models is a config change, not an architecture change. Two providers, by role:

  * Featherless (open models, flat-rate) — cheap/high-volume steps (routing, extraction,
    drafting). Confirmed working via pydantic-ai OpenAIChatModel + PromptedOutput mode
    (default tool-calling 500s on some Featherless open models; PromptedOutput is robust).
  * OpenAI (frontier, metered) — the safety-critical verification/entailment gate only.
    Low volume, so a strong model is worth the per-call cost. OpenAI supports native
    structured output, so verification uses the default output mode.

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

# OpenAI (frontier) — verified available 2026-07-13. Safety-critical entailment gate.
MODEL_VERIFICATION = "gpt-5.4-mini"


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


# Role-based accessors so callers don't hard-code provider/model choices.
def drafting_model() -> OpenAIChatModel:
    return featherless(MODEL_DRAFTING)


def extraction_model() -> OpenAIChatModel:
    return featherless(MODEL_EXTRACTION)


def verification_model() -> OpenAIChatModel:
    return openai_model(MODEL_VERIFICATION)
