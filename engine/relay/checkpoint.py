"""Durable local checkpoint so a relay/Mac restart never loses or replays a message.

A message GUID is marked processed ONLY after the backend has durably acknowledged it. On restart we
skip already-acknowledged GUIDs. Stored as a small JSON file (bounded ring of recent GUIDs) — content
never touches it, only opaque message ids.
"""

from __future__ import annotations

import json
import os
from collections import deque

from .durable import write_json_durable


class FileCheckpoint:
    def __init__(self, path: str, keep: int = 5000) -> None:
        self.path = path
        self.keep = keep
        self._order: deque[str] = deque(maxlen=keep)
        self._set: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                for g in json.load(f).get("processed", []):
                    self._order.append(g)
                    self._set.add(g)
        except (OSError, json.JSONDecodeError):
            pass

    def has(self, guid: str) -> bool:
        return guid in self._set

    def mark(self, guid: str) -> None:
        if guid in self._set:
            return
        if len(self._order) == self.keep:
            self._set.discard(self._order[0])  # evicted by the deque
        self._order.append(guid)
        self._set.add(guid)
        self._save()

    def _save(self) -> None:
        write_json_durable(self.path, {"processed": list(self._order)})  # fsync file+dir: crash-durable
