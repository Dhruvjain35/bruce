"""Cloud Tasks dispatch — wake the private worker to drain durable intake_jobs.

Boundary discipline (production-shaped): the API NEVER does model work inline. After it durably
commits an intake_job it enqueues a Cloud Task; Cloud Tasks then invokes the private worker service
(OIDC-authenticated) which claims jobs with leases and processes them. intake_jobs stays the source
of truth — the task is only a wake signal, so a lost task self-heals (the job stays 'pending' and
the next task, retry, or drain picks it up).

Config-gated: with the BRUCE_TASKS_* env unset (local/dev/tests) this is a no-op, so the existing
in-process worker path (BRUCE_INPROC_WORKER) still works and tests don't need Cloud Tasks.
"""

from __future__ import annotations

import json
import os
from uuid import UUID


def _config() -> dict | None:
    """Returns the Cloud Tasks config, or None if not fully configured (-> dispatch is a no-op)."""
    project = os.environ.get("BRUCE_TASKS_PROJECT")
    location = os.environ.get("BRUCE_TASKS_LOCATION")
    queue = os.environ.get("BRUCE_TASKS_QUEUE")
    worker_url = os.environ.get("BRUCE_WORKER_URL")          # https://bruce-worker-...run.app
    invoker_sa = os.environ.get("BRUCE_TASKS_INVOKER_SA")     # SA email with run.invoker on the worker
    if not all([project, location, queue, worker_url, invoker_sa]):
        return None
    return {"project": project, "location": location, "queue": queue, "worker_url": worker_url, "invoker_sa": invoker_sa}


def dispatch_enabled() -> bool:
    return _config() is not None


async def enqueue_intake(job_id: UUID, user_id: UUID) -> bool:
    """Enqueue a Cloud Task to wake the worker for this job. Best-effort: returns False (never raises)
    if dispatch isn't configured or the enqueue fails — the job remains durably pending regardless."""
    cfg = _config()
    if cfg is None:
        return False
    try:
        from google.cloud import tasks_v2  # imported lazily so local/tests don't need the lib loaded

        client = tasks_v2.CloudTasksClient()
        parent = client.queue_path(cfg["project"], cfg["location"], cfg["queue"])
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f'{cfg["worker_url"].rstrip("/")}/process',
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"job_id": str(job_id)}).encode(),
                # OIDC token so Cloud Run's IAM admits ONLY this invoker SA on the private worker.
                "oidc_token": {"service_account_email": cfg["invoker_sa"], "audience": cfg["worker_url"].rstrip("/")},
            }
        }
        # Run the blocking client in a thread so we don't block the event loop.
        import asyncio

        await asyncio.to_thread(client.create_task, request={"parent": parent, "task": task})
        return True
    except Exception:
        # Content-free: never log the body. The job is durable; a sweeper/retry will still drain it.
        return False
