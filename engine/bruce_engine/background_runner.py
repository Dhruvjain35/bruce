"""Background mission runner (G0.5 substrate + Phase E real advancer) — durably advance background_mission
AgentRuns off the request path, for real.

The worker CLAIMS one due run at a time via the AgentRun lease (FOR UPDATE SKIP LOCKED, worker session), then
drives the mission through its plan STEPS under that one lease: each step is a verified provider call (via the
MissionExecutor) or a WAIT; progress is CHECKPOINTED (fenced to the lease owner) after every step, the lease
is HEARTBEAT-renewed between consecutive steps, and the run yields only on a wait, completion, cancellation,
or failure. So it survives a worker restart (resume from the checkpoint, redo nothing), never double-writes or
double-notifies (idempotent steps + a notified flag), makes NO model call while merely waiting, and dead-
letters after the attempt budget instead of retrying forever.

Exactly-once for NON-idempotent side effects: a step executes before its checkpoint is durable, so a crash or
a lease-expiry can re-run it. Calendar ops are idempotent by read-back, but a Gmail send is not — so the
runner hands every step (and the notify) a stable per-step IDEMPOTENCY KEY (run_id:stepN); a non-idempotent
hand dedupes on it at the provider boundary. That is the contract for adding Gmail (Phase G) — the executor
must honor the key; the runner already supplies it.

The default advancer is still the honest NoopAdvancer; PlanMissionAdvancer is the REAL one, driving a plan of
steps. The first genuine end-user mission (send an email, wait for the reply, notify once) plugs in at Phase G.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import UUID

from . import agent_run_store, mission_executor
from .runtime_contracts import ActionType, NextAction

log = logging.getLogger("bruce.background_runner")   # content-free: run ids / statuses only

_DEFAULT_BACKOFF_SECONDS = 300
_MAX_STEPS_PER_CLAIM = 16            # bound one claim's work so a runaway plan can't starve other missions


@dataclass(frozen=True)
class AdvanceOutcome:
    """What one advance step decided. `done` -> commit the terminal `status`. Otherwise the run is
    rescheduled `retry_after_seconds` out (0 = continue to the next step immediately, under the same lease).
    `checkpoint` is the mission's recovery_state to persist BEFORE acting (restart-safe)."""
    done: bool = True
    status: str = "completed"          # terminal: completed | failed | cancelled | dead_letter
    note: str | None = None
    retry_after_seconds: int | None = None
    checkpoint: dict | None = None


class MissionAdvancer(Protocol):
    async def advance(self, run: dict) -> AdvanceOutcome: ...


class MissionStepFailed(Exception):
    """A plan step did not verify — surfaced so the runner's retry/dead-letter policy handles it."""


class NoopAdvancer:
    """No plan -> completed as an explicit no-op, never a fabricated 'i did the mission'."""

    async def advance(self, run: dict) -> AdvanceOutcome:
        return AdvanceOutcome(done=True, status="completed", note="no background mission handler registered")


class PlanMissionAdvancer:
    """The REAL advancer — drives a mission's stored plan ONE step per call. A run's goal carries
    {"steps": [{"kind": "action", "action": {...}} | {"kind": "wait", "seconds": N}], "notify": bool};
    recovery_state carries the checkpoint {"step_index", "notified", "results"}. Idempotent by construction:
    a completed step's index is checkpointed, so a resume never re-runs it."""

    def __init__(self, *, executor=None, notifier=None) -> None:
        self.executor = executor or mission_executor.MissionExecutor()
        self.notifier = notifier          # async notify(user_id, run) -> None ; sends exactly one message

    async def advance(self, run: dict) -> AdvanceOutcome:
        uid = UUID(run["user_id"])
        rid = run.get("id")
        goal = run.get("goal") or {}
        steps = list(goal.get("steps") or [])
        cp = dict(run.get("recovery_state") or {})
        idx = int(cp.get("step_index", 0))

        if idx >= len(steps):                                   # all steps done -> notify ONCE, then complete
            if goal.get("notify") and self.notifier is not None and not cp.get("notified"):
                await self.notifier(uid, run, idempotency_key=f"{rid}:notify")   # dedup key -> never double
                cp["notified"] = True
            return AdvanceOutcome(done=True, status="completed", note="mission complete", checkpoint=cp)

        step = steps[idx] or {}
        kind = step.get("kind")
        if kind == "wait":                                      # yield WITHOUT any work/model call
            cp["step_index"] = idx + 1
            return AdvanceOutcome(done=False, retry_after_seconds=int(step.get("seconds", 300)), checkpoint=cp)
        if kind == "action":
            # a stable per-step key: a non-idempotent hand (Gmail send) dedupes on it, so a crash-resume or a
            # lease-expiry re-run never doubles the side effect. Calendar ignores it (idempotent by read-back).
            tr = await self.executor.execute(uid, mission_executor.action_from_dict(step.get("action") or {}),
                                             idempotency_key=f"{rid}:step{idx}")
            if not tr.verified:
                raise MissionStepFailed(f"step {idx} unverified: {tr.reason}")   # -> runner retry/dead-letter
            cp["step_index"] = idx + 1
            # record the provider ids a later step needs (e.g. the thread to watch for a reply) — no user text
            cp.setdefault("results", {})[str(idx)] = {
                "verified": True, "op": tr.operation, "message_id": tr.provider_entity_id,
                "thread_id": (tr.read_back or {}).get("threadId")}
            return AdvanceOutcome(done=False, retry_after_seconds=0, checkpoint=cp)   # continue to next step
        if kind == "await_reply":
            # WAIT for an inbound reply in the thread a prior send opened. This is a POLL, not a busy loop: each
            # due tick does ONE cheap read (no model), and if no reply is in yet it reschedules and yields — so
            # a run can sleep for hours across worker restarts without holding a lease or spending a model call.
            src = str(step.get("from_step", idx - 1))
            prior = (cp.get("results") or {}).get(src) or {}
            thread_id, after_id = prior.get("thread_id"), prior.get("message_id")
            if not thread_id:
                raise MissionStepFailed("await_reply has no thread to watch")
            polls = int(cp.get("polls", 0))
            read = NextAction(type=ActionType.call_tool, capability="gmail.find_reply", provider="gmail",
                              operation="find_reply",
                              arguments={"thread_id": thread_id, "after_message_id": after_id})
            tr = await self.executor.execute(uid, read, idempotency_key=f"{rid}:step{idx}:poll{polls}")
            if tr.read_back:                                    # a reply arrived -> advance to the notify/next step
                cp["polls"] = 0
                cp["step_index"] = idx + 1
                cp.setdefault("results", {})[str(idx)] = {"reply": True, "reply_id": tr.read_back.get("id")}
                return AdvanceOutcome(done=False, retry_after_seconds=0, checkpoint=cp)
            if polls + 1 >= int(step.get("max_polls", 288)):    # gave up waiting -> finish honestly (no reply)
                cp["step_index"] = len(steps)
                cp["no_reply"] = True
                return AdvanceOutcome(done=False, retry_after_seconds=0, checkpoint=cp)
            cp["polls"] = polls + 1                             # still no reply -> keep waiting, do NOT advance
            return AdvanceOutcome(done=False, retry_after_seconds=int(step.get("poll_seconds", 300)), checkpoint=cp)
        raise MissionStepFailed(f"unknown step kind {kind!r}")


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
        """Claim ONE due run and drive it through consecutive ready steps under one lease. Returns False when
        nothing is due."""
        w = self.worker_id
        claimed = await agent_run_store.claim_background(w, lease_seconds=self.lease_seconds)
        if claimed is None:
            return False
        uid, rid = UUID(claimed["user_id"]), UUID(claimed["id"])

        if claimed["attempt_count"] > claimed["max_attempts"]:  # budget spent -> DEAD-LETTER (not a retry)
            await agent_run_store.complete_background(uid, rid, status="dead_letter",
                                                      note="max_attempts_exceeded", worker_id=w)
            return True

        for _ in range(_MAX_STEPS_PER_CLAIM):
            run = await agent_run_store.get_run(uid, rid)
            if run is None:                                     # transient invisibility -> reschedule; NEVER
                await agent_run_store.reschedule_background(   # advance a goal-less skeleton (would complete early)
                    uid, rid, next_run_at=_soon(self.backoff_seconds), worker_id=w, reset_attempts=False)
                return True
            if run.get("status") == "cancelled":                # cancelled while we held it -> stop, no advance
                return True
            try:
                outcome = await self.advancer.advance(run)
            except Exception:
                log.info("bg_advance_error run=%s attempt=%s", rid, claimed["attempt_count"])
                if claimed["attempt_count"] >= claimed["max_attempts"]:
                    await agent_run_store.complete_background(uid, rid, status="dead_letter",
                                                              note="advance_error", worker_id=w)
                else:
                    await agent_run_store.reschedule_background(uid, rid, next_run_at=_soon(self.backoff_seconds),
                                                               worker_id=w, reset_attempts=False)
                return True

            if outcome.checkpoint is not None:                  # persist progress BEFORE acting (restart-safe)
                await agent_run_store.checkpoint_background(uid, rid, recovery_state=outcome.checkpoint, worker_id=w)

            if outcome.done:
                await agent_run_store.complete_background(uid, rid, status=outcome.status,
                                                          note=outcome.note, worker_id=w)
                return True
            secs = outcome.retry_after_seconds if outcome.retry_after_seconds is not None else self.backoff_seconds
            if secs > 0:                                        # a WAIT -> reschedule and yield (no work while waiting)
                await agent_run_store.reschedule_background(uid, rid, next_run_at=_soon(secs),
                                                           worker_id=w, reset_attempts=True)
                return True
            if not await agent_run_store.renew_background_lease(w, rid, lease_seconds=self.lease_seconds):
                return True                                     # lost the lease -> another worker owns it; stop
        # hit the per-claim step bound -> reschedule to continue promptly, fair to other missions
        await agent_run_store.reschedule_background(uid, rid, next_run_at=_soon(1), worker_id=w, reset_attempts=True)
        return True

    async def drain(self, *, max_runs: int = 25) -> int:
        n = 0
        while n < max_runs and await self.run_once():
            n += 1
        return n
