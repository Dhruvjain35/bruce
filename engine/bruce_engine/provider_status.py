"""Provider availability — classify upstream model failures HONESTLY.

Why this module exists: Bruce's whole promise is that it PROVES a result. So when a model provider
fails, Bruce must say that plainly rather than dressing it up as an empty success.

Two rules, both deliberate:

  1. A provider outage surfaces as ``provider_unavailable`` with the real reason and the real
     provider/model — never as a generic 502, and never as an empty-but-successful extraction. An
     empty intake that looks like "Bruce read your flyer and found nothing" is a false completion.
  2. There is NO silent fallback to another provider that lies about who answered. The intake
     orchestrator MAY retry structured extraction on OpenAI after the Featherless primary produces
     invalid output or fails grounding — but that fallback is always RECORDED (fallback_reason +
     the model that actually answered), never an invisible swap. Provider selection for a whole
     deployment is an explicit operator choice, not an implicit per-request decision.

The reason strings here are shown to clients, so they must never contain the API key, the request
body, or any student content — provider + model + a short cause only.
"""

from __future__ import annotations

import dataclasses

import httpx
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

# Provider code when an account/order is risk-suspended or a model was never activated.
ACCESS_DENIED_CODES = ("AccessDenied.Unpurchased", "AccessDenied")


@dataclasses.dataclass(frozen=True)
class ProviderUnavailable(Exception):
    """The configured model provider could not serve the request. Fail closed, never lie."""

    provider: str
    model: str
    reason: str
    status_code: int | None = None

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}: {self.reason}"

    def as_detail(self) -> dict:
        """Client-facing payload. Contains no key material, no request body, no student content."""
        return {
            "error": "provider_unavailable",
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "status_code": self.status_code,
        }


def _body_code(body: object | None) -> str | None:
    """Pull the provider's error code out of a response body, tolerating any shape."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("code"):
            return str(err["code"])
        if body.get("code"):
            return str(body["code"])
    return None


def _for_status(provider: str, model: str, status: int, code: str | None = None) -> ProviderUnavailable:
    if status == 403 or code in ACCESS_DENIED_CODES:
        return ProviderUnavailable(
            provider=provider, model=model,
            reason=(
                f"access denied by provider ({code or 'AccessDenied'}) — the account is not "
                "entitled to run inference on this model."
            ),
            status_code=403,
        )
    if status in (401, 407):
        return ProviderUnavailable(
            provider=provider, model=model,
            reason="provider rejected the credentials", status_code=status,
        )
    if status == 429:
        return ProviderUnavailable(
            provider=provider, model=model,
            reason="provider rate limit or quota exhausted", status_code=429,
        )
    if status >= 500:
        return ProviderUnavailable(
            provider=provider, model=model,
            reason=f"provider server error (HTTP {status})", status_code=status,
        )
    return ProviderUnavailable(
        provider=provider, model=model,
        reason=f"provider rejected the request (HTTP {status})", status_code=status,
    )


def classify(exc: Exception, *, provider: str, model: str) -> ProviderUnavailable | None:
    """Map an upstream exception to ProviderUnavailable, or None if it is not a provider problem.

    None means "this is our bug or bad data, not the provider" — the caller should not disguise it
    as an outage. Handles both pydantic-ai's exception types (Featherless text path) and the OpenAI
    SDK's own types (raw vision client), plus httpx transport errors.
    """
    if isinstance(exc, ModelHTTPError):
        return _for_status(provider, model, exc.status_code, _body_code(exc.body))
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return ProviderUnavailable(
            provider=provider, model=model, reason=f"provider unreachable ({type(exc).__name__})",
        )
    if isinstance(exc, ModelAPIError):
        return ProviderUnavailable(
            provider=provider, model=model, reason=f"provider API error ({type(exc).__name__})",
        )
    # OpenAI SDK errors (raw vision/fallback client) are duck-typed: they expose .status_code.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        code = _body_code(getattr(exc, "body", None))
        return _for_status(provider, model, status, code)
    # Connection-class errors from the OpenAI SDK / httpx, matched by name to avoid a hard import.
    if type(exc).__name__ in {"APIConnectionError", "APITimeoutError"}:
        return ProviderUnavailable(
            provider=provider, model=model, reason=f"provider unreachable ({type(exc).__name__})",
        )
    if isinstance(exc, RuntimeError) and "not set" in str(exc):
        # e.g. a required API key missing — a configuration outage, not a code bug.
        return ProviderUnavailable(
            provider=provider, model=model, reason="provider is not configured on this deployment",
        )
    return None


def classify_provider_error(provider: str, model: str, exc: Exception) -> Exception:
    """Return the exception to raise: a ProviderUnavailable if this is a provider fault, else the
    original exception unchanged (a genuine bug/bad-data case we must not disguise as an outage)."""
    return classify(exc, provider=provider, model=model) or exc
