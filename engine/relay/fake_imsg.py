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

from .imsg import ImsgEvent, ImsgSendRejected, parse_event


class InProcessImsg:
    """Deterministic in-memory Imsg. Feed it raw event dicts; collect sent messages for assertions.

    Delivery-outcome controls (for the phase tests):
      send_rejects      -- first N sends raise ImsgSendRejected  (DEFINITE pre-handoff decline -> retryable)
      exit_before_handoff -- raise ImsgSendRejected              (child gone before dispatch -> retryable)
      crash_unknown     -- raise a generic transport error       (AMBIGUOUS handoff -> unknown)
      crash_after_handoff_hook -- called AFTER the bytes are recorded as sent but the guid is not returned
                                  (simulates a crash right after imsg accepted -> ambiguous to the relay)
      send_raises / send_fails -- generic errors (ambiguous), kept for existing tests
    """

    def __init__(self, events: list[dict] | None = None, *, send_fails: int = 0,
                 send_raises: bool = False, send_rejects: int = 0, exit_before_handoff: bool = False,
                 crash_unknown: bool = False) -> None:
        self._events = list(events or [])
        self.sent: list[dict] = []
        self.calls = 0                     # TOTAL send_text invocations (incl. failures) — for "sent exactly once"
        self.closed = False                # set by aclose() — for the reap-on-stop test
        self._send_fails = send_fails      # first N send_text calls raise (transient) then succeed
        self._send_raises = send_raises    # every send_text raises (permanent transport failure)
        self._send_rejects = send_rejects  # first N raise ImsgSendRejected (definite pre-handoff)
        self._exit_before_handoff = exit_before_handoff
        self._crash_unknown = crash_unknown
        self.crash_after_handoff_hook = None   # optional callable(): raise AFTER recording the handoff
        self._guid = 0

    def feed(self, event: dict) -> None:
        self._events.append(event)

    async def watch(self) -> AsyncIterator[ImsgEvent]:
        # Drain whatever is queued, then end the stream (the Relay treats this as a dropped watch and
        # reconnects — tests call process_inbound directly, so ending here keeps them deterministic).
        while self._events:
            yield parse_event(self._events.pop(0))

    async def send_text(self, to: str, text: str) -> str | None:
        self.calls += 1                    # count EVERY invocation, so a double-send is detectable
        if self._exit_before_handoff:
            raise ImsgSendRejected("child exited before handoff")
        if self._send_rejects > 0:
            self._send_rejects -= 1
            raise ImsgSendRejected("imsg declined (invalid recipient)")
        if self._crash_unknown:
            raise RuntimeError("transport crash (ambiguous)")
        if self._send_raises:
            raise RuntimeError("imsg send failed")
        if self._send_fails > 0:
            self._send_fails -= 1
            raise RuntimeError("imsg send transient")
        self._guid += 1
        guid = f"fake-out-{self._guid}"
        self.sent.append({"to": to, "text": text, "guid": guid})   # bytes accepted (recorded here)
        if self.crash_after_handoff_hook is not None:
            await self.crash_after_handoff_hook()                    # crash AFTER acceptance -> ambiguous
        return guid

    async def aclose(self) -> None:
        self.closed = True

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
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {}}), flush=True)
    if not subscribed:
        return


if __name__ == "__main__":
    main()
