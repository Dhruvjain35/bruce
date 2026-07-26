"""Crash-durable atomic JSON write.

`os.replace` is atomic against a *clean* crash, but on hard power-loss / kernel panic the temp file's
bytes and the rename can still be sitting in the page cache — so a durable delivery phase (or checkpoint,
or pending record) can silently revert on reboot. For the relay that means re-sending an iMessage to a
real person. So we fsync the file contents AND the containing directory (which makes the rename durable)
before returning. Used by every restart-safe relay store (checkpoint, outbound ledger, pending)."""

from __future__ import annotations

import json
import os
from typing import Any


def write_json_durable(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())          # the bytes are on disk, not just in the page cache
    os.replace(tmp, path)             # atomic rename
    dir_fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(dir_fd)              # the rename itself is durable (survives power loss)
    finally:
        os.close(dir_fd)
