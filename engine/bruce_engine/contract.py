"""The client-facing mission contract: machine state, display state, and SERVER-AUTHORIZED actions.

WHY THIS FILE EXISTS — one rule:

    The client must NEVER infer what it is allowed to do from a phase string.

If iOS decides "phase == awaiting_approval, so show Approve", then the set of permitted actions
lives in the app — in two places, drifting, and unenforceable. A stale client would offer Approve
on a mission that already executed, and the tap would race a real external action. Instead the
SERVER returns `available_actions` and the server enforces them. The client renders what it is
told. That is what makes automation policy server-authoritative rather than a UI convention.

Machine state vs display state, also deliberate:
  * `state`  — a stable enum the client switches on. Renaming a display string must never break
    a client.
  * `display_state` — one concise human line ("Adding to Calendar"). No chain-of-thought, no fake
    progress percentages, no invented certainty.

Nothing here executes anything. It computes what is TRUE about a mission and what may be done next.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class MachineState(str, Enum):
    """Canonical mission states. The client switches on these; they are not display copy."""

    understanding = "understanding"        # reading what the student handed over
    preparing = "preparing"                # building the proposal (dates/tasks/events)
    awaiting_approval = "awaiting_approval"  # STOPPED. a human decision is genuinely required
    executing = "executing"                # calling the external service
    waiting_external = "waiting_external"  # external service has it; we are waiting
    verifying = "verifying"                # reading the result BACK to prove it
    succeeded = "succeeded"                # verified by read-back. never set by a write alone
    blocked = "blocked"                    # cannot proceed without something (provider/integration)
    failed = "failed"                      # tried and did not work
    cancelled = "cancelled"                # the student stopped it


# Genuinely terminal: nothing further will happen without a NEW mission.
# `failed` and `blocked` are deliberately NOT here — both are retryable, and a student can still
# give up on them (cancel). Calling them terminal would mean offering no way out of a mission that
# failed for a transient reason, which is how a stuck Decisions queue happens.
TERMINAL_STATES = {MachineState.succeeded, MachineState.cancelled}
# States where Bruce has stopped and the student can act to move it forward.
RECOVERABLE_STATES = {MachineState.blocked, MachineState.failed}


class Action(str, Enum):
    """Everything a client may ask for. The SERVER decides which are currently permitted."""

    review_dates = "review_dates"
    approve_calendar_action = "approve_calendar_action"
    edit_calendar_action = "edit_calendar_action"
    decline_action = "decline_action"
    retry = "retry"
    open_source = "open_source"
    undo = "undo"
    cancel_mission = "cancel_mission"


# Display strings. One concise, observable line per state — what Bruce is DOING, not what it is
# "thinking". Deliberately boring: a status a student can trust beats a status that sounds clever.
DISPLAY: dict[MachineState, str] = {
    MachineState.understanding: "Understanding what you sent",
    MachineState.preparing: "Finding the dates and requirements",
    MachineState.awaiting_approval: "One decision needed",
    MachineState.executing: "Adding to Calendar",
    MachineState.waiting_external: "Waiting on Calendar",
    MachineState.verifying: "Checking it really saved",
    MachineState.succeeded: "Added and verified",
    MachineState.blocked: "Can't continue yet",
    MachineState.failed: "Didn't work",
    MachineState.cancelled: "Cancelled",
}


class VerificationState(str, Enum):
    """Was the external result PROVEN? Separate from mission state on purpose: a mission can be
    'succeeded' only because this is `verified`, and a receipt must never be shown without it."""

    not_applicable = "not_applicable"   # nothing external was executed
    pending = "pending"                 # executed, not yet read back
    verified = "verified"               # read back and matched what was approved
    mismatch = "mismatch"               # read back and did NOT match — never show success
    unverified = "unverified"           # executed but could not be read back — NOT success
    reversed = "reversed"               # undone, and absence confirmed by read-back


class ObjectRefs(BaseModel):
    """Stable ids for every canonical object this mission touches.

    The client uses these to navigate to the SAME canonical page from anywhere — tapping a date on
    Home and in Dates must open one page, not two similar ones.
    """

    mission_id: str
    source_id: str | None = None
    span_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    decision_id: str | None = None
    calendar_proposal_ids: list[str] = Field(default_factory=list)
    external_event_id: str | None = None
    receipt_id: str | None = None


class MissionContract(BaseModel):
    """What the server tells a client about a mission. The client renders this; it decides nothing."""

    state: MachineState
    display_state: str
    updated_at: str
    available_actions: list[Action]
    requires_approval: bool
    verification: VerificationState
    blocking_reason: str | None = None
    refs: ObjectRefs


def available_actions(
    state: MachineState,
    *,
    verification: VerificationState = VerificationState.not_applicable,
    has_source: bool = False,
    has_pending_decision: bool = False,
) -> list[Action]:
    """The SERVER's answer to "what may this client do right now?".

    Deliberate choices worth naming:
      * `undo` is offered ONLY when verification is `verified`. Offering undo on an unverified or
        mismatched execution would invite a student to "undo" something we cannot prove exists —
        and the delete could then hit the wrong state.
      * `approve` appears ONLY in awaiting_approval. A stale client showing Approve after execution
        would race a real external action; the server simply never authorizes it.
      * `retry` is never offered on `succeeded` — retrying a verified external action is how you
        double-book a student.
      * terminal states offer no `cancel`: there is nothing to stop.
    """
    if state == MachineState.awaiting_approval:
        acts = [Action.approve_calendar_action, Action.edit_calendar_action, Action.decline_action,
                Action.review_dates, Action.cancel_mission]
    elif state in (MachineState.understanding, MachineState.preparing):
        acts = [Action.cancel_mission]
    elif state in (MachineState.executing, MachineState.waiting_external, MachineState.verifying):
        # No cancel mid-execution: the external call is already in flight, and "cancelled" would be
        # a lie about a state we do not control. Undo after verification is the honest path.
        acts = []
    elif state == MachineState.succeeded:
        acts = [Action.undo] if verification == VerificationState.verified else []
    elif state == MachineState.blocked:
        acts = [Action.retry, Action.cancel_mission]
    elif state == MachineState.failed:
        acts = [Action.retry, Action.cancel_mission]
    else:  # cancelled
        acts = []

    if has_source and Action.open_source not in acts:
        acts.append(Action.open_source)
    if has_pending_decision and state == MachineState.awaiting_approval:
        pass  # already included above
    return acts


def build_contract(
    *,
    state: MachineState,
    updated_at: str,
    refs: ObjectRefs,
    verification: VerificationState = VerificationState.not_applicable,
    blocking_reason: str | None = None,
) -> MissionContract:
    return MissionContract(
        state=state,
        display_state=DISPLAY[state],
        updated_at=updated_at,
        available_actions=available_actions(
            state,
            verification=verification,
            has_source=refs.source_id is not None,
            has_pending_decision=state == MachineState.awaiting_approval,
        ),
        requires_approval=state == MachineState.awaiting_approval,
        verification=verification,
        blocking_reason=blocking_reason,
        refs=refs,
    )
