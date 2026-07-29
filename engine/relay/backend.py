"""Backend client — the relay's ONLY outbound connection (initiates TLS to Bruce; nothing calls in).

Carries the rotating device credential (Bearer, from the Keychain) + a per-request timestamp, nonce,
and request id. Certificate verification is mandatory (httpx default). No cloud/DB/OpenAI keys — only
the one device secret. A Protocol lets tests inject a fake backend.
"""

from __future__ import annotations

import base64
import datetime
import uuid
from typing import Protocol


class BackendError(Exception):
    """A transient backend failure (network / 5xx). The relay retries; it does NOT drop the message."""


class AuthError(Exception):
    """The device credential was rejected (401) — revoked/expired. The relay stops and alerts."""


# Reasons a 401 describes THIS REQUEST rather than the credential. The server now states this directly
# via `retryable`; these strings are the fallback for a relay running against an older API, so a Mac that
# has not been updated still recovers from a clock blip instead of parking.
_TRANSIENT_401_REASONS = ("stale request (replay)", "bad timestamp")

# How many CONSECUTIVE transient 401s to ride out before giving up and parking. At the 2s poll interval
# this is ~40s — far longer than the seconds-long blip that caused the outage, far shorter than the 11.9
# hours it actually cost, and short enough that a permanently wrong clock surfaces as a parked relay
# rather than an infinite quiet retry.
MAX_CONSECUTIVE_TRANSIENT_401 = 20


def _retryable_401(r) -> bool:
    """Is this 401 about the request (retry) or the credential (park)?

    Defaults to FALSE — an unparseable or unrecognised 401 keeps the old, conservative behaviour of
    parking, so a genuinely revoked device can never be talked into polling forever by a malformed body.
    """
    try:
        detail = r.json().get("detail")
    except Exception:
        return False
    if not isinstance(detail, dict):
        return False
    flag = detail.get("retryable")
    if isinstance(flag, bool):
        return flag
    return str(detail.get("reason", "")) in _TRANSIENT_401_REASONS


class Backend(Protocol):
    async def post_inbound(self, event: dict) -> dict: ...
    async def upload(self, data: bytes, media_type: str, filename: str | None) -> str: ...
    async def claim(self) -> dict | None: ...
    async def ack(self, outbound_id: str, status: str, provider_message_id: str | None, error: str | None) -> None: ...
    async def heartbeat(self) -> dict: ...
    async def directive(self) -> str: ...


class HttpBackend:
    def __init__(self, base_url: str, secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.last_retry_after: float | None = None   # Retry-After from the most recent paused 204 (backoff hint)
        self._transient_401s = 0                     # consecutive retryable 401s; any clean answer resets

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret}",
            "X-Bruce-Timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "X-Bruce-Nonce": uuid.uuid4().hex,
            "X-Bruce-Request-Id": uuid.uuid4().hex,
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, json_body: dict) -> "object":
        import httpx  # imported lazily so the module loads without httpx present
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:  # explicit timeout
                r = await c.post(self.base_url + path, headers=self._headers(), json=json_body)
        except Exception as exc:
            raise BackendError(str(exc)) from exc
        if r.status_code == 401:
            # A 401 means one of two very different things. "credential revoked/invalid" is permanent and
            # must park the relay. "stale request (replay)" / "bad timestamp" is a property of THIS request
            # — a clock blip or a network transition — and the credential is still good. Parking on the
            # second cost 11.9 hours of silent downtime on 2026-07-28, so it is now retried like any other
            # transient backend failure. BackendError is the correct channel: every caller already treats
            # it as "do not send, back off, try again", so this widens no permission anywhere.
            if _retryable_401(r) and self._transient_401s < MAX_CONSECUTIVE_TRANSIENT_401:
                self._transient_401s += 1
                raise BackendError("relay auth rejected this request (retryable)")
            # RETRYING FOREVER IS ALSO A FAILURE MODE. The server checks the timestamp BEFORE the
            # credential, so a REVOKED device whose clock is skewed is told "stale request (replay)" on
            # every single call — retrying on that alone would mean a revoked relay polls indefinitely and
            # REVOKED_CREDENTIAL_EXIT becomes unreachable. A blip clears in seconds; anything surviving
            # this many consecutive attempts is a real misconfiguration and should park loudly.
            raise AuthError("relay credential rejected")
        self._transient_401s = 0   # any non-401 answer proves both the credential and the clock are fine
        if r.status_code >= 500 or r.status_code == 429:
            raise BackendError(f"backend {r.status_code}")
        return r

    async def post_inbound(self, event: dict) -> dict:
        return (await self._post("/v1/relay/inbound", event)).json()

    async def upload(self, data: bytes, media_type: str, filename: str | None) -> str:
        r = await self._post("/v1/relay/upload", {
            "content_base64": base64.b64encode(data).decode(), "media_type": media_type, "filename": filename})
        if r.status_code == 415:
            raise BackendError("upload rejected")  # not retryable, but relay treats as skip upstream
        return r.json()["upload_ref"]

    async def claim(self) -> dict | None:
        r = await self._post("/v1/relay/outbound/claim", {})
        if r.status_code == 204:
            ra = r.headers.get("Retry-After")   # the server sets this on a PAUSED 204 (widen backoff)
            self.last_retry_after = float(ra) if ra and ra.isdigit() else None
            return None
        self.last_retry_after = None
        return r.json()

    async def ack(self, outbound_id: str, status: str, provider_message_id: str | None, error: str | None) -> None:
        await self._post(f"/v1/relay/outbound/{outbound_id}/ack",
                         {"status": status, "provider_message_id": provider_message_id, "error": error})

    async def heartbeat(self) -> dict:
        return (await self._post("/v1/relay/heartbeat", {})).json()

    async def directive(self) -> str:
        """Authenticated read of THIS device's current directive (run|pause_outbound|stop), via the
        content-free heartbeat endpoint. Device identity comes only from the Bearer credential — never a
        caller-supplied id. FAIL-CLOSED contract: a 401 (revoked/expired) raises AuthError; a network /
        TLS / timeout error raises BackendError (both handled upstream as "do not send"); a malformed body
        (bad JSON, missing/non-string 'directive') raises BackendError. The raw string is returned as-is —
        an UNKNOWN value is classified as blocked by the caller, never treated as permission to send. The
        full API response is never logged."""
        r = await self._post("/v1/relay/heartbeat", {})   # 401 -> AuthError, 5xx/429/network -> BackendError
        try:
            data = r.json()
        except Exception as exc:
            raise BackendError("malformed directive response") from exc
        directive = data.get("directive") if isinstance(data, dict) else None
        if not isinstance(directive, str) or not directive:
            raise BackendError("malformed directive response")
        return directive
