"""The notifier seam (C2) — how a finished background mission reaches the student.

Deliberately a seam and nothing more right now. A mission that has done its work still has to TELL the
student, and the only channel that counts for Bruce is the one the student already uses: iMessage, via
the relay. That transport is not wired here yet, and this module exists so that fact stays explicit
instead of being discovered later.

The one rule that matters: `build_notifier()` returns None until a real transport exists, and None is
load-bearing. `PlanMissionAdvancer` only stamps `recovery_state["notified"] = True` when a notifier is
actually invoked, so a None notifier lets the mission complete WITHOUT recording a delivery that never
happened. Wiring a do-nothing notifier here instead would be strictly worse than wiring none: the run
would carry `notified: true` and Bruce would believe it had told the student something it never sent.

When the relay transport is ready, `build_notifier()` returns a real Notifier and the exactly-once
contract is already in place on both sides: the runner passes a stable `idempotency_key` of
`f"{run_id}:notify"`, and the relay keeps its own outbound ledger.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol
from uuid import UUID

log = logging.getLogger(__name__)


class Notifier(Protocol):
    """Delivers exactly one message about a finished mission.

    `idempotency_key` is stable across retries and lease reclaims — a transport MUST dedupe on it rather
    than assuming it is called once, because a worker can crash between sending and checkpointing.
    """

    async def __call__(self, user_id: UUID, run: dict, *, idempotency_key: str) -> None: ...


class RecordingNotifier:
    """Test double. Captures deliveries in memory so a test can assert EXACTLY one, keyed by idempotency
    key. Not for production use: recording a delivery is not making one."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def __call__(self, user_id: UUID, run: dict, *, idempotency_key: str) -> None:
        self.keys.append(idempotency_key)

    @property
    def unique_keys(self) -> set[str]:
        return set(self.keys)


def transport_configured() -> bool:
    """Whether a real outbound transport has been wired AND switched on. Both are required: the flag alone
    must never be enough to claim delivery."""
    return os.environ.get("BRUCE_MISSION_NOTIFIER", "").strip().lower() in {"1", "true", "yes", "on"}


def build_notifier() -> Notifier | None:
    """Return the production notifier, or None while no transport exists.

    Returns None today. This is not a TODO left dangling — it is the honest state of the system, and the
    runner's behaviour on None (complete the mission, record no notification) is the correct one until a
    real iMessage send has been verified end to end from the relay.
    """
    if transport_configured():
        log.warning("mission_notifier_requested_but_unbuilt — no transport is wired; refusing to claim delivery")
    return None
