"""M1B — make Stage-0 misses OBSERVABLE, without changing what the router decides.

THE PROBLEM. `fast_router._stage0` returns None when no deterministic rule fires, and `_stage1` returns
`_DEFAULT` (fast_conversation) whenever no model provider exists — which is always, since Stage-1 has
been gated off since its live calibration measured p50 1383ms / p95 2002ms / 12.5% timeouts. So every
paraphrase the router does not recognise becomes "casual chat", and a miss is indistinguishable from a
correct chat classification. The true miss rate is currently unmeasurable.

WHAT THIS DOES, AND DELIBERATELY DOES NOT DO. It classifies each miss into exactly one bucket and logs
it content-free. It does NOT change the returned decision, and it does NOT call a model.

Not calling a model is a deliberate cost decision, not laziness: a shadow semantic call on every miss
would add the very latency that got Stage-1 switched off, on turns that are already falling through. The
cheap deterministic lookups (is a Decision open? is a run in flight?) answer the question that actually
matters first — how many misses are resolvable WITHOUT a model. If the report shows most misses need
semantics, that is the moment to measure the model path, with a number justifying the spend.

READ THE REPORT BEFORE GRANTING AUTHORITY. UNKNOWN must not become authoritative on a hunch, and the
old silent fallback must not survive alongside it once it does.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from uuid import UUID

log = logging.getLogger(__name__)

# Buckets. Exactly one per miss — a miss that could match two is recorded by precedence, highest first,
# because a reply to an open question is a stronger explanation than a stray pronoun.
PENDING_DECISION = "pending_decision"        # an open Decision exists; the text likely answers it
ACTIVE_RUN = "active_run"                    # work is in flight; likely a continuation or status ask
REFERENCE_ONLY = "reference_only"            # deixis with nothing else to go on ("that one", "it")
NEEDS_SEMANTIC = "needs_semantic"            # nothing deterministic explains it; a model call is needed
SHADOW_ERROR = "shadow_error"                # the shadow itself failed; never affects the turn

# Deixis with no other signal. NOT an intent matcher — it only explains WHY a miss happened, and is
# never used to route. Normalized first, because curly apostrophes have silently broken three matchers
# in this codebase already.
_DEIXIS = re.compile(
    r"\b(it|that|this|those|these|them|they|he|she|him|her|same|one)\b"
    r"|\bwhat about\b|\bwhich one\b|\bthe other\b")


@dataclass(frozen=True)
class MissObservation:
    bucket: str
    elapsed_ms: float
    had_pending_decision: bool = False
    had_active_run: bool = False
    text_len: int = 0                    # length only — never content
    reason: str = ""


async def classify_miss(user_id: UUID, text: str) -> MissObservation:
    """Explain ONE Stage-0 miss. Best-effort, bounded, content-free, and never raises into the turn."""
    from . import text_norm

    started = time.perf_counter()
    folded = text_norm.fold_match(text or "")
    pending = active = False
    try:
        from . import agent_run_store, mission_kernel
        try:
            pending = bool(await mission_kernel.latest_pending_calendar_mission(user_id))
        except Exception:
            pending = False
        try:
            # domain=None so a gmail mission is visible too — defaulting this to "calendar" is exactly
            # how background work became invisible to the runtime once already.
            active = bool(await agent_run_store.latest_active(user_id, domain=None))
        except Exception:
            active = False
    except Exception:
        return MissObservation(SHADOW_ERROR, (time.perf_counter() - started) * 1000,
                               text_len=len(text or ""), reason="lookup failed")

    if pending:
        bucket, reason = PENDING_DECISION, "an open decision is awaiting a reply"
    elif active:
        bucket, reason = ACTIVE_RUN, "work is in flight; likely continuation or status"
    elif _DEIXIS.search(folded):
        bucket, reason = REFERENCE_ONLY, "deixis with no other deterministic signal"
    else:
        bucket, reason = NEEDS_SEMANTIC, "no deterministic explanation; semantics required"

    obs = MissObservation(bucket, (time.perf_counter() - started) * 1000, pending, active,
                          len(text or ""), reason)
    # content-free: bucket, booleans, length, latency. No message text ever.
    log.info("router_miss bucket=%s pending=%s active=%s len=%d ms=%.1f",
             obs.bucket, obs.had_pending_decision, obs.had_active_run, obs.text_len, obs.elapsed_ms)
    return obs


def is_miss(decision) -> bool:
    """A decision that came from the silent default rather than a real classification."""
    return getattr(decision, "source", None) == "router_default"
