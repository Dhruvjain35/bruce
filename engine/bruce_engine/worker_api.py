"""Private worker service (Cloud Run, scale-to-zero) — invoked by Cloud Tasks to drain intake_jobs.

Exposes NO public surface: on Cloud Run it is deployed with no unauthenticated access, so only the
Cloud Tasks invoker service account (which holds run.invoker) can reach /process. That IAM boundary
IS the auth — there is no student data on this path, only a wake signal.

/process claims and processes a BOUNDED batch of pending jobs per invocation (not just one), so even
if a wake task is lost, a later invocation drains any leftover — combined with Cloud Tasks retries
this makes the queue self-healing. All durability + crash recovery is the job table's lease.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from . import notifier as notifier_mod
from . import worker
from .background_runner import BackgroundRunner, PlanMissionAdvancer
from .intake_jobs import PostgresJobStore
from .mission_executor import MissionExecutor

log = logging.getLogger(__name__)

app = FastAPI(title="Bruce Worker", version="0.1.0")

_MAX_DRAIN = int(os.environ.get("BRUCE_WORKER_DRAIN_MAX", "25"))


def background_runner_enabled() -> bool:
    """Kill switch, read at call time so a test (or a redeploy) can flip it without reimporting."""
    return os.environ.get("BRUCE_BACKGROUND_RUNNER_OFF", "").strip().lower() not in {"1", "true", "yes", "on"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "commit": os.environ.get("BRUCE_COMMIT", "unknown"), "env": os.environ.get("BRUCE_ENV", "local")}


@app.post("/process")
async def process() -> dict[str, int]:
    """Drain up to _MAX_DRAIN pending jobs. Each process_one claims one job under a lease, runs the
    (unchanged) extraction service, and persists — a crash mid-job just expires the lease."""
    store = PostgresJobStore()
    worker_id = f"cloudrun-{os.uname().nodename}-{os.getpid()}"
    processed, intake_errors = 0, 0
    for _ in range(_MAX_DRAIN):
        try:
            handled = await worker.process_one(store, worker_id=worker_id, lease_seconds=60)
        except Exception:
            # leave remaining jobs for the next invocation / retry — but SAY SO. A swallowed error here
            # used to be indistinguishable from an empty queue in both the response and the logs.
            intake_errors += 1
            log.exception("intake_drain_failed worker=%s processed=%d", worker_id, processed)
            break
        if not handled:
            break
        processed += 1

    # G0.5/Phase E: drain due background missions on the same wake with the REAL advancer — each claimed run
    # is driven through its plan steps (verified provider calls + waits), checkpointed, dead-lettered on
    # budget. Same lease/crash-recovery model. Isolated so an intake result is never lost to a background
    # error. C1 wires the live enqueue path, so this drain now has real work to claim.
    missions, mission_errors = 0, 0
    if background_runner_enabled():
        try:
            # notifier is None until a transport is verified end to end (see bruce_engine/notifier.py).
            # None is deliberate: the runner then completes the mission WITHOUT stamping notified=true,
            # so no run ever claims a delivery that did not happen.
            advancer = PlanMissionAdvancer(executor=MissionExecutor(),
                                           notifier=notifier_mod.build_notifier())
            missions = await BackgroundRunner(worker_id=worker_id, lease_seconds=60,
                                              advancer=advancer).drain(max_runs=_MAX_DRAIN)
        except Exception:
            # a mission crash must never look like a healthy empty queue: {"missions": 0} was previously
            # returned for BOTH, which made a production failure invisible.
            mission_errors += 1
            log.exception("background_drain_failed worker=%s", worker_id)
    return {"processed": processed, "missions": missions,
            "intake_errors": intake_errors, "mission_errors": mission_errors}
