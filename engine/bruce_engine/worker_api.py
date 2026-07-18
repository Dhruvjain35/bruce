"""Private worker service (Cloud Run, scale-to-zero) — invoked by Cloud Tasks to drain intake_jobs.

Exposes NO public surface: on Cloud Run it is deployed with no unauthenticated access, so only the
Cloud Tasks invoker service account (which holds run.invoker) can reach /process. That IAM boundary
IS the auth — there is no student data on this path, only a wake signal.

/process claims and processes a BOUNDED batch of pending jobs per invocation (not just one), so even
if a wake task is lost, a later invocation drains any leftover — combined with Cloud Tasks retries
this makes the queue self-healing. All durability + crash recovery is the job table's lease.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from . import worker
from .intake_jobs import PostgresJobStore

app = FastAPI(title="Bruce Worker", version="0.1.0")

_MAX_DRAIN = int(os.environ.get("BRUCE_WORKER_DRAIN_MAX", "25"))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "commit": os.environ.get("BRUCE_COMMIT", "unknown"), "env": os.environ.get("BRUCE_ENV", "local")}


@app.post("/process")
async def process() -> dict[str, int]:
    """Drain up to _MAX_DRAIN pending jobs. Each process_one claims one job under a lease, runs the
    (unchanged) extraction service, and persists — a crash mid-job just expires the lease."""
    store = PostgresJobStore()
    worker_id = f"cloudrun-{os.uname().nodename}-{os.getpid()}"
    processed = 0
    for _ in range(_MAX_DRAIN):
        try:
            handled = await worker.process_one(store, worker_id=worker_id, lease_seconds=60)
        except Exception:
            break  # unexpected error: leave remaining jobs for the next invocation / retry
        if not handled:
            break
        processed += 1
    return {"processed": processed}
