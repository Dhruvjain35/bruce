"""Provider availability — classify upstream model failures HONESTLY.

Why this module exists: Bruce's demonstrated intake path runs on Qwen Cloud. Qwen Cloud inference is
currently blocked at the account level (403 AccessDenied.Unpurchased under a
RISK.RISK_CONTROL_REJECTION hold), and a system whose promise is "proves the result" must say that
plainly rather than dressing it up.

Two rules, both deliberate:

  1. A provider outage surfaces as ``provider_unavailable`` with the real reason and the real
     provider/model — never as a generic 502, and never as an empty-but-successful extraction. An
     empty intake that looks like "Bruce read your flyer and found nothing" is a false completion.
  2. There is NO automatic fallback to another provider on the Qwen path. Silently answering with
     OpenAI while claiming a Qwen-powered workflow would make the whole demonstration a lie. If
     Qwen is down, the request fails and says so. Switching providers is an explicit, operator-made
     configuration change (BRUCE_INTAKE_PROVIDER), never an implicit runtime decision.

The reason strings here are shown to clients, so they must never contain the API key, the request
body, or any student content — provider + model + a short cause only.
"""

from __future__ import annotations

import dataclasses

import httpx
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

# Alibaba's code when an account/order is risk-suspended or the model was never activated.
ACCESS_DENIED_CODES = ("AccessDenied.Unpurchased", "AccessDenied")


@dataclasses.dataclass(frozen=True)
class ProviderUnavailable(Exception):
    """The configured model provider could not serve the request. Fail closed, never fall back."""

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


def classify(exc: Exception, *, provider: str, model: str) -> ProviderUnavailable | None:
    """Map an upstream exception to ProviderUnavailable, or None if it is not a provider problem.

    None means "this is our bug or bad data, not the provider" — the caller should not disguise it
    as an outage.
    """
    if isinstance(exc, ModelHTTPError):
        code = _body_code(exc.body)
        if exc.status_code == 403 or code in ACCESS_DENIED_CODES:
            return ProviderUnavailable(
                provider=provider,
                model=model,
                reason=(
                    f"access denied by provider ({code or 'AccessDenied'}) — the account is not "
                    "entitled to run inference on this model. No successful call is possible until "
                    "the provider account is enabled."
                ),
                status_code=403,
            )
        if exc.status_code in (401, 407):
            return ProviderUnavailable(
                provider=provider, model=model,
                reason="provider rejected the credentials", status_code=exc.status_code,
            )
        if exc.status_code == 429:
            return ProviderUnavailable(
                provider=provider, model=model,
                reason="provider rate limit or quota exhausted", status_code=429,
            )
        if exc.status_code >= 500:
            return ProviderUnavailable(
                provider=provider, model=model,
                reason=f"provider server error (HTTP {exc.status_code})", status_code=exc.status_code,
            )
        return ProviderUnavailable(
            provider=provider, model=model,
            reason=f"provider rejected the request (HTTP {exc.status_code})",
            status_code=exc.status_code,
        )
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return ProviderUnavailable(
            provider=provider, model=model, reason=f"provider unreachable ({type(exc).__name__})",
        )
    if isinstance(exc, ModelAPIError):
        return ProviderUnavailable(
            provider=provider, model=model, reason=f"provider API error ({type(exc).__name__})",
        )
    if isinstance(exc, RuntimeError) and "not set" in str(exc):
        # e.g. DASHSCOPE_API_KEY missing — a configuration outage, not a code bug.
        return ProviderUnavailable(
            provider=provider, model=model, reason="provider is not configured on this deployment",
        )
    return None
