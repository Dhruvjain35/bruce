"""Restart-safe store of inbound messages whose attachment was still downloading.

imsg 0.13.1 has NO message.get, so a still-downloading attachment is resolved by a SUBSEQUENT watch
event (imsg re-emits the message when the file lands) or by re-stat on a sweep. This durable, bounded
JSON store keys records by the stable message GUID and keeps exactly what's needed to (a) re-post once
the attachment resolves, or (b) time the message out honestly. Restart-safe (atomic write); bounded
(oldest evicted); holds no more than the event we already received.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict

from .durable import write_json_durable


class PendingStore:
    def __init__(self, path: str, *, max_records: int = 500) -> None:
        self.path = path
        self.max_records = max_records
        self._records: "OrderedDict[str, dict]" = OrderedDict()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                for guid, rec in (json.load(f).get("pending") or {}).items():
                    self._records[guid] = rec
        except (OSError, json.JSONDecodeError):
            pass

    def has(self, guid: str) -> bool:
        return guid in self._records

    def get(self, guid: str) -> dict | None:
        return self._records.get(guid)

    def items(self) -> list[tuple[str, dict]]:
        return list(self._records.items())

    def upsert(self, guid: str, event: dict, now: float) -> dict:
        """Insert a new pending record (first_seen=now, events=1) or bump the seen count. Returns it."""
        rec = self._records.get(guid)
        if rec is None:
            while len(self._records) >= self.max_records:
                self._records.popitem(last=False)              # evict oldest — bounded
            rec = {"event": event, "first_seen": now, "events": 1}
            self._records[guid] = rec
        else:
            rec["events"] = int(rec.get("events", 1)) + 1
        self._save()
        return rec

    def remove(self, guid: str) -> None:
        if guid in self._records:
            del self._records[guid]
            self._save()

    def _save(self) -> None:
        write_json_durable(self.path, {"pending": self._records})   # fsync file+dir: crash-durable
