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


class Backend(Protocol):
    async def post_inbound(self, event: dict) -> dict: ...
    async def upload(self, data: bytes, media_type: str, filename: str | None) -> str: ...
    async def claim(self) -> dict | None: ...
    async def ack(self, outbound_id: str, status: str, provider_message_id: str | None, error: str | None) -> None: ...
    async def heartbeat(self) -> dict: ...


class HttpBackend:
    def __init__(self, base_url: str, secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret

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
            raise AuthError("relay credential rejected")
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
        return None if r.status_code == 204 else r.json()

    async def ack(self, outbound_id: str, status: str, provider_message_id: str | None, error: str | None) -> None:
        await self._post(f"/v1/relay/outbound/{outbound_id}/ack",
                         {"status": status, "provider_message_id": provider_message_id, "error": error})

    async def heartbeat(self) -> dict:
        return (await self._post("/v1/relay/heartbeat", {})).json()
