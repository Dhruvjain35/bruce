"""Intake worker — claims durable jobs and does the model work outside the request lifecycle.

`process_one` is the unit of work: claim a job, run the (unchanged, synchronous) extraction service
on it, persist the result and advance the mission, then mark the job. It is deliberately storage-
agnostic — it takes a JobStore, so the same logic runs against Postgres in production and an
in-memory store in fast tests.

Failure taxonomy (this is where the "no false completion" guarantee lives on the async path):
  * ExtractionError  (unsupported type / unreadable / empty read) -> NON-retryable. The input can't
    be read no matter how many times we try. Job -> terminal_failed, mission -> failed.
  * ProviderUnavailable (outage / 20s budget timeout) -> RETRYABLE. Job -> retryable_failed and
    mission -> blocked while attempts remain; on the last attempt -> terminal_failed / failed.
  * A crash mid-process leaves status=processing with a lease that expires -> another worker reclaims.

`IntakeWorker` wraps `process_one` in a poll loop for in-process operation. THIS IS LABELLED: the
loop is a convenience, not the durability mechanism — durability comes from the job table + lease,
so a dedicated worker process can replace the in-process loop with no contract change.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

from . import intake_store
from .extraction import (
    ExtractionError,
    extract_from_image_traced,
    extract_from_pdf_traced,
    extract_from_text_traced,
)
from .intake_jobs import ClaimedJob, JobStore
from .models import IntakeSourceKind, MissionPhase
from .provider_status import ProviderUnavailable


async def _extract_for_job(job: ClaimedJob):
    """Dispatch to the existing extraction service by source kind. Returns (intake, telemetry)."""
    kind = IntakeSourceKind(job.source_kind)
    if kind is IntakeSourceKind.image:
        return await extract_from_image_traced(job.input_bytes or b"", mime=job.mime or "image/png")
    if kind is IntakeSourceKind.pdf:
        return await extract_from_pdf_traced(job.input_bytes or b"")
    return await extract_from_text_traced(job.input_text or "", source_kind=kind)


async def process_one(store: JobStore, *, worker_id: str, lease_seconds: int = 60) -> bool:
    """Claim and process a single job. Returns True if a job was handled, False if the queue was idle.

    Never raises for an expected intake failure — those become durable mission/job states. Only a
    truly unexpected error propagates (and the lease will expire so the job is reclaimed)."""
    job = await store.claim(worker_id, lease_seconds)
    if job is None:
        return False

    kind = IntakeSourceKind(job.source_kind)
    # Show the student progress before the model call: understanding -> extracting. Idempotent on a
    # reclaim (re-advancing to the same phase just appends another event, harmless).
    await intake_store.advance_intake_phase(
        user_id=job.user_id, mission_id=job.mission_id, phase=MissionPhase.extracting, source_kind=kind
    )
    try:
        intake, telem = await _extract_for_job(job)
    except ExtractionError as exc:
        # The input cannot be read — retrying won't help. Terminal, and the mission says so honestly.
        await store.mark_terminal(job, f"{type(exc).__name__}: {exc}")
        await intake_store.fail_intake_mission(
            user_id=job.user_id, mission_id=job.mission_id, phase=MissionPhase.failed,
            reason=f"{type(exc).__name__}", source_kind=kind,
        )
        return True
    except ProviderUnavailable as exc:
        # Transient. Retry while attempts remain (mission blocked, not failed); else give up honestly.
        if job.can_retry:
            await store.mark_retryable(job, f"{type(exc).__name__}: {exc}")
            await intake_store.fail_intake_mission(
                user_id=job.user_id, mission_id=job.mission_id, phase=MissionPhase.blocked,
                reason="provider_unavailable — retrying", source_kind=kind,
            )
        else:
            await store.mark_terminal(job, f"{type(exc).__name__}: {exc}")
            await intake_store.fail_intake_mission(
                user_id=job.user_id, mission_id=job.mission_id, phase=MissionPhase.failed,
                reason="provider_unavailable — gave up after retries", source_kind=kind,
            )
        return True

    # Success: persist content + advance mission (idempotent), then record job completion.
    transcript = intake.raw_source_excerpt if kind in (IntakeSourceKind.image, IntakeSourceKind.pdf) else None
    await intake_store.complete_intake_extraction(
        user_id=job.user_id, source_id=job.source_id, mission_id=job.mission_id,
        source_key=job.idempotency_key or "", intake=intake, transcript=transcript,
    )
    await store.mark_completed(job)
    return True


class IntakeWorker:
    """In-process poll loop over process_one. A convenience wrapper — durability is the job table."""

    def __init__(self, store: JobStore, *, worker_id: str | None = None, idle_sleep: float = 0.5, lease_seconds: int = 60):
        self.store = store
        self.worker_id = worker_id or f"worker-{os.getpid()}"
        self.idle_sleep = idle_sleep
        self.lease_seconds = lease_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                handled = await process_one(self.store, worker_id=self.worker_id, lease_seconds=self.lease_seconds)
            except Exception:
                handled = False  # unexpected error: the lease expires and the job is reclaimed
            if not handled:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.idle_sleep)

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
            self._task = None
