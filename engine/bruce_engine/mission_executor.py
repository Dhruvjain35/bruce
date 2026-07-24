"""Mission step executor (Phase E) — turns a planned NextAction into a REAL, verified provider call, so the
background runner genuinely ADVANCES a mission (not a no-op). It dispatches by capability to the SAME verified
tools the foreground uses (calendar_tools today; Gmail at Phase G) — provider-neutral: add a hand by adding a
branch, never a bespoke runner path. Verification (ToolResult.verified) is the tool's, not fabricated here.
"""

from __future__ import annotations

from uuid import UUID

from . import calendar_tools, entity_store
from .runtime_contracts import ActionType, NextAction, ToolOutcome, ToolResult


def action_from_dict(d: dict) -> NextAction:
    """Reconstruct a NextAction from its persisted checkpoint dict (as agent_loop._action_dict wrote it)."""
    return NextAction(
        type=ActionType.call_tool, capability=d.get("capability"), provider=d.get("provider"),
        operation=d.get("operation"), target_entity_id=d.get("target_entity_id"),
        arguments=dict(d.get("arguments") or {}))


class MissionExecutor:
    """A StepExecutor over NextActions — execute() performs the action's verified provider call. `adapter`
    is an injection seam (a fake provider in tests)."""

    def __init__(self, *, adapter=None) -> None:
        self._adapter = adapter

    async def execute(self, user_id: UUID, action: NextAction, *, idempotency_key: str | None = None) -> ToolResult:
        # idempotency_key is the runner's exactly-once contract for NON-idempotent hands (Gmail send). Calendar
        # ops are already idempotent by read-back, so they ignore it; a Gmail executor will pass it through.
        cap = action.capability or ""
        if cap in ("calendar.update_event", "calendar.delete_event"):
            eid = action.target_entity_id
            entity = await entity_store.get_entity(user_id, UUID(eid)) if eid else None
            if entity is None:
                return ToolResult(ToolOutcome.not_found, cap, "google_calendar", action.operation or "",
                                  reason="target entity not found")
            if cap == "calendar.delete_event":
                return await calendar_tools.delete_event(user_id, entity, adapter=self._adapter)
            a = action.arguments or {}
            return await calendar_tools.update_event(
                user_id, entity, new_start=a.get("new_start"), new_end=a.get("new_end"),
                new_timezone=a.get("new_timezone"), adapter=self._adapter)
        # No other capability is background-executable yet (Gmail lands at Phase G).
        return ToolResult(ToolOutcome.provider_error, cap, action.provider or "", action.operation or "",
                          reason="capability not background-executable yet")
