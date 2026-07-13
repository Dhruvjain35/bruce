"""LLM provider layer.

Provider-neutral by design: all model access goes through here, so swapping providers or
models is a config change, not an architecture change.

CONFIRMED WORKING 2026-07-13 over Featherless (OpenAI-compatible, api.featherless.ai/v1):
  - pydantic-ai `OpenAIChatModel` + `OpenAIProvider(base_url=..., api_key=...)`
  - `PromptedOutput` mode for validated structured output. We default to PromptedOutput
    because default tool-calling structured output 500s on some Featherless open models
    (e.g. the Qwen3-30B MoE); Qwen3-32B handles both, but PromptedOutput is robust across models.

Build structured-output agents like:

    from pydantic_ai import Agent, PromptedOutput
    from bruce_engine.llm import featherless, MODEL_DRAFTING
    agent = Agent(featherless(MODEL_DRAFTING), output_type=PromptedOutput(MyModel))

Model ids are verified against the live /v1/models list — never hard-trust a memorized id.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"

# Featherless model ids confirmed present in /v1/models on 2026-07-13.
MODEL_ROUTING = "Qwen/Qwen3-30B-A3B-Instruct-2507"  # fast MoE for cheap classify/parse (PromptedOutput)
MODEL_EXTRACTION = "Qwen/Qwen3-32B"                 # dense, reliable structured extraction
MODEL_DRAFTING = "Qwen/Qwen3-32B"                   # grounded drafting
# Verification/entailment should move to a frontier model (Claude, Anthropic direct) once an
# ANTHROPIC_API_KEY exists. Until then it runs on a strong open model and is PROVISIONAL.
MODEL_VERIFICATION = "Qwen/Qwen3-32B"


def featherless(model_id: str) -> OpenAIChatModel:
    """Featherless-backed pydantic-ai model. Reads FEATHERLESS_API_KEY from the environment."""
    api_key = os.environ.get("FEATHERLESS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FEATHERLESS_API_KEY not set — load engine/.env at your entrypoint before building a model."
        )
    return OpenAIChatModel(
        model_id,
        provider=OpenAIProvider(base_url=FEATHERLESS_BASE_URL, api_key=api_key),
    )
