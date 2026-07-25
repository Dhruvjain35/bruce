"""Gmail provider adapter (Phase G) — the same shape as calendar_adapter, so Gmail is an INHERITED hand: a
provider-neutral Protocol, a real Google adapter, and a FakeGmailAdapter that enforces the provider's real
rules (idempotent send, a SENT label, threads) so the send + fetch-back verification runs without network or
OAuth. No Gmail-specific routing/approval/loop lives here — only the provider I/O + verification.

Execute-once for a SEND: Google assigns the message id (we can't preset it like a calendar event), so
idempotency is keyed on a deterministic marker the runner supplies (run_id:stepN). The adapter records the
marker; a retry with the same marker returns the ALREADY-sent message instead of sending again — the same
"one verified side effect" guarantee calendar gets from a client-set id + provider 409.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol
from uuid import UUID

import httpx

log = logging.getLogger("bruce.gmail_adapter")   # content-free: status codes / ids, never message text

GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_IDEM_HEADER = "X-Bruce-Idempotency"      # a deterministic marker Gmail preserves -> dedup on read-back


class GmailError(Exception):
    """Base — never swallowed. A send failure is honest, never a fabricated 'sent'."""


class AlreadyExists(GmailError):
    """A send with a marker already present — resolve to the existing message, never a duplicate."""


class GmailAuthError(GmailError):
    """Reconnect needed (401)."""


class InsufficientScope(GmailError):
    """The grant is missing gmail.send / gmail.readonly (403) — re-consent."""


class GmailNotFound(GmailError):
    """A message/thread id that isn't there (404)."""


class RateLimited(GmailError):
    """Transient (429) — retryable."""


@dataclass(frozen=True)
class GmailMessageRef:
    message_id: str
    thread_id: str
    provider: str
    already_sent: bool = False        # True when idempotency resolved to an existing send (no duplicate)


def deterministic_marker(user_id: UUID, idempotency_key: str) -> str:
    """A stable per-send marker (base32hex of sha256(user|key)) written as a header so a retry finds the
    already-sent message. This is the send's execute-once guarantee."""
    h = hashlib.sha256(f"{user_id}|{idempotency_key}".encode()).digest()
    import base64
    return base64.b32hexencode(h).decode().rstrip("=").lower()[:32]


class GmailAdapter(Protocol):
    async def send(self, *, to: str, subject: str, body: str, thread_id: str | None,
                   marker: str) -> GmailMessageRef: ...
    async def get(self, message_id: str) -> dict | None: ...              # NORMALIZED, never raw google
    async def get_thread(self, thread_id: str) -> list[dict]: ...
    async def find_reply(self, thread_id: str, *, after_message_id: str,
                         own_message_ids: tuple[str, ...] = ()) -> dict | None: ...


def _normalize(raw: dict) -> dict:
    """The ONE place Gmail field names live. A normalized message: id/threadId/labels/to/from/subject/snippet."""
    headers = {h.get("name", "").lower(): h.get("value", "") for h in
               (raw.get("payload", {}).get("headers") or [])}
    return {
        "id": raw.get("id"), "threadId": raw.get("threadId"),
        "labelIds": list(raw.get("labelIds") or []),
        "to": headers.get("to"), "from": headers.get("from"), "subject": headers.get("subject"),
        "marker": headers.get(_IDEM_HEADER.lower()), "snippet": raw.get("snippet"),
    }


def matches_sent(read_back: dict | None, *, to: str, subject: str) -> tuple[bool, str]:
    """Verify a send by read-back: the message exists, carries the SENT label, and its recipient + subject
    match what we intended. verified=True ONLY when all hold — never on the strength of the send call."""
    if read_back is None:
        return False, "message not found on read-back"
    if "SENT" not in (read_back.get("labelIds") or []):
        return False, "message not in SENT"
    rb_to = (read_back.get("to") or "").lower()
    if to.lower() not in rb_to:
        return False, "recipient mismatch on read-back"
    if (subject or "").strip() and (subject or "").strip().lower() not in (read_back.get("subject") or "").lower():
        return False, "subject mismatch on read-back"
    return True, "read-back confirms SENT to the intended recipient"


async def send_and_verify(adapter, user_id: UUID, *, to: str, subject: str, body: str,
                          idempotency_key: str, thread_id: str | None = None):
    """Send ONCE (idempotent on the marker), then PROVE it by reading the sent message back and confirming it
    is in SENT to the intended recipient with the intended subject. Returns a ToolResult — verified ONLY on a
    matching read-back, never on the send call. Mirrors calendar's execute_and_verify."""
    from .runtime_contracts import ToolOutcome, ToolResult
    marker = deterministic_marker(user_id, idempotency_key)
    cap, prov, op = "gmail.send_message", "gmail", "send_message"
    try:
        ref = await adapter.send(to=to, subject=subject, body=body, thread_id=thread_id, marker=marker)
    except (GmailAuthError,) as exc:
        return ToolResult(ToolOutcome.unauthorized, cap, prov, op, reason=str(exc)[:120])
    except InsufficientScope as exc:
        return ToolResult(ToolOutcome.insufficient_scope, cap, prov, op, reason=str(exc)[:120])
    except RateLimited as exc:
        return ToolResult(ToolOutcome.rate_limited, cap, prov, op, reason=str(exc)[:120])
    except GmailError as exc:
        return ToolResult(ToolOutcome.provider_error, cap, prov, op, reason=str(exc)[:120])
    read_back = await adapter.get(ref.message_id)
    ok, reason = matches_sent(read_back, to=to, subject=subject)
    if not ok:
        return ToolResult(ToolOutcome.verification_failed, cap, prov, op, provider_entity_id=ref.message_id,
                          read_back=read_back, reason=reason)
    return ToolResult(ToolOutcome.ok, cap, prov, op, verified=True, provider_entity_id=ref.message_id,
                      read_back=read_back, reason=("already sent; " if ref.already_sent else "") + reason)


class FakeGmailAdapter:
    """In-memory Gmail that enforces the provider's rules (idempotent send by marker, SENT label, threads,
    replies) so the send + read-back verification runs without network/OAuth. Not a mock of Bruce code."""

    provider = "fake"

    def __init__(self, *, account: str | None = None) -> None:
        self.messages: dict[str, dict] = {}
        self._by_marker: dict[str, str] = {}
        self.send_calls = 0
        self.account = account

    async def send(self, *, to: str, subject: str, body: str, thread_id: str | None,
                   marker: str) -> GmailMessageRef:
        self.send_calls += 1
        if marker in self._by_marker:                       # execute-once: return the existing send
            mid = self._by_marker[marker]
            return GmailMessageRef(mid, self.messages[mid]["threadId"], self.provider, already_sent=True)
        mid = "m_" + hashlib.sha256(marker.encode()).hexdigest()[:16]
        tid = thread_id or ("t_" + mid)
        self.messages[mid] = {
            "id": mid, "threadId": tid, "labelIds": ["SENT"],
            "payload": {"headers": [{"name": "To", "value": to}, {"name": "Subject", "value": subject},
                                    {"name": "From", "value": self.account or ""},
                                    {"name": _IDEM_HEADER, "value": marker}]},
            "snippet": (body or "")[:80]}
        self._by_marker[marker] = mid
        return GmailMessageRef(mid, tid, self.provider)

    async def get(self, message_id: str) -> dict | None:
        raw = self.messages.get(message_id)
        return _normalize(raw) if raw is not None else None

    async def get_thread(self, thread_id: str) -> list[dict]:
        return [_normalize(m) for m in self.messages.values() if m.get("threadId") == thread_id]

    async def find_reply(self, thread_id: str, *, after_message_id: str,
                         own_message_ids: tuple[str, ...] = ()) -> dict | None:
        """Same rule as the real adapter: a reply is a thread message that arrived after ours and that we
        did not send, identified by id rather than by the SENT label (so a self-thread still resolves)."""
        msgs = await self.get_thread(thread_id)
        ours = {m for m in (after_message_id, *own_message_ids) if m}
        at = next((i for i, m in enumerate(msgs) if m.get("id") == after_message_id), None)
        later = msgs[at + 1:] if at is not None else msgs
        inbound = [m for m in later if m.get("id") not in ours]
        return inbound[-1] if inbound else None

    # test seam: simulate the student replying in the thread
    def inject_incoming(self, thread_id: str, *, from_addr: str, subject: str, body: str) -> str:
        mid = "in_" + hashlib.sha256(f"{thread_id}|{body}".encode()).hexdigest()[:12]
        self.messages[mid] = {
            "id": mid, "threadId": thread_id, "labelIds": ["INBOX"],
            "payload": {"headers": [{"name": "From", "value": from_addr}, {"name": "Subject", "value": subject}]},
            "snippet": body[:80]}
        return mid


# --- the REAL Google adapter ----------------------------------------------------------------------

def real_adapter(user_id: UUID, *, http_client: httpx.AsyncClient | None = None) -> "GoogleGmailAdapter":
    """Bind a live Gmail adapter to one user (token via the SAME Google integration calendar uses)."""
    return GoogleGmailAdapter(user_id, http_client=http_client)


def _build_raw(*, to: str, subject: str, body: str, sender: str | None, marker: str) -> str:
    """A base64url MIME message carrying the idempotency marker as a header. The marker is our own dedup key —
    it also lets a recovery scan re-find an already-sent message when the ledger write was lost to a crash."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    msg[_IDEM_HEADER] = marker
    msg.set_content(body or "")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GoogleGmailAdapter:
    """Live Gmail I/O over the Gmail REST API. Exactly-once send is enforced by the persisted marker ledger
    (gmail_store): a retry resolves to the already-sent message instead of sending twice, and a crash between
    send and ledger-write is recovered by a bounded scan of recent SENT messages for the marker header (Gmail
    cannot be queried by a custom header, so the scan is the only way to re-find it). Reads normalize the raw
    Google payload in exactly one place (_normalize) so no Gmail field name leaks past this module."""

    provider = "google"
    _RECOVERY_SCAN = 25          # how many recent SENT messages to check when re-finding a lost send

    def __init__(self, user_id: UUID, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.user_id = user_id
        self._client = http_client

    async def _token(self) -> str:
        from . import oauth_google
        return await oauth_google.access_token_for(self.user_id, http_client=self._client)

    async def _request(self, method: str, path: str, *, token: str, **kw) -> httpx.Response:
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            return await client.request(method, f"{GMAIL_API}{path}",
                                        headers={"Authorization": f"Bearer {token}"}, **kw)
        finally:
            if self._client is None:
                await client.aclose()

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        c = resp.status_code
        if c == 401:
            raise GmailAuthError("Gmail rejected the token (reconnect Google)")
        if c == 403:
            raise InsufficientScope("the Google account has not granted Gmail send/read")
        if c == 404:
            raise GmailNotFound("message or thread not found")
        if c == 429:
            raise RateLimited("Gmail rate limited")
        if c >= 400:
            raise GmailError(f"Gmail request failed (HTTP {c})")   # status only — bodies can echo content

    async def send(self, *, to: str, subject: str, body: str, thread_id: str | None,
                   marker: str) -> GmailMessageRef:
        from . import gmail_store
        prior = await gmail_store.get_by_marker(self.user_id, marker)      # completed retry -> no re-send
        if prior is not None:
            return GmailMessageRef(prior["message_id"], prior.get("thread_id") or "", self.provider, already_sent=True)
        token = await self._token()
        recovered = await self._recover_by_marker(marker, token=token)     # crash-window recovery -> no dup
        if recovered is not None:
            saved = await gmail_store.record_sent(self.user_id, marker=marker, message_id=recovered["id"],
                                                  thread_id=recovered.get("threadId"), to_addr=to, subject=subject)
            return GmailMessageRef(saved["message_id"], saved.get("thread_id") or "", self.provider, already_sent=True)

        sender = None
        try:
            from . import oauth_google
            integ = await oauth_google.get_integration(self.user_id)
            sender = integ.provider_account_id if integ is not None else None
        except Exception:
            sender = None
        payload = {"raw": _build_raw(to=to, subject=subject, body=body, sender=sender, marker=marker)}
        if thread_id:
            payload["threadId"] = thread_id
        resp = await self._request("POST", "/users/me/messages/send", token=token, json=payload)
        self._raise_for(resp)
        sent = resp.json()
        saved = await gmail_store.record_sent(self.user_id, marker=marker, message_id=sent["id"],
                                              thread_id=sent.get("threadId"), to_addr=to, subject=subject)
        # if a concurrent send won the ledger race, `saved` points at ITS message — return that (already_sent)
        already = saved["message_id"] != sent["id"]
        return GmailMessageRef(saved["message_id"], saved.get("thread_id") or sent.get("threadId") or "",
                               self.provider, already_sent=already)

    async def _recover_by_marker(self, marker: str, *, token: str) -> dict | None:
        """Bounded scan of recent SENT messages for our marker header — the only way to re-find a send whose
        ledger write was lost (Gmail can't be searched by a custom header). Best-effort: any error -> None."""
        try:
            lst = await self._request("GET", "/users/me/messages", token=token,
                                      params={"labelIds": "SENT", "maxResults": self._RECOVERY_SCAN})
            if lst.status_code != 200:
                return None
            for m in (lst.json().get("messages") or [])[:self._RECOVERY_SCAN]:
                got = await self._request("GET", f"/users/me/messages/{m['id']}", token=token,
                                          params={"format": "metadata", "metadataHeaders": _IDEM_HEADER})
                if got.status_code != 200:
                    continue
                if _normalize(got.json()).get("marker") == marker:
                    return got.json()
        except Exception:
            log.info("gmail recovery scan failed")
        return None

    async def get(self, message_id: str) -> dict | None:
        token = await self._token()
        resp = await self._request("GET", f"/users/me/messages/{message_id}", token=token,
                                   params=[("format", "metadata"), ("metadataHeaders", "To"),
                                           ("metadataHeaders", "Subject"), ("metadataHeaders", "From"),
                                           ("metadataHeaders", _IDEM_HEADER)])
        if resp.status_code == 404:
            return None
        self._raise_for(resp)
        return _normalize(resp.json())

    async def get_thread(self, thread_id: str) -> list[dict]:
        token = await self._token()
        resp = await self._request("GET", f"/users/me/threads/{thread_id}", token=token,
                                   params=[("format", "metadata"), ("metadataHeaders", "To"),
                                           ("metadataHeaders", "Subject"), ("metadataHeaders", "From")])
        if resp.status_code == 404:
            return []
        self._raise_for(resp)
        return [_normalize(m) for m in (resp.json().get("messages") or [])]

    async def find_reply(self, thread_id: str, *, after_message_id: str,
                         own_message_ids: tuple[str, ...] = ()) -> dict | None:
        """A reply = a message that arrived in the thread AFTER ours and that we did not send.

        "Ours" is identified by MESSAGE ID and position, not by the SENT label. A label-only rule looks
        right and silently fails the most ordinary case there is: when the student has Bruce email their
        OWN address, their reply is also sent by the connected account, so it carries SENT too and would
        be discarded forever — the mission would poll to exhaustion and finish "no reply" with nothing
        wrong in the logs. Position + identity works for both a self-thread and a normal one.
        """
        msgs = await self.get_thread(thread_id)
        ours = {m for m in (after_message_id, *own_message_ids) if m}
        at = next((i for i, m in enumerate(msgs) if m.get("id") == after_message_id), None)
        later = msgs[at + 1:] if at is not None else msgs
        inbound = [m for m in later if m.get("id") not in ours]
        return inbound[-1] if inbound else None
