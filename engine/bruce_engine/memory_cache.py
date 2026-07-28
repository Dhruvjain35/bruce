"""The retrieval cache, and the one rule that makes it safe: invalidation is a GENERATION, not a list.

A cache over memory is easy to write and easy to get catastrophically wrong. The failure is not a slow
turn — it is a student saying "forget that" and Bruce repeating the forgotten fact thirty seconds later
because the context it was built into is still warm. Forgetting that does not stick is worse than not
offering to forget at all.

WHY A GENERATION COUNTER RATHER THAN TARGETED EVICTION. The obvious design invalidates the entries that
contain the changed memory. It requires knowing which cached contexts contain which memory ids, and it
fails silently in exactly one case: a context that OMITTED a fact because a higher-ranked one filled the
budget is affected by that fact being forgotten (something else now fits), and it does not contain it. So
every write, correction, contradiction, forget, expiry and source deletion bumps ONE per-user counter, and
every cached entry from an older generation is a miss. It throws away more than it strictly must, and it
cannot be wrong.

The cache is per-process and in-memory. A second api instance keeps its own, and both bump on their own
writes — which is correct rather than merely convenient, because a generation is only ever compared
against entries created in the same process.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from uuid import UUID

log = logging.getLogger("bruce.memory.cache")   # CONTENT-FREE: ids, hit/miss, generations

# Small on purpose. This caches ONE turn's retrieval so a re-ask inside the same conversation is free; it
# is not a store, and a large one would mostly hold contexts for turns that will never recur.
MAX_ENTRIES = 512

_lock = threading.Lock()
_generation: dict[UUID, int] = {}
_entries: OrderedDict[tuple[UUID, str, int], object] = OrderedDict()


def generation(user_id: UUID) -> int:
    return _generation.get(user_id, 0)


def invalidate(user_id: UUID, *, reason: str) -> int:
    """Bump this student's generation. Every cached context for them becomes unreachable at once.

    Called on: a new memory, a correction, a contradiction, a forget, an expiry, a source deletion, an
    account isolation change, and a relevant entity update. `reason` is logged rather than branched on —
    if any of those ever stops calling this, the log is what shows it.
    """
    with _lock:
        nxt = _generation.get(user_id, 0) + 1
        _generation[user_id] = nxt
        # Drop this user's entries eagerly too. The generation check alone would be correct, but leaving
        # dead contexts holding forgotten content in process memory is not something to do on purpose.
        for key in [k for k in _entries if k[0] == user_id]:
            _entries.pop(key, None)
    log.info("memory_cache_invalidated reason=%s gen=%d", reason, nxt)
    return nxt


def get(user_id: UUID, cue) -> object | None:
    key = (user_id, cue.fingerprint(), generation(user_id))
    with _lock:
        hit = _entries.get(key)
        if hit is not None:
            _entries.move_to_end(key)
    if hit is None:
        return None
    from dataclasses import replace
    return replace(hit, cache_hit=True)


def put(user_id: UUID, cue, ctx) -> None:
    key = (user_id, cue.fingerprint(), generation(user_id))
    with _lock:
        _entries[key] = ctx
        _entries.move_to_end(key)
        while len(_entries) > MAX_ENTRIES:
            _entries.popitem(last=False)


def clear() -> None:
    """Tests only. Not exposed to production code, because "clear the cache" is never the right fix for
    a correctness problem — the generation bump is."""
    with _lock:
        _entries.clear()
        _generation.clear()
