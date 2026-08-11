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
from . import semantic_shadow, worker
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
async def process() -> dict:   # no longer dict[str,int]: the shadow health block is nested counts
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
    # SHADOW OBSERVATION, on the same wake and the same lease model. Each claim reads one queued turn
    # with the semantic executive and records a TYPED outcome on the job row — it creates no goal, no
    # Decision, no authorization and no provider call (see semantic_shadow). Isolated in its own loop for
    # the same reason as the mission drain: a shadow failure must never cost an intake result. The kill
    # switch lives inside `claim_and_observe`, so with shadow off this is one no-op call and no DB round
    # trip. Errors are COUNTED rather than swallowed — an invisible failure here is exactly how the
    # best-effort version lost records without anyone noticing.
    observed, shadow_errors, exhausted = 0, 0, 0
    # NO POPULATION IS BUILT HERE, AND THAT IS THE FIX. This function used to collect the user ids of the
    # jobs `claim_and_observe` returned and hand them to `health(reconcile_users=...)` as the set to
    # reconcile — the only ids a worker could get, since `conversation_turns` was invisible to it. A user
    # whose intake call was MISSING has no job, so was never in that list, so was never checked; and an
    # empty queue produced an empty list, which made `reconciled: True` the answer emitted on every wake
    # with an idle queue. A reconciliation whose population comes from the thing being reconciled cannot
    # fail. `semantic_shadow.reconcile_population` now reads the population from the canonical ledger
    # itself, and `health()` takes no user list at all so this cannot be re-introduced by a caller.
    #
    # AND NO POPULATION IS RECEIVED HERE EITHER, which is the second half. The per-owner aggregate
    # returned one row PER OWNER, so the reconciliation still had to loop those owners and open a session
    # per tenant to finish the join — the worker held every owner in the window, including the ones with
    # no jobs. Migration 0035 moved the join inside the SECURITY DEFINER function: this process now
    # receives counts and a verdict, and no user id crosses into it at all.
    #
    # HOW FAR BEHIND THE WAKE STARTED. Deliberately read BEFORE the drain, which is what makes it a
    # different number from the `queued` in the health block below: together they say whether this wake
    # made progress. `backlog()` had no production caller at all, so the durable queue was measurable in
    # principle and measured nowhere — the same blindness as the detached task it replaced, only slower.
    backlog_before = -1        # -1 is "not read", never a cheerful 0
    try:
        backlog_before = await semantic_shadow.backlog()
    except Exception as exc:
        shadow_errors += 1
        log.warning("shadow_backlog_failed worker=%s err=%s", worker_id, type(exc).__name__)
    try:
        # THE RETRY BOUND'S BOOKKEEPING, before the drain. A worker killed between claiming a shadow job
        # and recording it leaves an expiring lease on a row whose attempts have already advanced; once
        # they reach max_attempts nothing claims it again, so without this it would sit in the backlog
        # forever and the queue would look permanently behind. One idempotent UPDATE — no read, no lease,
        # no provider call — that relabels those rows `exhausted_infrastructure_retries`.
        exhausted = await semantic_shadow.sweep_exhausted()
    except Exception as exc:
        shadow_errors += 1
        log.warning("shadow_sweep_failed worker=%s err=%s", worker_id, type(exc).__name__)
    for _ in range(_MAX_DRAIN):
        try:
            job = await semantic_shadow.claim_and_observe(worker_id=worker_id, lease_seconds=60)
        except Exception as exc:
            shadow_errors += 1
            # The exception TYPE only: a shadow error can carry the student's message in its text.
            log.warning("shadow_drain_failed worker=%s observed=%d err=%s",
                        worker_id, observed, type(exc).__name__)
            break
        if job is None:
            break
        observed += 1

    # SHADOW HEALTH, emitted on the wake that just drained it. `counts()`/`backlog()` had no production
    # caller at all, so the queue was measurable in principle and measured nowhere — which is the same
    # blindness the detached-task version had, only slower to notice. Both reconciliation invariants ride
    # along (`intake.reconciled`, `queue.reconciled`), so work that went missing shows up as a number
    # rather than as an absence nobody counted.
    #
    # Every field is a count, an age or a boolean — no ids, no text — so it is safe both in the log and
    # in this response body, which Cloud Tasks records.
    #
    # `turns` is the INDEPENDENT one: trusted inbound turns counted from conversation_turns against the
    # rows shadow wrote for them, over the population that ledger names rather than the one this wake
    # happened to touch. The other two ledgers are shadow marking its own homework — the intake counter
    # used to define its denominator as the sum of its own numerators, so it reconciled perfectly while
    # an entire population of turns never reached it, and then the population selection did the same
    # thing one layer out.
    #
    # `turns.reconciliation_status` IS THE DATABASE'S VERDICT AND THIS FUNCTION IS NOT ALLOWED AN OPINION
    # ON IT. It is decided inside the aggregate function, in the same statement and the same snapshot as
    # the counts beside it, from anti-joins nothing in this process can see. Nothing here derives it,
    # upgrades it, downgrades it, or recomputes it from the counts in the block — a status computed twice
    # from the same numbers agrees today and drifts the first time either copy is edited, and the drift
    # is invisible because both look reasonable. That re-derivation is exactly how a collapsed population
    # was published as `clean`, twice. The block is passed through verbatim.
    shadow_health: dict = {}
    try:
        shadow_health = await semantic_shadow.health()
        log.info("shadow_health worker=%s backlog_before=%d intake=%s queue=%s turns=%s", worker_id,
                 backlog_before, shadow_health["intake"], shadow_health["queue"],
                 shadow_health["turns"])
    except Exception as exc:
        # A health read that fails must SAY so rather than report zeros: "the queue is empty" and "I
        # could not read the queue" are the two states this whole outbox exists to keep apart.
        shadow_errors += 1
        log.warning("shadow_health_failed worker=%s err=%s", worker_id, type(exc).__name__)

    return {"processed": processed, "missions": missions, "observed": observed,
            "intake_errors": intake_errors, "mission_errors": mission_errors,
            "shadow_errors": shadow_errors, "shadow_exhausted": exhausted,
            "shadow_backlog_before": backlog_before, "shadow_health": shadow_health}
