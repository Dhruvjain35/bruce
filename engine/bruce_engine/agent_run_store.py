"""AgentRun persistence (R2) — durable working state for the general runtime, surviving messages /
restarts / retries / corrections. Holds GoalSpec + TemporalSpec + selected entity + current NextAction +
last tool result + active decision, so execution state is read from HERE, never reconstructed from recent
chat. tenant_or_worker RLS (a resuming worker writes it). Idempotent create on (owner, idempotency_key).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import schema
from .db import user_session

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
                     status: str = "understanding") -> dict:
    """Create (or reference, if idempotency_key already exists) the run + its first event, atomically."""
    async with user_session(user_id) as s:
        if idempotency_key:
            ex = (await s.execute(select(schema.AgentRun).where(
                schema.AgentRun.user_id == user_id,
                schema.AgentRun.idempotency_key == idempotency_key))).scalar_one_or_none()
            if ex is not None:
                return _to_dict(ex)
        run = schema.AgentRun(user_id=user_id, domain=domain, status=status, goal=goal or {},
                              mission_id=mission_id, idempotency_key=idempotency_key)
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
