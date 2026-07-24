"""Background mission runner (G0.5) — durably advance background_mission AgentRuns off the request path.

A durable background run (a follow-up, a monitor, a long "stay on top of X til friday" handoff) can't run
inside the inbound turn. This runner is the substrate: it CLAIMS one due run at a time via the AgentRun
lease (FOR UPDATE SKIP LOCKED, worker session) so N workers never double-execute, advances it one step
through a pluggable MissionAdvancer, and commits or reschedules from the result. A crash just drops the
lease — the next poll reclaims the run (resumable), and attempts are bounded so nothing retries forever.

The claim/lease/resume machinery is the deliverable here. Real mission WORK plugs in as a MissionAdvancer
when the first multi-step mission exists (there is none yet — the default advancer is an honest no-op, not
a fabricated success). This mirrors the router's Stage-1 and the loop's Tier-1 seams: infrastructure first,
handlers as they land.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from . import agent_run_store

log = logging.getLogger("bruce.background_runner")   # content-free: run ids / statuses only

_DEFAULT_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class AdvanceOutcome:
    """What one advance step decided. `done` -> commit the terminal `status`; otherwise the run is
    rescheduled `retry_after_seconds` out (or the runner's default backoff)."""
    done: bool = True
    status: str = "completed"          # terminal status when done: completed | failed | cancelled
    note: str | None = None
    retry_after_seconds: int | None = None


class MissionAdvancer(Protocol):
    async def advance(self, run: dict) -> AdvanceOutcome: ...


class NoopAdvancer:
    """No multi-step mission handlers exist yet, so a claimed run is completed as an explicit no-op — never
    a fabricated 'i did the mission'. Replaced by real advancers when the first mission lands."""

    async def advance(self, run: dict) -> AdvanceOutcome:
        return AdvanceOutcome(done=True, status="completed", note="no background mission handler registered")


def _soon(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class BackgroundRunner:
    def __init__(self, *, worker_id: str, lease_seconds: int = 60, advancer: MissionAdvancer | None = None,
                 backoff_seconds: int = _DEFAULT_BACKOFF_SECONDS) -> None:
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.advancer = advancer or NoopAdvancer()
        self.backoff_seconds = backoff_seconds

    async def run_once(self) -> bool:
        """Claim + advance ONE due run. Returns False when nothing is due (drain complete).

        Every owner write below is FENCED by self.worker_id, so if the lease expired mid-advance and another
        worker reclaimed the run, this worker's write is a no-op instead of clobbering the new owner. NOTE:
        an advancer MUST finish well within lease_seconds (or renew via renew_background_lease); otherwise the
        run can be reclaimed and advanced by two workers — real advancers with external side effects must
        also be idempotent. The instant NoopAdvancer trivially satisfies this."""
        w = self.worker_id
        claimed = await agent_run_store.claim_background(w, lease_seconds=self.lease_seconds)
        if claimed is None:
            return False
        uid, rid = UUID(claimed["user_id"]), UUID(claimed["id"])

        # give up cleanly once the attempt budget is spent (the claim just incremented attempt_count).
        if claimed["attempt_count"] > claimed["max_attempts"]:
            await agent_run_store.complete_background(uid, rid, status="failed",
                                                      note="max_attempts_exceeded", worker_id=w)
            return True

        try:
            run = await agent_run_store.get_run(uid, rid) or claimed
            outcome = await self.advancer.advance(run)
        except Exception:
            log.info("bg_advance_error run=%s attempt=%s", rid, claimed["attempt_count"])
            if claimed["attempt_count"] >= claimed["max_attempts"]:
                await agent_run_store.complete_background(uid, rid, status="failed",
                                                          note="advance_error", worker_id=w)
            else:                                             # a FAILURE reschedule — keep the failure count
                await agent_run_store.reschedule_background(uid, rid, next_run_at=_soon(self.backoff_seconds),
                                                            worker_id=w, reset_attempts=False)
            return True

        if outcome.done:
            await agent_run_store.complete_background(uid, rid, status=outcome.status,
                                                      note=outcome.note, worker_id=w)
        else:                                                 # a HEALTHY recurring check — reset the budget
            secs = outcome.retry_after_seconds if outcome.retry_after_seconds is not None else self.backoff_seconds
            await agent_run_store.reschedule_background(uid, rid, next_run_at=_soon(secs),
                                                        worker_id=w, reset_attempts=True)
        return True

    async def drain(self, *, max_runs: int = 25) -> int:
        """Advance up to `max_runs` due runs this invocation; a crash mid-run just leaves the lease to expire."""
        n = 0
        while n < max_runs and await self.run_once():
            n += 1
        return n
