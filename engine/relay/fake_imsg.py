"""Fake `imsg` — a JSON-RPC 2.0 process that mimics the audited imsg watch/send contract.

Two forms:
  * `python -m relay.fake_imsg` — a REAL subprocess the relay's SubprocessImsg can drive end-to-end
    (used by the dedicated-Mac dry-run before real Messages is wired). It reads a scripted event list
    from BRUCE_FAKE_IMSG_EVENTS (a JSON file), replies to watch.subscribe, then streams those events
    as watch pushes, and answers `send` with a synthetic guid. Sent messages are appended to
    BRUCE_FAKE_IMSG_SENT (if set) so a test can assert outbound delivery.
  * `InProcessImsg` — an in-memory Imsg for the pytest integration suite (no subprocess, deterministic).

This is a test double; it holds NO Bruce logic and never talks to the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import AsyncIterator

from .imsg import ImsgEvent, parse_event


class InProcessImsg:
    """Deterministic in-memory Imsg. Feed it raw event dicts; collect sent messages for assertions."""

    def __init__(self, events: list[dict] | None = None, *, send_fails: int = 0,
                 send_raises: bool = False) -> None:
        self._events = list(events or [])
        self.sent: list[dict] = []
        self._send_fails = send_fails      # first N send_text calls raise (transient) then succeed
        self._send_raises = send_raises    # every send_text raises (permanent transport failure)
        self._guid = 0
        self._resolvable: dict[str, dict] = {}   # guid -> what get_message() returns (delayed resolve)
        self.get_message_calls = 0

    def feed(self, event: dict) -> None:
        self._events.append(event)

    def resolve_to(self, guid: str, raw: dict) -> None:
        """Register what get_message(guid) resolves to — e.g. the same event with missing removed
        (delayed download completes), or one that STAYS missing (metadata-but-missing-bytes)."""
        self._resolvable[guid] = raw

    async def get_message(self, guid: str) -> ImsgEvent | None:
        self.get_message_calls += 1
        raw = self._resolvable.get(guid)
        return parse_event(raw) if raw else None

    async def watch(self) -> AsyncIterator[ImsgEvent]:
        # Drain whatever is queued, then end the stream (the Relay treats this as a dropped watch and
        # reconnects — tests call process_inbound directly, so ending here keeps them deterministic).
        while self._events:
            yield parse_event(self._events.pop(0))

    async def send_text(self, to: str, text: str) -> str | None:
        if self._send_raises:
            raise RuntimeError("imsg send failed")
        if self._send_fails > 0:
            self._send_fails -= 1
            raise RuntimeError("imsg send transient")
        self._guid += 1
        guid = f"fake-out-{self._guid}"
        self.sent.append({"to": to, "text": text, "guid": guid})
        return guid

    async def send_file(self, to: str, path: str) -> str | None:
        self._guid += 1
        guid = f"fake-out-{self._guid}"
        self.sent.append({"to": to, "file": path, "guid": guid})
        return guid


# --------------------------------------------------------------------------------------------------
# Standalone subprocess form: `python -m relay.fake_imsg`
# --------------------------------------------------------------------------------------------------

def _load_events() -> list[dict]:
    path = os.environ.get("BRUCE_FAKE_IMSG_EVENTS")
    if not path or not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def _record_sent(entry: dict) -> None:
    path = os.environ.get("BRUCE_FAKE_IMSG_SENT")
    if not path:
        return
    existing: list[dict] = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = []
    existing.append(entry)
    with open(path, "w") as f:
        json.dump(existing, f)


def main() -> None:
    """Serve one JSON-RPC session on stdin/stdout, matching SubprocessImsg's expectations."""
    events = _load_events()
    guid_seq = 0
    subscribed = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
        if method == "watch.subscribe":
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"ok": True}}), flush=True)
            subscribed = True
            for ev in events:  # stream scripted watch pushes (bare message objects, no "jsonrpc")
                print(json.dumps(ev), flush=True)
        elif method == "send":
            guid_seq += 1
            guid = f"fake-out-{guid_seq}"
            _record_sent({"to": params.get("to"), "text": params.get("text"), "file": params.get("file"), "guid": guid})
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"guid": guid}}), flush=True)
        elif method == "message.get":
            # Dry-run: resolve a message by guid from BRUCE_FAKE_IMSG_RESOLVE (a {guid: event} JSON file).
            resolved = {}
            rp = os.environ.get("BRUCE_FAKE_IMSG_RESOLVE")
            if rp and os.path.exists(rp):
                try:
                    with open(rp) as f:
                        resolved = json.load(f)
                except (OSError, json.JSONDecodeError):
                    resolved = {}
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": resolved.get(params.get("guid"))}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}), flush=True)
    if not subscribed:
        return


if __name__ == "__main__":
    main()
