"""Status is enforced at the WRITE, not merely described somewhere.

`transitions.py` is a correct state machine that nothing was obliged to consult. `agent_run_store` copied
whatever string a caller handed it onto `agent_runs.status`, `complete_background` interpolated one into
raw SQL, and `mission_kernel.record_phase` was a bare `m.phase = phase`. So the transcript's twenty-two
turns could have produced a run that said `succeeded` with nothing sent, and the machine would have had
no opportunity to object.

These tests are about the OBLIGATION. Every one of them writes through a real production entry point
against real Postgres — no fixture pokes the column directly — and asserts either that the write was
refused with a machine-usable reason, or that it was allowed for a reason a human can check. Every
absence assertion ("this cannot happen") is paired with the positive control that makes it meaningful:
if the gate were replaced with `return`, the refusal tests fail; if it were replaced with `raise`, the
control tests fail. Neither half passes on its own.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_run_store, mission_kernel, transitions
from bruce_engine.agent_run_store import IllegalStatusWrite
from bruce_engine.contract import MachineState
from bruce_engine.models import MissionPhase
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

# The evidence a run carries when a verified write really happened: the provider handed back an id.
VERIFIED = {"verified": True, "provider_entity_id": "prov-entity-1"}
# The same claim with nothing behind it — `verified` asserted, no id. This is the shape the guard exists
# to catch, and it is why `_guard_succeeded` wants both halves.
UNEVIDENCED = {"verified": True, "provider_entity_id": ""}

EXECUTABLE = transitions.GuardContext(
    capability="gmail.send_message", capability_available=True, authorization_open=True)


@pytest.fixture(autouse=True)
def _pg(pg_test_db, monkeypatch):
    monkeypatch.setattr(db, "create_async_engine",
                        lambda url, **kw: (kw.pop("poolclass", None),
                                           _real_create_async_engine(url, poolclass=NullPool, **kw))[1])
    db._engine = None
    db._sessionmaker = None
    yield
    db._engine = None
    db._sessionmaker = None


def _run(c):
    return asyncio.run(c)


def _user() -> UUID:
    uid = uuid4()
    _run(users.ensure(uid, auth_provider="test"))
    return uid


def _new_run(uid: UUID, **kw) -> UUID:
    return UUID(str(_run(agent_run_store.create_run(uid, **kw))["id"]))


def _set(uid: UUID, rid: UUID, status: str, **fields) -> None:
    _run(agent_run_store.update_run(uid, rid, status=status, **fields))


def _status(uid: UUID, rid: UUID) -> str:
    return _run(agent_run_store.get_run(uid, rid))["status"]


def _to_verifying(uid: UUID, rid: UUID, tool_result: dict) -> None:
    """Walk a run to `verifying` the way the runtime does, carrying `tool_result` as its durable proof."""
    _set(uid, rid, MachineState.preparing.value)
    _set(uid, rid, MachineState.executing.value, guard_ctx=EXECUTABLE)
    _set(uid, rid, MachineState.verifying.value, last_tool_result=tool_result)


# --- the store: refusals -------------------------------------------------------------------------------

def test_an_illegal_status_write_raises_with_a_machine_usable_reason(clean_db):
    """`understanding -> verifying` skips the entire middle of the machine. The refusal has to be data:
    a caller (and the layer that writes the student's next message) needs the reason, not a log line."""
    uid = _user()
    rid = _new_run(uid)

    with pytest.raises(IllegalStatusWrite) as exc:
        _set(uid, rid, MachineState.verifying.value)

    assert exc.value.reason == transitions.NOT_AN_EDGE
    assert (exc.value.current, exc.value.target) == ("understanding", "verifying")
    assert _status(uid, rid) == "understanding"          # and NOTHING was written


def test_a_refused_transition_writes_none_of_the_other_fields(clean_db):
    """A rejected status must not leave half a state change behind. The gate runs before any assignment,
    so the accompanying fields are refused with it."""
    uid = _user()
    rid = _new_run(uid)

    with pytest.raises(IllegalStatusWrite):
        _set(uid, rid, MachineState.verifying.value, selected_provider_account="a@b.com")
    assert _run(agent_run_store.get_run(uid, rid))["selected_provider_account"] is None

    # positive control: the same field lands when the transition is legal
    _set(uid, rid, MachineState.preparing.value, selected_provider_account="a@b.com")
    assert _run(agent_run_store.get_run(uid, rid))["selected_provider_account"] == "a@b.com"


def test_awaiting_approval_can_never_reach_succeeded_through_the_store(clean_db):
    """THE headline claim. A run parked on a decision cannot become a success, no matter what evidence is
    attached to the write — because there is no edge, and because no operational lane may add one."""
    uid = _user()
    rid = _new_run(uid)
    _set(uid, rid, MachineState.preparing.value)
    _set(uid, rid, MachineState.awaiting_approval.value, active_decision={"decision_id": "d-1"})

    with pytest.raises(IllegalStatusWrite) as exc:
        _set(uid, rid, MachineState.succeeded.value, last_tool_result=VERIFIED)

    assert exc.value.reason == transitions.NOT_AN_EDGE
    assert _status(uid, rid) == "awaiting_approval"


def test_verifying_to_succeeded_needs_the_read_back_not_just_the_claim(clean_db):
    """`verified: true` with no provider id is somebody's opinion. The guard names what is missing so the
    runtime can say which half of the proof it does not have."""
    uid = _user()
    rid = _new_run(uid)
    _to_verifying(uid, rid, UNEVIDENCED)

    with pytest.raises(IllegalStatusWrite) as exc:
        _set(uid, rid, MachineState.succeeded.value)

    assert exc.value.reason == transitions.UNVERIFIED
    assert transitions.READ_BACK_ENTITY_ID in exc.value.missing_slots
    assert _status(uid, rid) == "verifying"              # the honest state for an unproven write


def test_a_write_with_no_tool_result_at_all_cannot_reach_succeeded(clean_db):
    uid = _user()
    rid = _new_run(uid)
    _to_verifying(uid, rid, {})

    with pytest.raises(IllegalStatusWrite) as exc:
        _set(uid, rid, MachineState.succeeded.value)
    assert exc.value.reason == transitions.UNVERIFIED
    assert transitions.VERIFIED_READ_BACK in exc.value.missing_slots


# --- the store: what a legal run actually does ---------------------------------------------------------

def test_verifying_to_succeeded_works_when_the_read_back_is_real(clean_db):
    """The positive control for every refusal above. The evidence is read from the run's OWN durable
    `last_tool_result` — written by verified I/O — not from an argument to this call."""
    uid = _user()
    rid = _new_run(uid)
    _to_verifying(uid, rid, VERIFIED)

    _set(uid, rid, MachineState.succeeded.value)

    assert _status(uid, rid) == "succeeded"


def test_a_legal_full_lifecycle_still_completes(clean_db):
    """understanding -> preparing -> executing -> verifying -> succeeded, through the real store, with no
    suspension and no direct column write anywhere."""
    uid = _user()
    rid = _new_run(uid)

    _set(uid, rid, MachineState.preparing.value)
    _set(uid, rid, MachineState.executing.value, guard_ctx=EXECUTABLE,
         current_action={"capability": "gmail.send_message"})
    _set(uid, rid, MachineState.verifying.value, last_tool_result=VERIFIED)
    _set(uid, rid, MachineState.succeeded.value)

    assert _status(uid, rid) == "succeeded"
    assert _run(agent_run_store.latest_active(uid, domain=None)) is None   # terminal -> no longer active


def test_the_direct_action_lifecycle_still_completes(clean_db):
    """`agent_loop.run_direct_action`'s shape: a Tier-0 run is created in `understanding`, has no
    persisted deliberation, and ends in the LEASE lane's `completed`. Declared in the operational table
    rather than silently permitted — deleting that declaration must break this test."""
    uid = _user()
    rid = _new_run(uid)

    _set(uid, rid, "executing", current_action={"capability": "gmail.send_message"})
    _set(uid, rid, "completed", last_tool_result=VERIFIED)

    assert _status(uid, rid) == "completed"


def test_completed_is_not_a_synonym_for_succeeded(clean_db):
    """The lease lane's terminal and the machine's terminal are different words on purpose. If they were
    folded together, `complete_background` — a write and nothing more — would land in `succeeded`."""
    uid = _user()
    rid = _new_run(uid)
    _to_verifying(uid, rid, VERIFIED)

    with pytest.raises(IllegalStatusWrite):
        _set(uid, rid, "completed")                       # verifying has no lease-lane exit
    _set(uid, rid, MachineState.succeeded.value)          # positive control: the machine exit is open
    assert _status(uid, rid) == "succeeded"


# --- terminals -----------------------------------------------------------------------------------------

def test_a_succeeded_run_is_immovable(clean_db):
    uid = _user()
    rid = _new_run(uid)
    _to_verifying(uid, rid, VERIFIED)
    _set(uid, rid, MachineState.succeeded.value)

    for target in ("preparing", "executing", "failed", "cancelled"):
        with pytest.raises(IllegalStatusWrite) as exc:
            _set(uid, rid, target)
        assert exc.value.reason == transitions.TERMINAL_STATE
    assert _status(uid, rid) == "succeeded"


def test_cancelling_a_run_mid_provider_call_is_refused_but_a_background_run_stops(clean_db):
    """`transitions.py` offers no edge out of `executing` into `cancelled`: the provider call is already
    in flight and "cancelled" would be a statement about a system we do not control. A background
    `running` run is genuinely different — the worker sees the status on its next read and stops — so the
    operational lane declares that one, and this test is what keeps the two apart."""
    uid = _user()
    mid_call = _new_run(uid)
    _set(uid, mid_call, MachineState.preparing.value)
    _set(uid, mid_call, MachineState.executing.value, guard_ctx=EXECUTABLE)

    with pytest.raises(IllegalStatusWrite) as exc:
        _run(agent_run_store.cancel_background(uid, mid_call))
    assert exc.value.reason == transitions.NOT_AN_EDGE
    assert _status(uid, mid_call) == "executing"

    background = UUID(str(_run(agent_run_store.enqueue_background(uid, domain="mission"))["id"]))
    claimed = _run(agent_run_store.claim_background("w1"))
    assert claimed["id"] == str(background) and claimed["status"] == "running"
    _run(agent_run_store.cancel_background(uid, background))
    assert _status(uid, background) == "cancelled"


def test_cancelling_an_already_finished_run_is_a_no_op_not_an_error(clean_db):
    """"Stop it" has already been satisfied; asking twice is not a defect. The run must also not be
    relabelled by the second call."""
    uid = _user()
    rid = UUID(str(_run(agent_run_store.enqueue_background(uid, domain="mission"))["id"]))
    _run(agent_run_store.cancel_background(uid, rid))
    _run(agent_run_store.cancel_background(uid, rid))
    assert _status(uid, rid) == "cancelled"


def test_a_late_worker_cannot_relabel_a_cancelled_mission_as_completed(clean_db):
    """The bug the gate closes in `complete_background`: its UPDATE had no status predicate, so a worker
    finishing a mission the student cancelled while it was in flight overwrote `cancelled`."""
    uid = _user()
    rid = UUID(str(_run(agent_run_store.enqueue_background(uid, domain="mission"))["id"]))
    _run(agent_run_store.claim_background("w1"))
    _run(agent_run_store.cancel_background(uid, rid))

    _run(agent_run_store.complete_background(uid, rid, status="completed", worker_id="w1"))
    assert _status(uid, rid) == "cancelled"

    # positive control: a run that was NOT cancelled completes exactly as before
    other = UUID(str(_run(agent_run_store.enqueue_background(uid, domain="mission"))["id"]))
    _run(agent_run_store.claim_background("w1"))
    _run(agent_run_store.complete_background(uid, other, status="completed", worker_id="w1"))
    assert _status(uid, other) == "completed"


def test_an_unknown_status_word_is_refused_before_it_reaches_the_column(clean_db):
    uid = _user()
    rid = _new_run(uid)

    with pytest.raises(IllegalStatusWrite) as exc:
        _set(uid, rid, "sending messages")               # the transcript's free text, in another field
    assert exc.value.reason == agent_run_store.UNKNOWN_STATUS

    with pytest.raises(IllegalStatusWrite) as exc:
        _new_run(uid, status="in_progress")              # a creation cannot invent one either
    assert exc.value.reason == agent_run_store.UNKNOWN_STATUS


# --- the database's own opinion ------------------------------------------------------------------------

def test_the_db_constraint_rejects_a_bogus_status(clean_db):
    """Belt and braces, and not redundant: the column is a plain String(32), so anything holding a
    session can still write to it. With the code gate deliberately suspended, Postgres refuses."""
    uid = _user()
    rid = _new_run(uid)

    with agent_run_store.unchecked_status_writes_for_test():
        with pytest.raises((IntegrityError, DBAPIError)):
            _set(uid, rid, "totally_made_up")
        # positive control: the SAME suspended path writes a status the constraint knows
        _set(uid, rid, MachineState.blocked.value)

    assert _status(uid, rid) == "blocked"


def test_migration_vocabulary_matches_the_code():
    """The constraint is a literal in the migration (a migration is a historical record and must not
    change meaning when a constant does), so the drift has to be caught here instead."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions"
           / "0031_agent_run_status_check.py").read_text()
    block = re.search(r"STATUS_VOCABULARY = \(\n(.*?)\n\)", src, re.S)
    assert block, "0031 no longer declares STATUS_VOCABULARY"
    assert set(re.findall(r'"([a-z_]+)"', block.group(1))) == set(agent_run_store.STATUS_VOCABULARY)


# --- the escape hatch ----------------------------------------------------------------------------------

def test_the_escape_hatch_suspends_the_gate_and_restores_it(clean_db):
    uid = _user()
    rid = _new_run(uid)

    with agent_run_store.unchecked_status_writes_for_test():
        _set(uid, rid, MachineState.verifying.value)      # illegal, and deliberately permitted here
    assert _status(uid, rid) == "verifying"

    with pytest.raises(IllegalStatusWrite):               # the suspension did not leak past the block
        _set(uid, rid, MachineState.awaiting_approval.value)


def test_the_escape_hatch_is_unreachable_outside_pytest(monkeypatch):
    """A runtime property, not a naming convention: without pytest in the environment the first call
    raises, so a production path cannot use it even by importing it."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError):
        with agent_run_store.unchecked_status_writes_for_test():
            pass                                          # pragma: no cover — the enter must raise


# --- what an operational lane is allowed to be ---------------------------------------------------------

def test_an_operational_lane_may_never_open_a_route_into_succeeded():
    """The invariant that makes the supplement safe to widen. Both halves matter: a lane that adds an
    ordinary edge is fine, one that adds `succeeded` is rejected at import."""
    agent_run_store.check_operational_lane({"blocked": frozenset({"executing"})}, "control")
    with pytest.raises(AssertionError):
        agent_run_store.check_operational_lane({"verifying": frozenset({"succeeded"})}, "bad")
    with pytest.raises(AssertionError):
        agent_run_store.check_operational_lane({"succeeded": frozenset({"preparing"})}, "bad")
    with pytest.raises(AssertionError):
        agent_run_store.check_operational_lane({"understanding": frozenset({"nonsense"})}, "bad")


# --- the mission phase log -----------------------------------------------------------------------------

def _mission(uid: UUID, src: str = "pmid-1") -> UUID:
    return _run(mission_kernel.create_handoff_mission(
        uid, capability="calendar.create_event", source_message_id=src,
        proposed_goal="add the thing", short_status="capturing")).mission_id


def _phase(uid: UUID, mid: UUID) -> str:
    return _run(mission_kernel.get_mission_state(uid, mid))["phase"]


def _record(uid: UUID, mid: UUID, phase: str, short: str = "s", **kw) -> bool:
    return _run(mission_kernel.record_phase(uid, mid, phase, short, **kw))


def test_a_mission_cannot_jump_from_awaiting_approval_to_succeeded(clean_db):
    """The phase log is what a student reads to decide whether Bruce did something. It used to be a bare
    assignment, so "awaiting your ok" could become "Added and verified" with no provider call at all."""
    uid = _user()
    mid = _mission(uid)
    _record(uid, mid, MissionPhase.extracting.value, "prepared")     # approval is approval OF something
    _record(uid, mid, MissionPhase.awaiting_approval.value, status="running")

    with pytest.raises(IllegalStatusWrite) as exc:
        _record(uid, mid, MissionPhase.succeeded.value, "verified", status="succeeded")
    assert exc.value.reason == transitions.NOT_AN_EDGE
    assert _phase(uid, mid) == "awaiting_approval"


def test_a_mission_reaches_succeeded_only_with_the_read_back(clean_db):
    uid = _user()
    mid = _mission(uid)
    _record(uid, mid, MissionPhase.extracting.value, "prepared")
    _record(uid, mid, MissionPhase.executing.value, "creation_attempted")
    _record(uid, mid, MissionPhase.verifying.value, "fetched_back")

    with pytest.raises(IllegalStatusWrite) as exc:
        _record(uid, mid, MissionPhase.succeeded.value, "verified", status="succeeded")
    assert exc.value.reason == transitions.UNVERIFIED
    assert _phase(uid, mid) == "verifying"

    # positive control: the caller holding the provider's answer records the success it can prove
    _record(uid, mid, MissionPhase.succeeded.value, "verified", status="succeeded",
            verified_read_back=True, read_back_entity_id="evt-abc")
    state = _run(mission_kernel.get_mission_state(uid, mid))
    assert state["phase"] == "succeeded" and state["status"] == "succeeded"


def test_a_mission_row_cannot_be_marked_succeeded_behind_the_phases_back(clean_db):
    """The status column is a rollup of the phase, not a second machine — otherwise it is a way to claim
    success through the one field the read-back guard does not look at."""
    uid = _user()
    mid = _mission(uid)

    with pytest.raises(IllegalStatusWrite) as exc:
        _record(uid, mid, MissionPhase.blocked.value, "stuck", status="succeeded")
    assert exc.value.reason == transitions.NOT_FROM_VERIFYING

    _record(uid, mid, MissionPhase.blocked.value, "stuck", status="running")   # positive control
    assert _phase(uid, mid) == "blocked"


def test_an_unrecognised_phase_word_is_refused(clean_db):
    uid = _user()
    mid = _mission(uid)

    with pytest.raises(IllegalStatusWrite) as exc:
        _record(uid, mid, "almost_done")
    assert exc.value.reason == agent_run_store.UNKNOWN_STATUS

    with pytest.raises(IllegalStatusWrite) as exc:
        _record(uid, mid, MissionPhase.extracting.value, status="mostly_fine")
    assert exc.value.reason == agent_run_store.UNKNOWN_STATUS


def test_a_finished_mission_records_nothing_further(clean_db):
    """A redelivered flyer re-walks the operation graph. That must not drag the phase log back to
    `preparing` and forward to a second `verified` — terminal means the log stops."""
    uid = _user()
    mid = _mission(uid)
    _record(uid, mid, MissionPhase.extracting.value, "prepared")
    _record(uid, mid, MissionPhase.executing.value, "creation_attempted")
    _record(uid, mid, MissionPhase.verifying.value, "fetched_back")
    _record(uid, mid, MissionPhase.succeeded.value, "verified", status="succeeded",
            verified_read_back=True, read_back_entity_id="evt-abc")
    before = len(_run(mission_kernel.get_mission_state(uid, mid))["phase_events"])

    assert _record(uid, mid, MissionPhase.extracting.value, "prepared") is False
    state = _run(mission_kernel.get_mission_state(uid, mid))
    assert state["phase"] == "succeeded" and len(state["phase_events"]) == before


# ---------------------------------------------------------------------------------------------------
# The guard derivation itself. Both defects below were invisible to a green suite: nothing exercised the
# `executing` edge yet, so the store could have shipped with sends structurally impossible.
# ---------------------------------------------------------------------------------------------------

def test_guard_derivation_never_raises_on_a_half_collected_goal():
    """`tool_arguments()` raises when a required slot is empty. Deriving the guard from it meant an
    ordinary status write on a goal still being collected exploded — the guard failing on exactly the
    runs it protects."""
    from bruce_engine import agent_run_store, goal_slots
    from bruce_engine.goal_slots import SlotValue, Source

    kind = "send_email"
    slots = {"recipient": SlotValue("coach@school.edu", Source.user_stated, turn_id="t1", turn_index=1)}
    goal = goal_slots.to_goal_jsonb({}, kind, slots)          # subject + body deliberately absent

    class _Row:
        current_action = last_tool_result = verification_result = active_decision = None

    row = _Row()
    row.goal = goal
    ctx = agent_run_store._derive_guard_ctx(row, {})           # must NOT raise
    assert "recipient" not in ctx.missing(), "a filled slot must not read as missing"
    assert set(ctx.missing()) == {"subject", "body"}, ctx.missing()


def test_a_fully_collected_goal_reports_nothing_missing_so_executing_is_reachable():
    """The namespace bug: required_slots names slots ("recipient"), tool_arguments names tool args
    ("to"). Comparing them meant a complete goal still reported missing and no send could ever run."""
    from bruce_engine import agent_run_store, goal_slots
    from bruce_engine.goal_slots import SlotValue, Source

    kind = "send_email"
    slots = {n: SlotValue(v, Source.user_stated, turn_id="t1", turn_index=1) for n, v in (
        ("recipient", "coach@school.edu"), ("subject", "thank you"), ("body", "thanks for the season"))}

    class _Row:
        current_action = last_tool_result = verification_result = active_decision = None

    row = _Row()
    row.goal = goal_slots.to_goal_jsonb({}, kind, slots)
    ctx = agent_run_store._derive_guard_ctx(row, {})
    assert ctx.missing() == (), f"a complete goal must report nothing missing, got {ctx.missing()}"
