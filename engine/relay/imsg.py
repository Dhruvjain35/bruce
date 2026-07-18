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


class Imsg(Protocol):
    def watch(self) -> AsyncIterator[ImsgEvent]: ...
    async def send_text(self, to: str, text: str) -> str | None: ...        # -> provider message guid
    async def send_file(self, to: str, path: str) -> str | None: ...


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
        self._proc = await asyncio.create_subprocess_exec(
            self.binary, "rpc", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        await self._rpc("watch.subscribe", {})
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

    async def _send(self, params: dict, *, timeout: float = 60.0) -> str | None:
        """Send via a DEDICATED, short-lived `imsg rpc` process.

        Outbound MUST NOT reuse the watch subprocess: watch() is continuously awaiting
        ``stdout.readline()`` on that process, and a second concurrent readline on the same stream
        raises ``RuntimeError`` in asyncio. Previously send shared it, so every send that overlapped
        the watch loop raised AFTER the request had already reached imsg — the message was delivered
        but recorded as failed, and the durable queue resent it every retry-backoff. A separate
        process per send removes that shared reader entirely."""
        proc = await asyncio.create_subprocess_exec(
            self.binary, "rpc",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            assert proc.stdin and proc.stdout
            req = {"jsonrpc": "2.0", "id": 1, "method": "send", "params": params}
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
        if not line:
            return None
        resp = json.loads(line.decode())
        if "error" in resp:
            raise RuntimeError(f"imsg send error: {resp['error']}")   # a real send failure -> retry
        return (resp.get("result") or {}).get("guid")

    async def send_text(self, to: str, text: str) -> str | None:
        return await self._send({"to": to, "text": text})

    async def send_file(self, to: str, path: str) -> str | None:
        return await self._send({"to": to, "file": path})
