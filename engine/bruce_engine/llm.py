"""LLM provider layer.

Provider-neutral by design: all model access goes through here, so swapping providers or
models is a config change, not an architecture change. Three providers, by role:

  * Qwen Cloud (Alibaba Model Studio) — the multimodal intake path (flyers/screenshots/forms).
    OpenAI-compatible, so it is an adapter, not a rewrite. See QWEN NOTES below.
  * Featherless (open models, flat-rate) — cheap/high-volume steps (routing, extraction,
    drafting). Confirmed working via pydantic-ai OpenAIChatModel + PromptedOutput mode
    (default tool-calling 500s on some Featherless open models; PromptedOutput is robust).
  * OpenAI (frontier, metered) — the safety-critical verification/entailment gate only.
    Low volume, so a strong model is worth the per-call cost. OpenAI supports native
    structured output, so verification uses the default output mode.

Model ids are verified against each provider's live /v1/models list — never hard-trust a
memorized id.

QWEN NOTES — every line here was verified against the live API on 2026-07-16, not memorized:

  * BASE URL: only ``dashscope-intl.aliyuncs.com/compatible-mode/v1`` authenticated our key.
    ``dashscope.aliyuncs.com`` and ``dashscope-us.aliyuncs.com`` both returned 401 with the same
    key, and one Alibaba doc page states the ``-us`` host — it is wrong for this account. A third
    documented form is workspace-scoped (``{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/
    compatible-api/v1``). Hence QWEN_BASE_URL is configuration, never a constant.
  * ENTITLEMENT: model ids appearing in GET /models does NOT mean they are callable. Our key lists
    149 models and every one of them (incl. qwen-turbo) returned
    403 AccessDenied.Unpurchased until Model Studio -> Model Inference is activated on the account.
    A 403 here is an account/billing state, NOT a bug in this file.
  * NON-THINKING: intake sends ``enable_thinking: false``. Alibaba's docs say json_object does not
    error in thinking mode, but that "some models may return invalid JSON in thinking mode" — so a
    thinking response must never be relied on to BE the action JSON.
  * JSON MODE: with ``response_format={"type":"json_object"}`` the request is REJECTED unless the
    literal word "json" appears in the messages. The intake system prompt therefore says "JSON"
    on purpose — do not "clean that up" out of the prompt.
"""

from __future__ import annotations

import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

FEATHERLESS_BASE_URL = "https://api.featherless.ai/v1"
QWEN_BASE_URL_DEFAULT = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

# Featherless (open) — checked against /v1/models 2026-07-13.
MODEL_ROUTING = "Qwen/Qwen3-30B-A3B-Instruct-2507"  # fast MoE for cheap classify/parse
MODEL_EXTRACTION = "Qwen/Qwen3-32B"                 # dense, reliable structured extraction
MODEL_DRAFTING = "Qwen/Qwen3-32B"                   # grounded drafting

# OpenAI (frontier) — verified available 2026-07-13. Safety-critical entailment gate.
MODEL_VERIFICATION = "gpt-5.4-mini"

# Qwen Cloud — both ids confirmed PRESENT in the live dashscope-intl /models list 2026-07-16.
# qwen3.7-plus is the flagship multimodal model (text+image+video); qwen3.6-flash is the cheaper
# path to switch to once the intake flow is stable.
QWEN_INTAKE_MODEL_DEFAULT = "qwen3.7-plus"
QWEN_RERANK_MODEL_DEFAULT = "qwen3-rerank"

# Which provider serves multimodal/text intake. Qwen is the default (this is the Qwen Cloud
# build); "openai" is kept ONLY as the evaluation baseline for Phase Q2 A/B numbers.
INTAKE_PROVIDER_DEFAULT = "qwen"


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


def qwen_base_url() -> str:
    return os.environ.get("QWEN_BASE_URL") or QWEN_BASE_URL_DEFAULT


def qwen_intake_model_id() -> str:
    return os.environ.get("QWEN_INTAKE_MODEL") or QWEN_INTAKE_MODEL_DEFAULT


def qwen_rerank_model_id() -> str:
    return os.environ.get("QWEN_RERANK_MODEL") or QWEN_RERANK_MODEL_DEFAULT


def qwen(model_id: str, *, http_client=None) -> OpenAIChatModel:
    """Qwen Cloud (Alibaba Model Studio) model over its OpenAI-compatible endpoint.

    Reads DASHSCOPE_API_KEY + QWEN_BASE_URL from the environment — never hard-code either; the
    correct host is account/region dependent (see QWEN NOTES at the top of this module).
    ``http_client`` is injectable so tests can assert the exact outgoing wire format without a key.
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY not set — load engine/.env at your entrypoint.")
    kwargs = {"base_url": qwen_base_url(), "api_key": api_key}
    if http_client is not None:
        kwargs["http_client"] = http_client
    return OpenAIChatModel(model_id, provider=OpenAIProvider(**kwargs))


# Role-based accessors so callers don't hard-code provider/model choices.
def drafting_model() -> OpenAIChatModel:
    return featherless(MODEL_DRAFTING)


def extraction_model() -> OpenAIChatModel:
    return featherless(MODEL_EXTRACTION)


def intake_provider() -> str:
    return (os.environ.get("BRUCE_INTAKE_PROVIDER") or INTAKE_PROVIDER_DEFAULT).strip().lower()


def intake_model(*, http_client=None) -> OpenAIChatModel:
    """The model serving student intake (text or image), selected by configuration.

    'qwen' (default) is the Qwen Cloud build. 'openai'/'featherless' exist ONLY so Phase Q2 can
    measure Qwen against a baseline on the same fixtures — they are not the shipped path.
    Featherless serves open Qwen weights and is TEXT-ONLY here; it cannot serve the image path.
    """
    provider = intake_provider()
    if provider == "qwen":
        return qwen(qwen_intake_model_id(), http_client=http_client)
    if provider == "openai":
        return openai_model(MODEL_VERIFICATION)
    if provider == "featherless":
        return featherless(MODEL_EXTRACTION)
    raise RuntimeError(
        f"BRUCE_INTAKE_PROVIDER={provider!r} is not one of: qwen, openai, featherless"
    )


def verification_model() -> OpenAIChatModel:
    return openai_model(MODEL_VERIFICATION)
