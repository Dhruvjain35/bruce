"""imsg client — thin wrapper over the audited `openclaw/imsg` JSON-RPC contract.

TRANSPORT ONLY. No Bruce logic, no model calls, no mission state. Uses the SIP-safe surface only:
watch (inbound), send / poll.send (outbound), message.send_status. Never `read`/typing/mutation
(those need injection + SIP disabled — forbidden).

The subprocess speaks JSON-RPC 2.0 over stdin/stdout, one object per line. A Protocol lets tests
inject a fake imsg with no subprocess.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import AsyncIterator, Protocol


class ImsgSendRejected(Exception):
    """imsg DEFINITELY declined the send BEFORE accepting any bytes (an explicit error response, or the
    child was gone before the request was dispatched). No bytes left the machine -> safely retryable.
    Any OTHER exception from a send is AMBIGUOUS (a transport crash may have straddled the handoff)."""


@dataclasses.dataclass
class ImsgEvent:
    """A normalized inbound event (subset of imsg's watch schema we consume)."""
    guid: str                      # stable message id
    chat_guid: str | None          # conversation (group reply target)
    sender: str | None             # sender handle
    is_from_me: bool               # Bruce's own echo
    is_group: bool
    text: str | None
    created_at: str | None
    attachments: list[dict]        # each: {mime_type, original_path, missing, byte_count, transfer_name}
    reply_to_guid: str | None = None
    # --- Bite 2 A: message-relationship metadata (null-safe; populated per imsg's real event schema).
    # These are the RAW iMessage signals; the provider-neutral mapping happens at the POST boundary.
    thread_originator_guid: str | None = None      # inline-reply originator (DISTINCT from chat_guid)
    associated_message_guid: str | None = None     # a reaction/tapback's TARGET message
    associated_message_type: int | None = None     # Apple tapback code (2000-3007); 0/None = not a tapback
    service: str | None = None                     # "iMessage" | "SMS"
    is_edited: bool = False
    is_unsent: bool = False


# Apple associated_message_type -> provider-neutral tapback. 2000-2007 = added, 3000-3007 = removed.
# ONLY verified Apple codes map; any other value in the reaction range is "unknown" (never guessed).
_TAPBACK = {2000: "love", 2001: "like", 2002: "dislike", 2003: "laugh", 2004: "emphasis",
            2005: "question", 2006: "sticker", 2007: "sticker"}


def reaction_of(assoc_type: int | None) -> tuple[str | None, bool]:
    """Map an Apple associated_message_type to (provider-neutral reaction type, removed).
    Returns (None, False) when it isn't a tapback at all (0/None or outside 2000-3999); returns
    ("unknown", removed) for a reaction-range code we don't recognize — we never guess a meaning."""
    if not assoc_type or not (2000 <= assoc_type < 4000):
        return (None, False)
    removed = assoc_type >= 3000
    base = assoc_type - 1000 if removed else assoc_type
    return (_TAPBACK.get(base, "unknown"), removed)


class Imsg(Protocol):
    def watch(self) -> AsyncIterator[ImsgEvent]: ...
    async def send_text(self, to: str, text: str) -> str | None: ...        # -> provider message guid
    async def send_file(self, to: str, path: str) -> str | None: ...
    # NOTE: no get_message — imsg 0.13.1 has no message.get. A still-downloading attachment is
    # resolved by a SUBSEQUENT watch event (imsg re-emits the message when the file lands).


def parse_event(raw: dict) -> ImsgEvent:
    """Map an imsg watch message object -> ImsgEvent using the audited field names."""
    return ImsgEvent(
        guid=str(raw.get("guid") or raw.get("id") or ""),
        chat_guid=raw.get("chat_guid"),
        sender=raw.get("sender"),
        is_from_me=bool(raw.get("is_from_me")),
        is_group=bool(raw.get("is_group")),
        text=raw.get("text"),
        created_at=raw.get("created_at"),
        attachments=list(raw.get("attachments") or []),
        reply_to_guid=raw.get("reply_to_guid"),
        thread_originator_guid=raw.get("thread_originator_guid"),
        associated_message_guid=raw.get("associated_message_guid"),
        associated_message_type=(int(raw["associated_message_type"])
                                 if str(raw.get("associated_message_type") or "").strip().lstrip("-").isdigit()
                                 else None),
        service=raw.get("service"),
        is_edited=bool(raw.get("is_edited") or raw.get("date_edited")),
        is_unsent=bool(raw.get("is_unsent") or raw.get("date_retracted")),
    )


def stream_event(obj: object) -> ImsgEvent | None:
    """Turn ONE line of imsg's rpc watch stream into an ImsgEvent, or None if it isn't a message.

    imsg (0.13.x) frames all rpc I/O as JSON-RPC 2.0, so watch pushes arrive as NOTIFICATIONS —
    ``{"jsonrpc":"2.0","method":"...","params":{...message...}}`` (a notification has a ``method`` and
    no ``id``). We accept that, and also a bare message object (some builds emit the message directly).
    Request RESPONSES to our own calls (``watch.subscribe``/``send`` → ``{"result"|"error","id":...}``)
    are NOT events and are skipped. Being tolerant of both shapes means the live watch can't silently
    yield nothing if imsg wraps events in JSON-RPC framing."""
    if not isinstance(obj, dict):
        return None
    if "method" in obj and "id" not in obj:            # JSON-RPC notification -> event in params
        params = obj.get("params")
        if isinstance(params, dict):
            msg = params if params.get("guid") else params.get("message")
            if isinstance(msg, dict) and msg.get("guid"):
                return parse_event(msg)
        return None
    if "result" in obj or "error" in obj:              # a response/error to one of our requests
        return None
    if obj.get("guid"):                                # bare message object (no JSON-RPC framing)
        return parse_event(obj)
    return None


class SubprocessImsg:
    """Real client: drives one `imsg rpc` subprocess. Reconnect is the caller's job (see Relay)."""

    def __init__(self, binary: str = "imsg") -> None:
        self.binary = binary
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0

    async def _rpc(self, method: str, params: dict | None = None) -> dict:
        assert self._proc and self._proc.stdin and self._proc.stdout
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self._proc.stdin.write((json.dumps(req) + "\n").encode())
        await self._proc.stdin.drain()
        line = await self._proc.stdout.readline()
        return json.loads(line.decode() or "{}")

    async def watch(self) -> AsyncIterator[ImsgEvent]:
        # stderr=DEVNULL: the long-lived watch child must NOT inherit stderr — the supervisor/LaunchAgent
        # routes the tree's stderr to a plist log file, and imsg can emit message content there (would
        # break content-free logging). The one-shot RPC spawn below already sets this.
        self._proc = await asyncio.create_subprocess_exec(
            self.binary, "rpc", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        # attachments:True == imsg's --attachments: watch pushes then carry the attachments array
        # (mime_type/original_path/missing/…). Without it the array never populates and images are lost.
        await self._rpc("watch.subscribe", {"attachments": True})
        assert self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return  # subprocess exited -> the Relay reconnects
            try:
                obj = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            ev = stream_event(obj)   # tolerant of JSON-RPC-notification OR bare-object framing
            if ev is not None:
                yield ev

    async def _oneshot_rpc(self, method: str, params: dict, *, timeout: float = 60.0) -> dict:
        """Run ONE rpc call on a DEDICATED, short-lived `imsg rpc` process, returning the parsed
        response object.

        Anything other than the watch stream (send, message.get, …) MUST NOT reuse the watch
        subprocess: watch() is continuously awaiting ``stdout.readline()`` on it, and a second
        concurrent readline on the same asyncio stream raises ``RuntimeError``. A fresh process per
        call removes the shared reader entirely (this is the fix that stopped the outbound resend
        loop; get_message reuses it for exactly the same reason)."""
        proc = await asyncio.create_subprocess_exec(
            self.binary, "rpc",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            assert proc.stdin and proc.stdout
            req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            proc.stdin.write((json.dumps(req) + "\n").encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()          # one-shot: terminate as soon as we have the response
                except ProcessLookupError:
                    pass
            await proc.wait()
        return json.loads(line.decode()) if line else {}

    async def _send(self, params: dict, *, timeout: float = 60.0) -> str | None:
        resp = await self._oneshot_rpc("send", params, timeout=timeout)
        if "error" in resp:
            # imsg processed the request and DECLINED it -> definitely no bytes handed off -> retryable.
            raise ImsgSendRejected(f"imsg declined: {resp['error']}")
        guid = (resp.get("result") or {}).get("guid")
        if not guid:
            # No confirmation guid (e.g. the child exited before responding) -> AMBIGUOUS handoff, never
            # a silent success. The relay classifies this as handoff_outcome_unknown, not a resend.
            raise RuntimeError("imsg send: no confirmation guid (ambiguous handoff)")
        return guid

    async def send_text(self, to: str, text: str) -> str | None:
        return await self._send({"to": to, "text": text})

    async def send_file(self, to: str, path: str) -> str | None:
        return await self._send({"to": to, "file": path})

    async def aclose(self) -> None:
        """Reap the watch subprocess (called on relay stop / a `stop` directive). Send uses one-shot
        processes that are already reaped per call; this terminates the long-lived watch child so a
        parked relay leaves no orphaned imsg process."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await proc.wait()
        except Exception:
            pass
