"""AgentRun persistence (R2) — durable working state for the general runtime, surviving messages /
restarts / retries / corrections. Holds GoalSpec + TemporalSpec + selected entity + current NextAction +
last tool result + active decision, so execution state is read from HERE, never reconstructed from recent
chat. tenant_or_worker RLS (a resuming worker writes it). Idempotent create on (owner, idempotency_key).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from . import schema
from .db import user_session, worker_session

_MUTABLE = {"status", "goal", "temporal", "selected_entity_id", "selected_provider_account",
            "current_action", "last_tool_result", "verification_result", "active_decision",
            "recovery_state", "blocked_reason", "completed_at"}


def _to_dict(r: "schema.AgentRun") -> dict:
    return {
        "id": str(r.id), "user_id": str(r.user_id), "domain": r.domain, "status": r.status,
        "goal": r.goal, "temporal": r.temporal, "selected_entity_id": str(r.selected_entity_id) if r.selected_entity_id else None,
        "selected_provider_account": r.selected_provider_account, "current_action": r.current_action,
        "last_tool_result": r.last_tool_result, "verification_result": r.verification_result,
        "active_decision": r.active_decision, "recovery_state": r.recovery_state,
        "blocked_reason": r.blocked_reason, "mission_id": str(r.mission_id) if r.mission_id else None,
    }


async def create_run(user_id: UUID, *, domain: str = "calendar", goal: dict | None = None,
                     mission_id: UUID | None = None, idempotency_key: str | None = None,
                     status: str = "understanding", next_run_at: datetime | None = None) -> dict:
    """Create (or reference, if idempotency_key already exists) the run + its first event, atomically.
    `next_run_at` is set in THIS transaction so a scheduled background run is never briefly claimable with
    a NULL next_run_at (which the claim treats as due-now)."""
    async with user_session(user_id) as s:
        if idempotency_key:
            ex = (await s.execute(select(schema.AgentRun).where(
                schema.AgentRun.user_id == user_id,
                schema.AgentRun.idempotency_key == idempotency_key))).scalar_one_or_none()
            if ex is not None:
                return _to_dict(ex)
        run = schema.AgentRun(user_id=user_id, domain=domain, status=status, goal=goal or {},
                              mission_id=mission_id, idempotency_key=idempotency_key, next_run_at=next_run_at)
        s.add(run)
        try:
            await s.flush()
        except IntegrityError:
            async with user_session(user_id) as s2:
                ex = (await s2.execute(select(schema.AgentRun).where(
                    schema.AgentRun.user_id == user_id,
                    schema.AgentRun.idempotency_key == idempotency_key))).scalar_one_or_none()
                if ex is not None:
                    return _to_dict(ex)
            raise
        s.add(schema.AgentRunEvent(user_id=user_id, agent_run_id=run.id, status=status, detail={}))
        await s.flush()
        return _to_dict(run)


async def get_run(user_id: UUID, run_id: UUID) -> dict | None:
    async with user_session(user_id) as s:
        r = (await s.execute(select(schema.AgentRun).where(
            schema.AgentRun.id == run_id, schema.AgentRun.user_id == user_id))).scalar_one_or_none()
        return _to_dict(r) if r is not None else None


async def latest_active(user_id: UUID, *, domain: str = "calendar") -> dict | None:
    """Most recent run not in a terminal state — resumes across messages/restarts."""
    async with user_session(user_id) as s:
        r = (await s.execute(select(schema.AgentRun).where(
            schema.AgentRun.user_id == user_id, schema.AgentRun.domain == domain,
            schema.AgentRun.status.notin_(("completed", "failed", "cancelled"))).order_by(
            schema.AgentRun.created_at.desc()).limit(1))).scalar_one_or_none()
        return _to_dict(r) if r is not None else None


async def update_run(user_id: UUID, run_id: UUID, **fields) -> None:
    """Patch mutable run fields + append a transition event when the status changes."""
    async with user_session(user_id) as s:
        r = (await s.execute(select(schema.AgentRun).where(
            schema.AgentRun.id == run_id, schema.AgentRun.user_id == user_id))).scalar_one_or_none()
        if r is None:
            return
        status_changed = "status" in fields and fields["status"] != r.status
        for k, v in fields.items():
            if k in _MUTABLE:
                setattr(r, k, v)
        if status_changed:
            s.add(schema.AgentRunEvent(user_id=user_id, agent_run_id=run_id, status=fields["status"],
                                       detail=fields.get("event_detail", {})))
        await s.flush()


# --- Background-mission lease (G0.5) -------------------------------------------------------------------
# A durable background run advanced by the worker. The CLAIM is atomic (FOR UPDATE SKIP LOCKED) under a
# WORKER RLS session — the only cross-user step — exactly like intake_jobs; the per-run transitions run
# under the run OWNER's session. Status vocabulary is disjoint from direct-action runs (queued/running vs
# understanding/executing), so the claim can never grab a synchronous direct action.

_CLAIM_BG = sa_text("""
    UPDATE agent_runs SET
        status = 'running',
        lease_owner = :worker,
        lease_expires_at = now() + make_interval(secs => :lease),
        attempt_count = attempt_count + 1,
        version = version + 1,
        updated_at = now()
    WHERE id = (
        SELECT id FROM agent_runs
        WHERE (status = 'queued' AND (next_run_at IS NULL OR next_run_at <= now()))
           OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at < now())
        ORDER BY COALESCE(next_run_at, created_at)
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, user_id, domain, status, attempt_count, max_attempts
""")


async def enqueue_background(user_id: UUID, *, domain: str = "mission", goal: dict | None = None,
                             next_run_at: datetime | None = None,
                             idempotency_key: str | None = None) -> dict:
    """Enqueue a durable background mission for the worker to claim (status 'queued'). `next_run_at` gates
    when it first becomes claimable (None = immediately) — set atomically with the insert, so a scheduled
    run is never momentarily due-now."""
    return await create_run(user_id, domain=domain, goal=goal, idempotency_key=idempotency_key,
                            status="queued", next_run_at=next_run_at)


async def claim_background(worker_id: str, *, lease_seconds: int = 60) -> dict | None:
    """Atomically claim the next due background run across ALL users (worker session; SKIP LOCKED so N
    workers never grab the same run). Reclaims a 'running' run whose lease expired (crash recovery).
    Returns identity + attempt counters, or None if nothing is due."""
    async with worker_session() as s:
        row = (await s.execute(_CLAIM_BG, {"worker": worker_id[:64], "lease": lease_seconds})).mappings().first()
    if row is None:
        return None
    return {"id": str(row["id"]), "user_id": str(row["user_id"]), "domain": row["domain"],
            "status": row["status"], "attempt_count": row["attempt_count"], "max_attempts": row["max_attempts"]}


async def renew_background_lease(worker_id: str, run_id: UUID, *, lease_seconds: int = 60) -> bool:
    """Extend the lease on a run THIS worker holds (worker session). False if it no longer owns it."""
    async with worker_session() as s:
        res = await s.execute(sa_text(
            "UPDATE agent_runs SET lease_expires_at = now() + make_interval(secs => :lease), "
            "version = version + 1, updated_at = now() "
            "WHERE id = :id AND lease_owner = :worker AND status = 'running'"),
            {"id": str(run_id), "worker": worker_id[:64], "lease": lease_seconds})
    return res.rowcount > 0


async def complete_background(user_id: UUID, run_id: UUID, *, status: str = "completed",
                             note: str | None = None, worker_id: str | None = None) -> None:
    """Terminal transition for a background run, run as the OWNER. FENCED by `worker_id`: if this worker no
    longer holds the lease (its lease expired and another worker reclaimed the run), the write is a no-op —
    it must not clobber the new owner's in-flight state."""
    async with user_session(user_id) as s:
        cond = "WHERE id = :id AND user_id = :uid"
        params: dict = {"st": status, "note": (note or None), "id": str(run_id), "uid": str(user_id)}
        if worker_id is not None:
            cond += " AND lease_owner = :worker"
            params["worker"] = worker_id[:64]
        res = await s.execute(sa_text(
            "UPDATE agent_runs SET status = :st, completed_at = now(), lease_owner = NULL, "
            "lease_expires_at = NULL, blocked_reason = :note, version = version + 1, updated_at = now() "
            + cond), params)
        if res.rowcount:                                      # only record the transition if we owned the row
            s.add(schema.AgentRunEvent(user_id=user_id, agent_run_id=run_id, status=status,
                                       detail={"note": note} if note else {}))
            await s.flush()


async def reschedule_background(user_id: UUID, run_id: UUID, *, next_run_at: datetime,
                                worker_id: str | None = None, reset_attempts: bool = False) -> None:
    """Return a background run to the queue for a later attempt (owner session); clears the lease. FENCED by
    `worker_id` like complete_background. `reset_attempts` zeroes attempt_count on a HEALTHY reschedule
    (done=False) so the failure budget bounds only CONSECUTIVE failures, never healthy recurring checks."""
    async with user_session(user_id) as s:
        cond = "WHERE id = :id AND user_id = :uid"
        params: dict = {"nra": next_run_at, "id": str(run_id), "uid": str(user_id)}
        if worker_id is not None:
            cond += " AND lease_owner = :worker"
            params["worker"] = worker_id[:64]
        reset = "attempt_count = 0, " if reset_attempts else ""
        await s.execute(sa_text(
            "UPDATE agent_runs SET status = 'queued', next_run_at = :nra, lease_owner = NULL, "
            "lease_expires_at = NULL, " + reset + "version = version + 1, updated_at = now() " + cond),
            params)


async def checkpoint_background(user_id: UUID, run_id: UUID, *, recovery_state: dict,
                               worker_id: str | None = None) -> None:
    """Persist a mission's progress checkpoint (recovery_state) FENCED to the lease owner in the UPDATE's own
    WHERE (write-time, like complete/reschedule — not a read-then-flush) so a worker that lost the lease can
    never clobber the new owner's more-advanced checkpoint. A restart resumes from here, redoing no step."""
    import json
    async with user_session(user_id) as s:
        cond = "WHERE id = :id AND user_id = :uid"
        params: dict = {"cp": json.dumps(recovery_state), "id": str(run_id), "uid": str(user_id)}
        if worker_id is not None:
            cond += " AND lease_owner = :worker"
            params["worker"] = worker_id[:64]
        await s.execute(sa_text(
            "UPDATE agent_runs SET recovery_state = CAST(:cp AS jsonb), version = version + 1, "
            "updated_at = now() " + cond), params)


async def cancel_background(user_id: UUID, run_id: UUID) -> None:
    """User/owner cancellation — terminal 'cancelled', lease cleared. The claim excludes it thereafter; a
    worker mid-advance sees status=cancelled on its next read and stops."""
    async with user_session(user_id) as s:
        res = await s.execute(sa_text(
            "UPDATE agent_runs SET status = 'cancelled', completed_at = now(), lease_owner = NULL, "
            "lease_expires_at = NULL, version = version + 1, updated_at = now() "
            "WHERE id = :id AND user_id = :uid AND status NOT IN ('completed','failed','cancelled','dead_letter')"),
            {"id": str(run_id), "uid": str(user_id)})
        if res.rowcount:                                    # only log a transition that actually happened
            s.add(schema.AgentRunEvent(user_id=user_id, agent_run_id=run_id, status="cancelled", detail={}))
            await s.flush()
