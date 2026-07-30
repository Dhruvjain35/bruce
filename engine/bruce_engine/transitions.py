"""The backend-enforced state machine — the model PROPOSES a next state, this module DECIDES it.

WHAT GOES WRONG WITHOUT IT — a real transcript, not a hypothetical. A student asks Bruce to email one
named address a thank-you note. Bruce drafts it. On the next turn it asks who the recipient is (given
one turn earlier) and what the subject line should be; then asks what the message should say (stated
three turns before that); then says "i can't send messages for you" while `tool_broker` is reporting
`gmail.send_message` ok=True. Twenty-two turns produced zero missions and zero agent_runs.

That reads as four bugs. It is one: the MODEL was holding the state. It decided every turn whether a
run existed, what was still missing, and whether it had already asked — by re-reading recent chat text
with no memory of the turn before. Asked "what state are we in", a language model answers fluently at
a rate unrelated to whether the answer is correct, and the wrong answers arrive with exactly the same
confidence as the right ones, so nothing downstream can separate them.

So state does not live in the model. The model may propose a target; `propose_transition` accepts or
rejects it against an explicit edge table plus guards over evidence the BACKEND gathered itself. Three
guards carry the weight:

    executing          needs a real capability id, a broker that says this user can call it, every
                       required argument filled, and a live authorization. Bruce may not begin an
                       action it cannot finish, or one nobody agreed to.
    succeeded          is reachable ONLY from `verifying`, and only with an independent read-back. A
                       write that returned 200 is not success. This is the rule that stops Bruce
                       claiming it sent something it did not send.
    awaiting_approval  needs a decision to actually exist, so "one decision needed" is never a state
                       with nothing behind it.

A rejection is not prose. It carries a typed reason plus `missing_slots` — the NAMES of what is
missing — because the clarifying question is generated from that. A question generated from state
cannot ask for the recipient a second time.

PURE ON PURPOSE: contract.py and the standard library. No DB, no broker, no provider, no IO. Every
edge and every guard is therefore exhaustively exercisable without a fixture or a network, which is
the only way an edge table stays trustworthy while it grows.

SCOPE: this speaks `contract.MachineState`, the vocabulary `agent_runs.status` already uses. The
background-mission LEASE vocabulary (queued / running / completed / dead_letter) is deliberately
disjoint from it — see `agent_run_store` — and is not this module's business.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .contract import TERMINAL_STATES, MachineState

# --- rejection reasons ---------------------------------------------------------------------------------
# Typed and closed, like `authorization_evidence`'s denials: a refused transition has to be countable in
# the evaluation and explainable to a human without reading this file, and neither works with prose.

ALLOW = "allow"
NOT_AN_EDGE = "not_an_edge"                                    # the table has no such move
TERMINAL_STATE = "terminal_state"                              # the run is over; nothing may move it
MISSING_SLOTS = "missing_slots"                                # required arguments are not filled in
NO_AUTHORIZATION = "no_live_authorization"                     # nobody has said yes to THIS operation
CAPABILITY_UNAVAILABLE = "capability_unavailable"              # not live / disconnected / unscoped
CAPABILITY_NOT_AN_OPERATION_ID = "capability_not_an_operation_id"   # free text, not a registry id
NO_DECISION = "no_decision"                                    # approval state with no decision behind it
UNVERIFIED = "unverified_read_back"                            # executed, never proven
NOT_FROM_VERIFYING = "not_from_verifying"                      # success claimed without verification

REASONS: frozenset[str] = frozenset({
    ALLOW, NOT_AN_EDGE, TERMINAL_STATE, MISSING_SLOTS, NO_AUTHORIZATION, CAPABILITY_UNAVAILABLE,
    CAPABILITY_NOT_AN_OPERATION_ID, NO_DECISION, UNVERIFIED, NOT_FROM_VERIFYING,
})

# --- pseudo-slot names ---------------------------------------------------------------------------------
# `missing_slots` normally names arguments ("to", "subject"). When what is missing is evidence rather
# than an argument, it is named here instead of described, so the layer that writes the next question
# maps a name to a question rather than pattern-matching a sentence.

CAPABILITY = "capability"
AUTHORIZATION = "authorization"
DECISION_ID = "decision_id"
VERIFIED_READ_BACK = "verified_read_back"
READ_BACK_ENTITY_ID = "read_back_entity_id"


# --- the edge table ------------------------------------------------------------------------------------
# TOTAL: every MachineState is a key, so a state can never be "unconstrained by omission". The notable
# exclusions are the substance:
#
#   * no self-edges. "Still executing" is not a transition; only an actual state change is proposed
#     here, and a retry has to pass back through `failed` where the attempt is counted.
#   * `succeeded` appears in exactly ONE set — `verifying`'s. Grep it: that is the claim.
#   * nothing reaches `cancelled` from executing / waiting_external / verifying. The external call is
#     already in flight, and "cancelled" would be a statement about a system we do not control. This
#     matches `contract.available_actions`, which offers no cancel there either; a test pins the two
#     together so they cannot drift.
#   * `understanding` cannot jump to `awaiting_approval`. Approval is approval OF something, and the
#     proposal is built in `preparing`.
#   * `blocked` and `failed` keep real ways out — a mission that fails for a transient reason must not
#     become a permanent resident of the Decisions queue.

ALLOWED: dict[MachineState, frozenset[MachineState]] = {
    MachineState.understanding: frozenset({
        MachineState.preparing, MachineState.blocked, MachineState.failed, MachineState.cancelled}),
    MachineState.preparing: frozenset({
        # straight to executing is the direct_action path: one known op, low risk, no decision needed.
        MachineState.awaiting_approval, MachineState.executing, MachineState.blocked,
        MachineState.failed, MachineState.cancelled}),
    MachineState.awaiting_approval: frozenset({
        # back to preparing is an EDIT ("no, friday"); cancelled is a decline.
        MachineState.preparing, MachineState.executing, MachineState.blocked, MachineState.failed,
        MachineState.cancelled}),
    MachineState.executing: frozenset({
        # blocked is reachable because scope/connection failures surface at call time, and they are
        # recoverable by the student rather than a dead end.
        MachineState.waiting_external, MachineState.verifying, MachineState.blocked,
        MachineState.failed}),
    MachineState.waiting_external: frozenset({
        MachineState.verifying, MachineState.blocked, MachineState.failed}),
    MachineState.verifying: frozenset({
        # a read-back that mismatched is a FAILURE, not a success with a footnote; one that could not be
        # performed yet is blocked and retryable.
        MachineState.succeeded, MachineState.blocked, MachineState.failed}),
    MachineState.succeeded: frozenset(),
    MachineState.cancelled: frozenset(),
    MachineState.blocked: frozenset({
        MachineState.understanding, MachineState.preparing, MachineState.awaiting_approval,
        MachineState.executing, MachineState.failed, MachineState.cancelled}),
    MachineState.failed: frozenset({
        # a retry of a destructive operation can need its own approval again — hence awaiting_approval.
        MachineState.understanding, MachineState.preparing, MachineState.awaiting_approval,
        MachineState.executing, MachineState.cancelled}),
}


def legal_targets(current: MachineState) -> frozenset[MachineState]:
    """The edges out of `current`, before any guard. Empty for terminal states."""
    return ALLOWED.get(current, frozenset())


def is_operation_id(capability: str | None) -> bool:
    """Whether `capability` is SHAPED like a registry capability id ("gmail.send_message").

    The transcript's second root cause: the model emitted its required capability as free text —
    "sending messages" — which joins to no row in `tool_registry`, so nothing could ever be selected
    and the runtime fell back to announcing it had no such ability. This module cannot import the
    registry without becoming impure, but it can refuse the shape: `domain.operation`, both halves
    valid identifiers. Free text never survives that; a real id always does.

    Shape is necessary, not sufficient — whether the id EXISTS and is callable is `capability_available`,
    which the caller resolves through the broker.
    """
    if not capability:
        return False
    domain, dot, operation = capability.partition(".")
    return bool(dot) and domain.isidentifier() and operation.isidentifier()


def slots_filled(arguments: dict) -> tuple[str, ...]:
    """The NAMES of the arguments that actually carry a value, for building a GuardContext.

    Blank counts as absent: `{"subject": ""}` is not a subject, and treating it as one is how a student
    gets an email with an empty subject line instead of a question. Returns names only and never values,
    so a rejection built from it is safe to log and safe to hand to a model.
    """
    return tuple(k for k, v in arguments.items()
                 if v is not None and not (isinstance(v, str) and not v.strip()))


@dataclass(frozen=True)
class GuardContext:
    """Everything the guards need, gathered by the backend and handed in.

    Injected rather than fetched: a state machine that opens a database connection cannot be exercised
    exhaustively, and one that is not exercised exhaustively is a table of edges nobody has checked. The
    caller resolves each field from the module that owns it — `capability_available` / `availability_status`
    from `tool_broker.availability`, `authorization_open` from `execution_gate.evaluate`, the read-back
    fields from `ToolResult.verified` / `provider_entity_id` — and this module does no IO at all.
    """

    capability: str | None = None                # the registry id being executed, e.g. "gmail.send_message"
    capability_available: bool = False            # tool_broker.availability(...).ok for THIS user
    availability_status: str = ""                 # the broker's typed status, carried for the reply
    required_slots: tuple[str, ...] = ()          # required argument names, from the ToolSpec arg_schema
    filled_slots: tuple[str, ...] = ()            # the ones actually resolved — see `slots_filled`
    authorization_open: bool = False              # a live authorization covers this exact operation
    decision_id: str | None = None                # the pending decision, when one is required
    verified_read_back: bool = False              # an INDEPENDENT read-back ran and matched
    read_back_entity_id: str | None = None        # what the read-back found (provider entity id)

    def missing(self) -> tuple[str, ...]:
        """Required slots with nothing in them, in the order they were declared (stable questions)."""
        have = set(self.filled_slots)
        return tuple(s for s in self.required_slots if s not in have)


@dataclass(frozen=True)
class TransitionResult:
    """The decision. `reason` is one of the typed constants above; `missing_slots` names what is
    missing so the next question is generated from state instead of from a guess."""

    ok: bool
    reason: str
    missing_slots: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        return not self.ok


def _guard_executing(current: MachineState, ctx: GuardContext) -> TransitionResult:
    """Nothing external starts until Bruce can name the operation, reach it, fill it, and is allowed it.

    The ORDER is deliberate, and it is the transcript's first failure written as code. Availability is
    checked before arguments, because collecting a recipient and a subject line and only then admitting
    the account is not connected is precisely the exchange that made Bruce feel broken. Authorization is
    checked last, because a student approves a fully specified action, not a sketch of one.
    """
    if not is_operation_id(ctx.capability):
        # "sending messages" gets its own reason rather than dissolving into "unavailable": one means the
        # model invented a capability, the other means a real one is out of reach. Different bugs,
        # different fixes, and the runtime should be able to count them separately.
        return TransitionResult(False, CAPABILITY_NOT_AN_OPERATION_ID, (CAPABILITY,))
    if not ctx.capability_available:
        return TransitionResult(False, CAPABILITY_UNAVAILABLE, (CAPABILITY,))
    missing = ctx.missing()
    if missing:
        return TransitionResult(False, MISSING_SLOTS, missing)
    if not ctx.authorization_open:
        return TransitionResult(False, NO_AUTHORIZATION, (AUTHORIZATION,))
    return TransitionResult(True, ALLOW)


def _guard_succeeded(current: MachineState, ctx: GuardContext) -> TransitionResult:
    """Success is a claim about the outside world, so it needs evidence from the outside world.

    The `current is verifying` check duplicates the edge table on purpose. The table is data, and data
    gets edited by someone in a hurry who needs a run to finish; this second, independent check means a
    future edge into `succeeded` from anywhere else still cannot produce an unverified success.
    """
    if current is not MachineState.verifying:
        return TransitionResult(False, NOT_FROM_VERIFYING)
    missing: tuple[str, ...] = ()
    if not ctx.verified_read_back:
        missing += (VERIFIED_READ_BACK,)
    if not (ctx.read_back_entity_id or "").strip():
        # A `verified` flag with nothing read back is somebody's opinion. The entity id is the thing that
        # came back from the provider, and it is what a receipt and an undo are later built from.
        missing += (READ_BACK_ENTITY_ID,)
    if missing:
        return TransitionResult(False, UNVERIFIED, missing)
    return TransitionResult(True, ALLOW)


def _guard_awaiting_approval(current: MachineState, ctx: GuardContext) -> TransitionResult:
    """Stopping to ask requires something to ask about. A run parked in awaiting_approval with no
    decision row is a mission that will never move again and a Decisions queue that shows nothing."""
    if not (ctx.decision_id or "").strip():
        return TransitionResult(False, NO_DECISION, (DECISION_ID,))
    return TransitionResult(True, ALLOW)


_GUARDS: dict[MachineState, Callable[[MachineState, GuardContext], TransitionResult]] = {
    MachineState.executing: _guard_executing,
    MachineState.succeeded: _guard_succeeded,
    MachineState.awaiting_approval: _guard_awaiting_approval,
}


def propose_transition(current: MachineState, target: MachineState, *,
                       guard_ctx: GuardContext) -> TransitionResult:
    """Decide whether a run may move from `current` to `target`. The proposal is untrusted input.

    `guard_ctx` is required rather than defaulted: every call site has to state what evidence it holds,
    and an empty context is a deliberate claim ("I hold none"), never an accident of a missing argument.
    """
    if current in TERMINAL_STATES:
        # Distinct from NOT_AN_EDGE: something tried to move a finished run, which is a different defect
        # from proposing an illegal move, and the runtime should not have to guess which happened.
        return TransitionResult(False, TERMINAL_STATE)
    if target not in legal_targets(current):
        return TransitionResult(False, NOT_AN_EDGE)
    guard = _GUARDS.get(target)
    if guard is None:
        return TransitionResult(True, ALLOW)
    return guard(current, guard_ctx)
