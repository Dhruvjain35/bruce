"""Calendar CapabilityExecutor (G0.4) — the calendar domain's plug into the general AgentRun loop.

It builds a NextAction for a mutation and, on execute, delegates to the ALREADY-VERIFIED calendar_tools
(update_event / delete_event) — which do the provider write + independent read-back + _matches and return
a ToolResult. The executor reimplements NONE of that verification, so the live-verified behavior is
unchanged; the loop just gains a durable run + the frozen contracts around the same trusted I/O. Adding
create/search later is another executor here, never a new branch in the runtime.
"""

from __future__ import annotations

from uuid import UUID

from . import calendar_tools
from .runtime_contracts import ActionType, NextAction, Risk, ToolResult

_PROVIDER = "google_calendar"


class CalendarMutationExecutor:
    """Drives a delete OR an update/repair on one resolved calendar entity through the verified tools."""

    domain = "calendar"

    def __init__(self, kind: str, entity: dict, *, new_start: str | None = None,
                 new_end: str | None = None, new_timezone: str | None = None, adapter=None) -> None:
        if kind not in ("delete", "update", "repair"):
            raise ValueError(f"unsupported mutation kind: {kind}")
        self.kind = kind
        self.entity = entity
        self.new_start = new_start
        self.new_end = new_end
        self.new_timezone = new_timezone
        self._adapter = adapter
        self._is_delete = kind == "delete"
        self.capability = "calendar.delete_event" if self._is_delete else "calendar.update_event"
        self._operation = "delete_event" if self._is_delete else "update_event"

    def goal(self) -> dict:
        # provider-neutral summary the AgentRun/world model can read back — title + what changed, no secrets
        g: dict = {"action": self.kind, "entity_id": self.entity.get("id"),
                   "title": self.entity.get("title"), "desired_outcome": f"{self.kind} {self.entity.get('title', 'event')}"}
        if not self._is_delete:
            g["new_start"], g["new_end"] = self.new_start, self.new_end
        return g

    def build_action(self) -> NextAction:
        args: dict = {} if self._is_delete else {
            "new_start": self.new_start, "new_end": self.new_end, "new_timezone": self.new_timezone}
        return NextAction(
            type=ActionType.call_tool, capability=self.capability, provider=_PROVIDER,
            operation=self._operation, target_entity_id=str(self.entity.get("id")) if self.entity.get("id") else None,
            arguments=args, verification_method="provider_readback",
            risk=Risk.medium if not self._is_delete else Risk.high, reversible=not self._is_delete)

    async def execute(self, user_id: UUID) -> ToolResult:
        if self._is_delete:
            return await calendar_tools.delete_event(user_id, self.entity, adapter=self._adapter)
        return await calendar_tools.update_event(
            user_id, self.entity, new_start=self.new_start, new_end=self.new_end,
            new_timezone=self.new_timezone, adapter=self._adapter)
