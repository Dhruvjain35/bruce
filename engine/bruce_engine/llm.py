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
MODEL_CONVERSATION = "gpt-5.4-mini"  # vision-capable conversation brain (multimodal + structured out)
MODEL_ROUTING = "gpt-5.4-mini"       # compact Stage-1 router: text-only, tiny structured out, hard timeout

# Conversation-brain latency/cost bounds (env-overridable) — protect the "replies in a few seconds"
# promise. Timeout + one retry cap the tail; a timeout still yields exactly one honest fallback reply.
CONVERSATION_TIMEOUT_S = float(os.environ.get("BRUCE_CONVERSATION_TIMEOUT_S", "22"))
CONVERSATION_MAX_RETRIES = int(os.environ.get("BRUCE_CONVERSATION_MAX_RETRIES", "1"))

# Stage-1 router bounds — a HARD, short deadline (routing must be cheap): on breach the router falls back
# to the deterministic default, never blocking a turn. No retry (a slow route defeats the point).
ROUTING_TIMEOUT_S = float(os.environ.get("BRUCE_ROUTING_TIMEOUT_S", "2.0"))

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


# ONE provider per (api key) for the life of the process, and one model object per (provider, model id).
#
# MEASURED, not assumed: constructing `OpenAIChatModel(..., OpenAIProvider(...))` costs 5.45ms, and an
# `Agent` built over a fresh model costs 7.04ms against 1.52ms over a shared one. That is the cheap half.
# The expensive half is that each provider builds its own HTTP client, and the client connects lazily —
# so a per-call provider makes every first request pay a TLS handshake. `semantic_triage` measured that
# at 3003ms in Cloud Run, where it blew the deadline and produced the run's only mechanical fallback.
#
# SAFE TO SHARE because nothing user-scoped lives here. The OpenAI key is process configuration, not a
# student's credential; there is no account binding, no conversation state and no request argument on
# the provider. The Google path is different and is handled in `oauth_google` — there the token is
# request-scoped and only the transport is shared.
_PROVIDERS: dict[str, "OpenAIProvider"] = {}
_MODELS: dict[tuple[str, int], OpenAIChatModel] = {}


def _openai_provider(api_key: str) -> "OpenAIProvider":
    provider = _PROVIDERS.get(api_key)
    if provider is None:
        provider = OpenAIProvider(api_key=api_key)
        _PROVIDERS[api_key] = provider
    return provider


def openai_model(model_id: str) -> OpenAIChatModel:
    """OpenAI-backed model over the process-lifetime provider. Reads OPENAI_API_KEY from the environment.

    Keyed by the key's identity as well as the model id: a test that swaps `OPENAI_API_KEY` gets a fresh
    provider rather than silently reusing a transport authenticated with the old one.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set — load engine/.env at your entrypoint.")
    provider = _openai_provider(api_key)
    key = (model_id, id(provider))
    model = _MODELS.get(key)
    if model is None:
        model = OpenAIChatModel(model_id, provider=provider)
        _MODELS[key] = model
    return model


def reset_clients() -> None:
    """Drop the shared provider and model objects. Tests only — a process never needs this, and calling
    it in production would reintroduce the per-call handshake this exists to avoid."""
    _PROVIDERS.clear()
    _MODELS.clear()


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


def conversation_model() -> OpenAIChatModel:
    """Vision-capable conversation brain — OpenAI gpt-5.4-mini. Declares vision support to callers."""
    return openai_model(MODEL_CONVERSATION)


def routing_model() -> OpenAIChatModel:
    """Compact Stage-1 router model (NOT the strongest planner model) — a fast, text-only structured call."""
    return openai_model(MODEL_ROUTING)


# Role-based accessors — OFFLINE ONLY (Featherless). Each raises unless the flag is set, so a
# stray production caller fails loudly instead of silently taking the slow serverless path.
def featherless_extraction_model() -> OpenAIChatModel:
    _require_featherless()
    return featherless(MODEL_FEATHERLESS_EXTRACTION)


def featherless_drafting_model() -> OpenAIChatModel:
    _require_featherless()
    return featherless(MODEL_FEATHERLESS_DRAFTING)
