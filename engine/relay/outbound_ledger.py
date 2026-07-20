"""Durable per-message DELIVERY-PHASE ledger for outbound sends (A2 hardening).

A single boolean "already attempted" mark cannot tell a message that was *never handed to iMessage* from
one that *may have been* — so it can silently lose a message (mark -> crash before send -> reclaim sees
"marked" -> never delivered) or double-send. This ledger records the explicit delivery PHASE of each
outbound id so restart recovery is derived from durable state, never guessed.

Phases (only the durable ones are persisted; CLAIMED/PREPARED are transient in-memory states):

    SEND_INTENT_RECORDED        persisted BEFORE the imsg call. A crash here is AMBIGUOUS — we cannot
                                know whether imsg was invoked or accepted the bytes.
    HANDED_TO_IMSG              imsg returned a guid (accepted the bytes); persisted BEFORE the server
                                ack, so a lost ack still recovers as "sent" without a resend.
    SEND_FAILED_BEFORE_HANDOFF  imsg DEFINITELY declined before accepting bytes (an explicit rejection).
                                Safe to retry; never suppressed forever by the ledger.
    HANDOFF_OUTCOME_UNKNOWN     a transport crash across the external boundary — bytes may or may not
                                have gone. Never blindly resent, never reported sent; surfaced.
    SERVER_ACKNOWLEDGED         terminal: the server row transition is confirmed.
    BLOCKED_BEFORE_SEND         the gate blocked before any intent — no handed_to_imsg state; retryable.

Restart recovery reads phase(oid) and derives behavior (see relay.Relay._reclaim_by_phase). Terminology:
at-most-once IMSG INVOCATION; exactly-once SERVER-ROW state transition where enforceable; an AMBIGUOUS
delivery state whenever a crash straddles the external handoff — NOT end-to-end exactly-once (iMessage
gives no transactional handoff + acknowledgement).

Persisted as a small atomic JSON file (bounded ring; content-free — only opaque ids, phases, guids).
Reads the legacy A2 format ({"processed": [id,...]}) and migrates each entry to HANDED_TO_IMSG so an
existing relay never suddenly resends a message it had already attempted.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict

# Durable delivery phases.
SEND_INTENT_RECORDED = "send_intent_recorded"
HANDED_TO_IMSG = "handed_to_imsg"
SEND_FAILED_BEFORE_HANDOFF = "send_failed_before_handoff"
HANDOFF_OUTCOME_UNKNOWN = "handoff_outcome_unknown"
SERVER_ACKNOWLEDGED = "server_acknowledged"
BLOCKED_BEFORE_SEND = "blocked_before_send"
# Transient (never persisted) — represented for completeness / phase() callers.
CLAIMED = "claimed"
PREPARED = "prepared"

# Phases that mean "the bytes may already be with iMessage" -> NEVER blindly resend.
_HANDED_OR_UNKNOWN = (HANDED_TO_IMSG, SERVER_ACKNOWLEDGED, SEND_INTENT_RECORDED, HANDOFF_OUTCOME_UNKNOWN)
# Phases that are safe to (re)send from.
_RETRYABLE = (None, BLOCKED_BEFORE_SEND, SEND_FAILED_BEFORE_HANDOFF, CLAIMED, PREPARED)


class OutboundLedger:
    """Durable oid -> {phase, guid, at} store (atomic write, bounded, legacy-format migrating)."""

    def __init__(self, path: str, *, keep: int = 5000) -> None:
        self.path = path
        self.keep = keep
        self._e: "OrderedDict[str, dict]" = OrderedDict()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict) and "entries" in raw:                 # current format
            for oid, rec in (raw.get("entries") or {}).items():
                if isinstance(rec, dict) and rec.get("phase"):
                    self._e[oid] = rec
        elif isinstance(raw, dict) and "processed" in raw:             # legacy A2 boolean ledger -> migrate
            for oid in (raw.get("processed") or []):
                self._e[str(oid)] = {"phase": HANDED_TO_IMSG, "guid": None, "at": 0, "migrated": True}
            self._save()                                               # rewrite in the new format once

    def phase(self, oid: str) -> str | None:
        rec = self._e.get(oid)
        return rec.get("phase") if rec else None

    def guid(self, oid: str) -> str | None:
        rec = self._e.get(oid)
        return rec.get("guid") if rec else None

    def is_retryable(self, oid: str) -> bool:
        return self.phase(oid) in _RETRYABLE

    def maybe_handed_off(self, oid: str) -> bool:
        """True if the bytes MAY already be with iMessage (handed / acknowledged / intent / unknown)."""
        return self.phase(oid) in _HANDED_OR_UNKNOWN

    def record(self, oid: str, phase: str, *, guid: str | None = None) -> None:
        rec = self._e.get(oid) or {}
        rec = {"phase": phase, "guid": guid if guid is not None else rec.get("guid"), "at": time.time()}
        self._e[oid] = rec
        self._e.move_to_end(oid)
        while len(self._e) > self.keep:
            self._e.popitem(last=False)                                # bounded: evict oldest
        self._save()

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"version": 2, "entries": self._e}, f)
        os.replace(tmp, self.path)                                     # atomic -> restart-safe
