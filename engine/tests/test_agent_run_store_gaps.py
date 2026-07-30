"""The three store-level gaps that kept the brain spine from changing anything, pinned against real Postgres.

The transcript this whole spine exists for: a student asks Bruce to email one named address a thank-you
note, and twenty-two turns produce zero missions and zero agent_runs. The layers above (goal_slots,
transitions, goal_runtime) are only as good as the row underneath them, and `agent_run_store` had three
holes that made durable state lie about itself:

  1. `latest_active` decided "finished" from a HAND-WRITTEN list of statuses. It predated `succeeded`
     being reachable at all, so the one state that means "we verified the send really happened" read as
     IN FLIGHT forever — and the next turn would continue a goal that was already done. It is derived
     from `contract.TERMINAL_STATES` now, so the loop below covers a terminal nobody has added yet.
  2. `create_run` had no `conversation_id` parameter although the column has existed since migration
     0023, so every run ever created through the store was NULL there and no goal could be scoped to the
     thread that asked for it.
  3. `_to_dict` dropped the column even when something else had set it, so a caller holding a run dict
     could not see the attribution that was sitting in the row.

Every absence assertion here ("this run is not active", "this value is not written") is paired with the
positive control that makes it mean something: if `latest_active` were changed to return None always, the
control tests fail; if it returned every run, the terminal tests fail. Neither half passes alone.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine as _real_create_async_engine
from sqlalchemy.pool import NullPool

import bruce_engine.db as db
from bruce_engine import agent_run_store, transitions
from bruce_engine.contract import TERMINAL_STATES, MachineState
from bruce_engine.repositories import PostgresUserRepository

users = PostgresUserRepository()

# What a run carries once a provider really did the thing and an independent read-back found it. This is
# the only way into `succeeded`, so a test about `succeeded` has to earn it rather than assert it.
VERIFIED = {"verified": True, "provider_entity_id": "prov-entity-1"}

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


def _park(uid: UUID, rid: UUID, status: str) -> None:
    """Plant a run in an arbitrary status. The sanctioned suspension, used for exactly what its docstring
    describes: a fixture that needs a run parked somewhere, not a way to make an illegal move legal."""
    with agent_run_store.unchecked_status_writes_for_test():
        _run(agent_run_store.update_run(uid, rid, status=status))


def _active_id(uid: UUID) -> str | None:
    row = _run(agent_run_store.latest_active(uid, domain=None))
    return None if row is None else str(row["id"])


# --- gap 1: a finished run must stop reading as in-flight ------------------------------------------------

def test_verified_send_stops_being_active(clean_db):
    """The transcript's next turn. A run walked all the way to `succeeded` — the one terminal that means
    the email genuinely went — must not still be offered as the open goal, or Bruce keeps working a goal
    it already finished. `succeeded` was the exact status the old hand-written exclusion list forgot."""
    uid = _user()
    rid = _new_run(uid, domain="gmail", goal={"action": "gmail.send_message"})

    _set(uid, rid, MachineState.preparing.value)
    _set(uid, rid, MachineState.executing.value, guard_ctx=EXECUTABLE)
    _set(uid, rid, MachineState.verifying.value, last_tool_result=VERIFIED)
    assert _active_id(uid) == str(rid)                    # control: mid-flight, it IS the open goal

    _set(uid, rid, MachineState.succeeded.value)
    assert _run(agent_run_store.get_run(uid, rid))["status"] == MachineState.succeeded.value
    assert _active_id(uid) is None                        # verified and over -> nothing to continue


def test_cancelled_run_stops_being_active(clean_db):
    """The student's own "stop". `cancelled` is terminal for the same reason `succeeded` is: there is
    nothing left to advance, and reporting it as in-flight work is a lie about durable state."""
    uid = _user()
    rid = _new_run(uid, domain="gmail")
    assert _active_id(uid) == str(rid)                    # control: freshly opened, active

    _run(agent_run_store.cancel_background(uid, rid))
    assert _active_id(uid) is None


@pytest.mark.parametrize("state", sorted(s.value for s in TERMINAL_STATES))
def test_every_contract_terminal_reads_as_finished(clean_db, state):
    """Parametrised over `contract.TERMINAL_STATES` itself, so this is a test about the DERIVATION and not
    about two states that happen to be listed today. Add a terminal to the contract and it is covered here
    the moment it exists — which is the property the hand-written list did not have."""
    uid = _user()
    rid = _new_run(uid, domain="gmail")
    _park(uid, rid, state)
    assert _active_id(uid) is None


@pytest.mark.parametrize("state", sorted(
    s.value for s in MachineState if s not in TERMINAL_STATES and s is not MachineState.failed))
def test_every_non_terminal_state_still_reads_as_active(clean_db, state):
    """The positive control for the two tests above, and the reason "just exclude more" is not a fix.

    Every machine state that is NOT terminal is work the student can still be told about. `failed` is the
    one deliberate omission — retryable by the machine, but nothing is currently advancing it, so
    `latest_active` does not report it as in flight — and it is excluded from this parametrisation rather
    than quietly passing, so removing that carve-out fails the test below instead of this one.
    """
    uid = _user()
    rid = _new_run(uid, domain="gmail")
    _park(uid, rid, state)
    assert _active_id(uid) == str(rid)


def test_failed_is_not_reported_as_in_flight(clean_db):
    """`failed` is not a contract terminal (a retry must stay possible) but it is not in-flight work
    either. Pinned on its own so the carve-out is a decision with a test, not an accident of a list."""
    uid = _user()
    rid = _new_run(uid, domain="gmail")
    _park(uid, rid, MachineState.failed.value)
    assert _active_id(uid) is None
    assert _run(agent_run_store.get_run(uid, rid))["status"] == MachineState.failed.value  # still there


# --- gap 2: the run has to know which conversation it belongs to -----------------------------------------

def test_create_run_persists_conversation_id(clean_db):
    """`get_run` re-reads the row in a new session, so this is the COLUMN answering, not the dict that
    `create_run` happened to return."""
    uid, conv = _user(), uuid4()
    rid = _new_run(uid, domain="gmail", conversation_id=conv)
    assert _run(agent_run_store.get_run(uid, rid))["conversation_id"] == str(conv)


def test_conversation_id_is_absent_unless_asked_for(clean_db):
    """The control for the test above: nothing defaults the column, so a run that reads as belonging to a
    conversation belongs to it because a caller said so. Unattributed is None — background missions are
    created by the worker with no conversation at all, and that is a real answer, not a missing one."""
    uid = _user()
    rid = _new_run(uid, domain="gmail")
    assert _run(agent_run_store.get_run(uid, rid))["conversation_id"] is None


def test_uuid_and_string_forms_agree(clean_db):
    """Callers hold this id both ways (a UUID from the row, a str through a JSON boundary). Two runs that
    named the same conversation differently must not read as two different conversations."""
    uid, conv = _user(), uuid4()
    as_obj = _new_run(uid, domain="gmail", conversation_id=conv)
    as_str = _new_run(uid, domain="gmail", conversation_id=str(conv))
    assert (_run(agent_run_store.get_run(uid, as_obj))["conversation_id"]
            == _run(agent_run_store.get_run(uid, as_str))["conversation_id"] == str(conv))


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_blank_conversation_id_means_unattributed(clean_db, blank):
    """Empty is not an error and not a value. `goal_runtime._conversation_of` already reads "" as
    UNATTRIBUTED — a run visible to every thread — so the store must land it as NULL rather than raise."""
    uid = _user()
    rid = _new_run(uid, domain="gmail", conversation_id=blank)
    assert _run(agent_run_store.get_run(uid, rid))["conversation_id"] is None


def test_non_uuid_conversation_id_is_refused_loudly(clean_db):
    """A handle is not a uuid and the column cannot hold one. Dropping it silently would recreate this
    mission's whole failure mode one layer down: the caller believes the goal is scoped to a thread, the
    row says nothing, and the next turn continues somebody else's goal. So it raises — and it raises
    BEFORE the insert, leaving no half-attributed run behind."""
    uid = _user()
    with pytest.raises(ValueError) as excinfo:
        _new_run(uid, domain="gmail", conversation_id="+15550001111")
    assert "+1555" not in str(excinfo.value)              # the value is a handle; it is never echoed
    assert _active_id(uid) is None                        # nothing was written

    _new_run(uid, domain="gmail", conversation_id=str(uuid4()))   # control: a real one still goes in
    assert _active_id(uid) is not None


def test_idempotent_create_does_not_reattribute(clean_db):
    """A second create with the same key REFERENCES the existing run. Re-pointing a live goal at whichever
    conversation asked last is the cross-thread confusion the column exists to prevent."""
    uid, first, second = _user(), uuid4(), uuid4()
    a = _run(agent_run_store.create_run(uid, domain="gmail", conversation_id=first, idempotency_key="k1"))
    b = _run(agent_run_store.create_run(uid, domain="gmail", conversation_id=second, idempotency_key="k1"))
    assert a["id"] == b["id"]
    assert b["conversation_id"] == str(first)
    assert _run(agent_run_store.get_run(uid, UUID(a["id"])))["conversation_id"] == str(first)


# --- gap 3: every reader of a run has to be able to see it -----------------------------------------------

def test_every_store_reader_carries_conversation_id(clean_db):
    """`create_run`, `get_run` and `latest_active` all go through `_to_dict`, which dropped the column —
    so a caller scoping a goal to a thread had to re-query the table itself to see what was already on
    the row it was holding."""
    uid, conv = _user(), uuid4()
    created = _run(agent_run_store.create_run(uid, domain="gmail", conversation_id=conv))
    fetched = _run(agent_run_store.get_run(uid, UUID(created["id"])))
    active = _run(agent_run_store.latest_active(uid, domain=None))
    assert created["conversation_id"] == fetched["conversation_id"] == active["conversation_id"] == str(conv)


def test_conversation_id_is_a_string_or_none_never_a_uuid_object(clean_db):
    """Shaped like every other id this dict emits (and like `goal_runtime._row_dict`), because callers
    compare it against `str(conversation_id)` — a UUID object would compare unequal to its own string."""
    uid, conv = _user(), uuid4()
    with_conv = _run(agent_run_store.create_run(uid, domain="gmail", conversation_id=conv))
    without = _run(agent_run_store.create_run(uid, domain="gmail"))
    assert isinstance(with_conv["conversation_id"], str)
    assert without["conversation_id"] is None
