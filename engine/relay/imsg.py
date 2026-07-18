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
            # Watch pushes message objects (not RPC responses) — those carry a guid.
            if isinstance(obj, dict) and (obj.get("guid") or obj.get("id")) and "jsonrpc" not in obj:
                yield parse_event(obj)

    async def send_text(self, to: str, text: str) -> str | None:
        r = await self._rpc("send", {"to": to, "text": text})
        return (r.get("result") or {}).get("guid")

    async def send_file(self, to: str, path: str) -> str | None:
        r = await self._rpc("send", {"to": to, "file": path})
        return (r.get("result") or {}).get("guid")
