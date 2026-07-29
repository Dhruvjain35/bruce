"""AgentRun persistence (R2) — durable working state for the general runtime, surviving messages /
restarts / retries / corrections. Holds GoalSpec + TemporalSpec + selected entity + current NextAction +
last tool result + active decision, so execution state is read from HERE, never reconstructed from recent
chat. tenant_or_worker RLS (a resuming worker writes it). Idempotent create on (owner, idempotency_key).

STATUS IS NO LONGER AN ASSIGNMENT. Every write to `agent_runs.status` now goes through ONE choke point,
`enforce_status_write`, and a refused move raises `IllegalStatusWrite` carrying the typed reason plus the
NAMES of what is missing. That is the second half of the fix `transitions.py` describes: the state machine
is worthless if any caller can still write `r.status = "succeeded"` and skip it. Before this, `update_run`
copied whatever string it was handed straight onto the column and `complete_background` interpolated one
into raw SQL, so the edge table was advisory. It is now the only way in.

The rules themselves are NOT restated here. `transitions.propose_transition` owns them — including the
one that matters most, that `succeeded` is reachable only from `verifying` and only with an independent
read-back. This module supplies the evidence and raises on refusal; it decides nothing.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from . import goal_slots, schema, transitions
from .contract import TERMINAL_STATES, MachineState
from .db import user_session, worker_session

_MUTABLE = {"status", "goal", "temporal", "selected_entity_id", "selected_provider_account",
            "current_action", "last_tool_result", "verification_result", "active_decision",
            "recovery_state", "blocked_reason", "completed_at"}


# --- the status gate -----------------------------------------------------------------------------------
# TWO VOCABULARIES SHARE THIS COLUMN, and pretending otherwise is what makes an "obvious" unification
# unsafe. `contract.MachineState` is the mission machine the client renders and `transitions.py` governs.
# The background-mission LEASE words (queued / running / completed / dead_letter) are the scheduler's own
# lifecycle, which `transitions.py`'s docstring explicitly hands to this module ("is not this module's
# business"). They are NOT synonyms of machine states, and the tempting mapping is the dangerous one:
#
#     completed IS NOT succeeded.
#
# `succeeded` is a claim about the outside world that requires a read-back. `completed` means the worker
# finished its turn on this row. Aliasing them would let `complete_background(status="completed")` — a
# write, nothing more — land in the one state this whole subsystem exists to protect.
#
# So the gate resolves a pair into a lane rather than flattening the two:
#
#   1. terminal source, or a word in neither vocabulary -> refused outright, before anything else. The
#      second is the same answer the CHECK constraint in migration 0031 gives the database, so code and
#      schema agree by construction rather than by review.
#   2. both ends are MachineState -> `transitions.propose_transition` decides. Authoritative, guards run.
#   3. it refused -> the operational table below may ADD that exact pair, and may do nothing else. What
#      keeps that safe is not the refusal reason but WHAT A SUPPLEMENT IS ALLOWED TO CONTAIN:
#      `check_operational_lane` forbids `succeeded` as a target and forbids a terminal as a source, and
#      step 1 already settled terminals and unknown words. So the two rules this subsystem exists for are
#      structurally out of reach, and a supplement can only ever waive an ADMISSION guard for a lane whose
#      write records an attempt rather than being one.

LEASE_STATES: frozenset[str] = frozenset({"queued", "running", "completed", "dead_letter"})
STATUS_VOCABULARY: frozenset[str] = frozenset(s.value for s in MachineState) | LEASE_STATES

# Terminal in the store = nothing may move it afterwards. The machine's own terminals plus the lease
# lane's two ends. `failed` is deliberately absent from both: a mission that failed transiently must keep
# a way back, which is `transitions.ALLOWED[failed]`.
TERMINAL_STATUSES: frozenset[str] = frozenset(s.value for s in TERMINAL_STATES) | {"completed", "dead_letter"}

UNKNOWN_STATUS = "unknown_status"          # a word in neither vocabulary; the CHECK constraint agrees

_OPERATIONAL: dict[str, frozenset[str]] = {
    # THE DIRECT-ACTION LANE (`agent_loop.run_direct_action`). A Tier-0 run is created in `understanding`
    # and its deliberation is conversational, never persisted — there is no `preparing` row to move
    # through, because the executor was fully built before the run existed. Its verified terminal is
    # `completed`, not `succeeded`, and `agent_loop._status_for` only reaches it when `tr.verified` is
    # true. `test_agent_run_pg.test_create_resume_update` pins this lifecycle; it encodes a real product
    # path (send this email now) rather than the transcript's bug, so the edges are declared, not deleted.
    "understanding": frozenset({"executing"}),
    "executing": frozenset({"completed"}),
    # THE BACKGROUND LEASE LANE. `running -> running` is not a self-edge in the machine sense: it is a
    # DIFFERENT worker reclaiming a run whose lease expired, which is the crash recovery the claim exists
    # for. `running -> cancelled` is allowed where `executing -> cancelled` is not, and the difference is
    # real: a background mission is stopped cooperatively (the worker sees `cancelled` on its next read
    # and stops) rather than by claiming something about a provider call already in flight.
    "queued": frozenset({"running", "cancelled", "failed", "dead_letter"}),
    "running": frozenset({"running", "queued", "completed", "failed", "dead_letter", "cancelled"}),
}


class IllegalStatusWrite(RuntimeError):
    """A refused status write, carried as data rather than as a log line.

    Callers need the machine-usable parts — which move was refused, WHY in `transitions`' closed reason
    vocabulary, and the NAMES of what is missing — because the next question Bruce asks is generated from
    exactly those. A rejection that only exists as a formatted string is how the transcript ended up
    asking for a recipient that had already been given.
    """

    def __init__(self, current: str, target: str, reason: str, missing_slots: tuple[str, ...] = ()) -> None:
        super().__init__(f"illegal status write {current} -> {target}: {reason}")
        self.current = current
        self.target = target
        self.reason = reason
        self.missing_slots = missing_slots


def _machine(status: str) -> MachineState | None:
    try:
        return MachineState(status)
    except ValueError:
        return None


def check_operational_lane(edges: dict[str, frozenset[str]], name: str) -> None:
    """The three invariants every operational supplement must satisfy, checked once at import.

    A supplement exists to ADD edges the machine table does not have, for a lane `transitions.py` declared
    out of its own scope. Anything beyond that would make it a second, competing rule set — so it may not
    name an unknown word, may not start from a finished run, and may not lead to `succeeded`. The third is
    the one worth the assertion: `succeeded` has exactly one legal route in, and this keeps it that way no
    matter who edits a table in a hurry.
    """
    assert all(s in STATUS_VOCABULARY for s in edges), f"{name}: unknown source status"
    assert all(t in STATUS_VOCABULARY for ts in edges.values() for t in ts), f"{name}: unknown target status"
    assert MachineState.succeeded.value not in {t for ts in edges.values() for t in ts}, \
        f"{name}: may never be a route into `succeeded` — that is `verifying` + a read-back, only"
    assert not (set(edges) & TERMINAL_STATUSES), f"{name}: a terminal status may not be a source"


def authorize_status_write(current: str, target: str, *,
                           guard_ctx: transitions.GuardContext | None = None,
                           extra_edges: dict[str, frozenset[str]] | None = None) -> transitions.TransitionResult:
    """Decide one status write. Pure: no IO, no session, no side effect — the caller raises or proceeds.

    `guard_ctx` is what the caller can prove. It is separate from the edge because the two fail for
    different reasons and need different repairs: NOT_AN_EDGE is a control-flow defect, MISSING_SLOTS is
    a question to ask the student.

    `extra_edges` lets a second lane over the same vocabulary (the mission PHASE log — see
    `mission_kernel`) declare its own additions without copying this function. It is held to the same
    `check_operational_lane` invariants as `_OPERATIONAL`, which is what makes it safe to consult.
    """
    if current == target:
        # Not a transition. `update_run` never reaches here (it compares first); a caller that does is
        # asking whether standing still is legal, and it is — nothing changed.
        return transitions.TransitionResult(True, transitions.ALLOW)
    if current not in STATUS_VOCABULARY or target not in STATUS_VOCABULARY:
        return transitions.TransitionResult(False, UNKNOWN_STATUS)
    if current in TERMINAL_STATUSES:
        return transitions.TransitionResult(False, transitions.TERMINAL_STATE)

    cur_ms, tgt_ms = _machine(current), _machine(target)
    verdict: transitions.TransitionResult | None = None
    if cur_ms is not None and tgt_ms is not None:
        verdict = transitions.propose_transition(cur_ms, tgt_ms, guard_ctx=guard_ctx or transitions.GuardContext())
        if verdict.ok:
            return verdict

    # A refusal is reconsidered ONLY against a table that names this exact pair. That reads permissive
    # until you check what a supplement is allowed to contain: `check_operational_lane` forbids
    # `succeeded` as a target and forbids a terminal as a source, and terminal + unknown-word were both
    # settled above. So the two rules this subsystem exists for — success needs a read-back, finished is
    # finished — are structurally out of reach here, and what a supplement can actually waive is the
    # ADMISSION guard on `executing` (capability reachable, arguments filled, authorization open). That
    # waiver is correct for a lane whose write is a RECORD of an attempt rather than the attempt itself;
    # see `mission_kernel._PHASE_LANE`, where the admission decision belongs to `mutation_gateway` one
    # statement later and asking it twice would mean answering it from stale columns.
    for table in (_OPERATIONAL, extra_edges or {}):
        if target in table.get(current, frozenset()):
            return transitions.TransitionResult(True, transitions.ALLOW)
    # The machine's own verdict is the one worth returning — "you have no recipient" is a better answer
    # than "no such edge", and it is what the next question gets written from.
    return verdict or transitions.TransitionResult(False, transitions.NOT_AN_EDGE)


def enforce_status_write(current: str, target: str, *,
                         guard_ctx: transitions.GuardContext | None = None,
                         extra_edges: dict[str, frozenset[str]] | None = None) -> None:
    """`authorize_status_write`, but a refusal raises. This is the only door onto `agent_runs.status`."""
    if _unchecked():
        return
    verdict = authorize_status_write(current, target, guard_ctx=guard_ctx, extra_edges=extra_edges)
    if not verdict.ok:
        raise IllegalStatusWrite(current, target, verdict.reason, verdict.missing_slots)


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _derive_guard_ctx(row: "schema.AgentRun", fields: dict) -> transitions.GuardContext:
    """Build the guard evidence from the RUN'S OWN durable state, plus whatever this same write is setting.

    This is R2's whole premise applied to the guards: the proof that a send really happened is
    `last_tool_result.provider_entity_id`, written by verified I/O, and it is already on the row. Reading
    it from there rather than from a boolean the caller passes means a run cannot be talked into
    `succeeded` by an argument — it has to have the receipt.

    `capability_available` and `authorization_open` are NOT derived. Both are live judgements (the broker's
    view of this user's connection; `execution_gate`'s view of consent right now) and a store that answered
    them from a stale column would be reintroducing the copied-verdict bug `authorization_evidence` exists
    to prevent. A caller that wants to move a run into `executing` through the machine lane passes its own
    `guard_ctx`; nothing in the store guesses on its behalf.
    """
    def _pick(name: str) -> dict:
        return _as_dict(fields[name]) if name in fields else _as_dict(getattr(row, name, None))

    action, tool_result = _pick("current_action"), _pick("last_tool_result")
    verification, decision = _pick("verification_result"), _pick("active_decision")
    goal = fields["goal"] if "goal" in fields else getattr(row, "goal", None)

    kind, slots = goal_slots.from_goal_jsonb(_as_dict(goal))
    required = goal_slots.required_slots(kind) if kind is not None else ()
    # Filled slots are named in the SLOT namespace, and derived without raising. Both matter, and getting
    # either wrong is silent:
    #   * `tool_arguments()` RAISES when a required slot is still empty. Deriving the guard from it meant
    #     every ordinary status write on a half-collected goal (understanding -> preparing, -> blocked)
    #     blew up instead of transitioning — the guard exploding on the exact runs it exists to protect.
    #   * `tool_arguments()` also keys by the TOOL's argument names ("to"), while `required_slots` returns
    #     SLOT names ("recipient"). Comparing the two namespaces meant a fully collected email goal still
    #     reported recipient missing, so `executing` was unreachable and no send could ever have run. The
    #     suite did not catch it because nothing exercises that edge yet — which is exactly why it had to
    #     be found by reading rather than by waiting for a green run.
    filled = tuple(
        s.name for s in goal_slots.slot_specs(kind)
        if (sv := (slots or {}).get(s.name)) is not None and sv.filled
    ) if kind is not None else ()

    return transitions.GuardContext(
        capability=action.get("capability") or _as_dict(goal).get("capability"),
        required_slots=required,
        filled_slots=filled,
        decision_id=decision.get("decision_id") or decision.get("id"),
        verified_read_back=bool(tool_result.get("verified") or verification.get("verified")),
        read_back_entity_id=(tool_result.get("provider_entity_id")
                             or verification.get("provider_entity_id")),
    )


# --- the escape hatch ----------------------------------------------------------------------------------
# Deliberately shaped like `execution_gate.unchecked_provider_writes_for_test`, for the same reason: one
# named suspension beats the alternative, which is the next person adding a real bypass because there was
# no sanctioned one. It is physically unreachable outside pytest — not by convention, by a raise on entry.

_UNCHECKED_ENV = "BRUCE_STATUS_GATE_SUSPENDED"


def _unchecked() -> bool:
    return os.environ.get(_UNCHECKED_ENV) == "1"


@contextlib.contextmanager
def unchecked_status_writes_for_test():
    """Suspend the status gate for a test or a backfill that is deliberately planting an arbitrary state.

    A migration backfill and a fixture that needs a run parked in `verifying` are legitimate; teaching
    them to walk the whole edge table would add ceremony to setup code and tempt someone to widen the
    real table instead. So this exists, it is named, and it cannot be reached from a production path:
    without pytest in the environment the first call raises, in every environment, always.
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        raise RuntimeError("unchecked_status_writes_for_test is reachable only under pytest")
    previous = os.environ.get(_UNCHECKED_ENV)
    os.environ[_UNCHECKED_ENV] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(_UNCHECKED_ENV, None)
        else:
            os.environ[_UNCHECKED_ENV] = previous


def _conversation_uuid(value: str | UUID | None) -> UUID | None:
    """Coerce a caller's conversation id to what the COLUMN can hold, or refuse loudly.

    `agent_runs.conversation_id` is a uuid column (migration 0023), while several conversation ids in
    this codebase are channel identities — a handle, not a uuid. Those two facts have to meet somewhere,
    and the only safe place is here, at the write:

      * empty / None means UNATTRIBUTED, which is a real answer (a background mission belongs to no
        conversation) and becomes NULL. `goal_runtime._conversation_of` already reads "" the same way.
      * a non-uuid conversation key RAISES rather than being quietly dropped. Silently NULLing it is the
        exact shape of this whole mission's bug — a caller believes a goal is scoped to one conversation,
        the column says nothing, and the next turn continues somebody else's goal. A caller whose
        conversation key is a handle must keep it in the goal blob, where it is a string.

    The value is never interpolated into the message: it can be a handle, and handles are not logged.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("conversation_id must be a UUID — agent_runs.conversation_id is a uuid column; "
                         "a non-uuid conversation key belongs in the goal blob") from exc


def _to_dict(r: "schema.AgentRun") -> dict:
    return {
        "id": str(r.id), "user_id": str(r.user_id), "domain": r.domain, "status": r.status,
        # The conversation this run belongs to. It used to be dropped here, so a caller holding a run
        # dict could not tell whose thread the goal was — and `goal_runtime` had to keep a second
        # row-to-dict of its own just to see the column. Shaped identically to that one (str or None) so
        # the two agree by construction; "" is never emitted, because unattributed is None.
        "conversation_id": str(r.conversation_id) if r.conversation_id else None,
        "goal": r.goal, "temporal": r.temporal, "selected_entity_id": str(r.selected_entity_id) if r.selected_entity_id else None,
        "selected_provider_account": r.selected_provider_account, "current_action": r.current_action,
        "last_tool_result": r.last_tool_result, "verification_result": r.verification_result,
        "active_decision": r.active_decision, "recovery_state": r.recovery_state,
        "blocked_reason": r.blocked_reason, "mission_id": str(r.mission_id) if r.mission_id else None,
    }


async def create_run(user_id: UUID, *, domain: str = "calendar", conversation_id: str | UUID | None = None,
                     goal: dict | None = None,
                     mission_id: UUID | None = None, idempotency_key: str | None = None,
                     status: str = "understanding", next_run_at: datetime | None = None) -> dict:
    """Create (or reference, if idempotency_key already exists) the run + its first event, atomically.
    `next_run_at` is set in THIS transaction so a scheduled background run is never briefly claimable with
    a NULL next_run_at (which the claim treats as due-now).

    A creation is not a transition — there is no `current` to move from — but the WORD still has to be one
    the machine knows, or the run starts life in a state nothing can legally move it out of and the CHECK
    constraint refuses it a layer later with a much worse error.

    `conversation_id` records WHICH THREAD this run belongs to. The column has existed since migration
    0023 and this parameter did not, so every run ever created through the store had NULL there — which
    is why `goal_runtime` had to write the same attribution into the goal blob as a fallback. A run with
    no conversation is not the same claim as a run whose conversation is unknown, and only the caller
    knows which one it has, so this stays optional and None keeps meaning UNATTRIBUTED.

    On the idempotent path the existing run is REFERENCED, not re-attributed: a second create with the
    same key returns the row as it stands. Reassigning a live goal to whichever conversation asked for it
    last is precisely the cross-thread confusion the column exists to prevent.
    """
    if status not in STATUS_VOCABULARY:
        raise IllegalStatusWrite("", status, UNKNOWN_STATUS)
    conversation_uuid = _conversation_uuid(conversation_id)
    async with user_session(user_id) as s:
        if idempotency_key:
            ex = (await s.execute(select(schema.AgentRun).where(
                schema.AgentRun.user_id == user_id,
                schema.AgentRun.idempotency_key == idempotency_key))).scalar_one_or_none()
            if ex is not None:
                return _to_dict(ex)
        run = schema.AgentRun(user_id=user_id, domain=domain, conversation_id=conversation_uuid,
                              status=status, goal=goal or {},
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


async def latest_active(user_id: UUID, *, domain: str | None = "calendar") -> dict | None:
    """Most recent run not in a terminal state — resumes across messages/restarts.

    `domain=None` means ANY domain. The old signature defaulted to "calendar" with no way to opt out,
    which made every gmail background mission structurally invisible to callers that did not think to
    pass a domain — including the context the conversation model reads. A missing domain is UNKNOWN,
    never Calendar.
    """
    async with user_session(user_id) as s:
        conds = [schema.AgentRun.user_id == user_id]
        if domain is not None:
            conds.append(schema.AgentRun.domain == domain)
        r = (await s.execute(select(schema.AgentRun).where(
            *conds,
            # dead_letter IS terminal — the cancel SQL already treats it so. Omitting it here meant a
            # dead-lettered run read as ACTIVE forever, so the runtime would report in-flight work that
            # nothing will ever advance. `succeeded` had exactly the same hole and it was invisible until
            # the gate made that state reachable: this list was written when `completed` was the only way
            # a run could finish. Derived from TERMINAL_STATUSES now so the next terminal cannot be
            # forgotten. `failed` is added by hand rather than promoted: it is retryable by the machine
            # but nothing is currently advancing it, so it must not be reported as in-flight work.
            schema.AgentRun.status.notin_(tuple(sorted(TERMINAL_STATUSES | {"failed"})))).order_by(
            schema.AgentRun.created_at.desc()).limit(1))).scalar_one_or_none()
        return _to_dict(r) if r is not None else None


async def update_run(user_id: UUID, run_id: UUID, *,
                     guard_ctx: transitions.GuardContext | None = None, **fields) -> None:
    """Patch mutable run fields + append a transition event when the status changes.

    A status change is PROPOSED, not applied. The gate runs inside the same session as the read, so the
    `current` it judges is the row this write is about to modify, and a refusal raises before any field
    lands — a rejected transition must not leave half of a state change behind.

    `guard_ctx` is optional because most writes do not need one: everything the guards can honestly read
    from the row (the read-back receipt, the pending decision, the filled slots) is derived from the run
    itself. Pass one only to assert something the store cannot see — that the broker says this capability
    is reachable, or that an authorization is open right now.
    """
    async with user_session(user_id) as s:
        r = (await s.execute(select(schema.AgentRun).where(
            schema.AgentRun.id == run_id, schema.AgentRun.user_id == user_id))).scalar_one_or_none()
        if r is None:
            return
        status_changed = "status" in fields and fields["status"] != r.status
        if status_changed:
            enforce_status_write(r.status, fields["status"],
                                 guard_ctx=guard_ctx or _derive_guard_ctx(r, fields))
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

check_operational_lane(_OPERATIONAL, "the direct-action + background-lease lane")


async def _status_of(s, user_id: UUID, run_id: UUID) -> str | None:
    """The run's live status inside the caller's OPEN session — so the gate judges the same row the
    UPDATE is about to touch, under the same transaction and the same RLS."""
    return (await s.execute(sa_text("SELECT status FROM agent_runs WHERE id = :id AND user_id = :uid"),
                            {"id": str(run_id), "uid": str(user_id)})).scalar_one_or_none()


# The claim is the one status write that CANNOT read-then-write: `FOR UPDATE SKIP LOCKED` across all
# users is a single atomic statement, and splitting it would reintroduce the double-execution it exists to
# prevent. Its legality is therefore enforced by its own WHERE clause — which only ever selects a `queued`
# run that is due or a `running` run whose lease expired — and pinned to the edge table here, so tightening
# one without the other fails at import instead of silently letting the worker make an illegal move.
assert "running" in _OPERATIONAL["queued"], "the claim moves queued -> running"
assert "running" in _OPERATIONAL["running"], "the claim reclaims an expired lease: running -> running"

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
    it must not clobber the new owner's in-flight state.

    A run that is ALREADY terminal is a no-op rather than an error, and that is a real fix rather than
    leniency: this UPDATE had no status predicate, so a worker finishing a mission the student cancelled
    while it was in flight used to relabel `cancelled` as `completed`. Now the gate stops it, and the
    caller — a runner loop draining a queue — is not asked to handle an exception for a run that is simply
    already over. Any other refusal is a control-flow defect and raises."""
    async with user_session(user_id) as s:
        current = await _status_of(s, user_id, run_id)
        if current is None or current in TERMINAL_STATUSES:
            return
        enforce_status_write(current, status)
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
    (done=False) so the failure budget bounds only CONSECUTIVE failures, never healthy recurring checks.

    Requeueing a finished run is a no-op for the same reason completing one is: the lease loop should not
    have to distinguish "already over" from "not mine any more"."""
    async with user_session(user_id) as s:
        current = await _status_of(s, user_id, run_id)
        if current is None or current in TERMINAL_STATUSES:
            return
        enforce_status_write(current, "queued")
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
    worker mid-advance sees status=cancelled on its next read and stops.

    Cancelling an already-finished run is a no-op, because "stop it" has already been satisfied and asking
    twice is not an error. Cancelling a run that is mid-provider-call IS refused: `transitions.py` offers
    no edge out of `executing` / `waiting_external` / `verifying` into `cancelled`, on the grounds that
    "cancelled" would be a statement about a system we do not control. A background `running` run is
    different and is allowed — see the operational table."""
    async with user_session(user_id) as s:
        current = await _status_of(s, user_id, run_id)
        if current is None or current in TERMINAL_STATUSES:
            return
        enforce_status_write(current, MachineState.cancelled.value)
        res = await s.execute(sa_text(
            "UPDATE agent_runs SET status = 'cancelled', completed_at = now(), lease_owner = NULL, "
            "lease_expires_at = NULL, version = version + 1, updated_at = now() "
            "WHERE id = :id AND user_id = :uid AND status NOT IN ('completed','failed','cancelled','dead_letter')"),
            {"id": str(run_id), "uid": str(user_id)})
        if res.rowcount:                                    # only log a transition that actually happened
            s.add(schema.AgentRunEvent(user_id=user_id, agent_run_id=run_id, status="cancelled", detail={}))
            await s.flush()
