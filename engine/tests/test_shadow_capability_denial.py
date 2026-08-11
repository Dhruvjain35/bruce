"""FALSE CAPABILITY DENIAL — the metric this whole module exists to collect, and the one it counted wrong.

The production defect, in the founder's own thread: Bruce answered "i can't actually do outside actions
like scheduling or sending stuff from here" while `calendar.create_event` was connected, registered and
live. That is a FALSE CAPABILITY DENIAL, and it is one of the four numbers the decision to grant the
semantic executive authority rests on.

TWO DEFECTS, IN OPPOSITE DIRECTIONS, AND THE SECOND IS WHY THIS FILE WAS REWRITTEN.

  1. `compare()` ACCEPTED a `reachable` argument and discarded it, so the number was never computed at
     all — not wrongly, not approximately: never, on any turn, and nothing failed.

  2. Then it WAS computed, from `semantic_executive.mini_context`, which takes no user_id and defaults
     `reachable` to `tool_registry.specs(None) if s.live` — a nine-element static table, identical for
     every user and every turn, from a function whose own docstring says it does NOT check the user's
     connection. So: a student with no Google connection asks for a calendar event; the router truthfully
     says Bruce has no hands; the executive proposes `calendar.create_event` because it is globally live;
     and a false capability denial is recorded against a router that was RIGHT.

     That is the worst possible direction for this number to be wrong in. It over-counts in exactly the
     direction that argues FOR granting authority, and it does so hardest on the accounts with the least
     capability — the ones where the router's honesty is most correct. The previous version of this file
     asserted that behaviour was correct: it enqueued under `uuid4()`, a user with no OAuth of any kind,
     and asserted `false_capability_denial is True`.

Capability truth is now computed at the REQUEST BOUNDARY through the per-user broker, PERSISTED on the
turn's row, and CONSUMED from there by the worker. The tests below are written so that putting the global
registry back — in either place — fails them.

It still has to be classified IN PROCESS. The row stores no message body and never will, so there is
nothing to re-derive it from later. What is persisted is a boolean, an operation id from Bruce's own
registry, a short reason code, and the SIZE of the turn's reachable set — nothing a model wrote.

The conditions are asserted one at a time, each by REMOVING exactly one of them, because a conjunction
that is only ever tested with everything true is a conjunction nobody has tested.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from bruce_engine import semantic_shadow, tool_broker, tool_registry
from bruce_engine.semantic_contracts import (Actionability, Family, GoalCount, OperationFamily,
                                             SemanticTurn, TurnRole)

CHANNEL, PMID = "imessage", "pm-denial"
LIVE_OP = "calendar.create_event"          # registered, live, and what the founder was told Bruce lacked
SEND_OP = "gmail.send_message"
CAL_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# THIS user, on THIS turn, could reach these — the shape the broker returns, never the registry's.
REACHABLE = semantic_shadow.ReachableOperations(frozenset({LIVE_OP, SEND_OP}), established=True)
NOTHING_REACHABLE = semantic_shadow.ReachableOperations(frozenset(), established=True)
UNKNOWN = semantic_shadow.ReachableOperations()


class _Turn:
    """The executive's reading, reduced to what `compare` reads. Shape-compatible with ExecutiveTurn."""

    class _Mode:
        def __init__(self, v): self.value = v

    def __init__(self, mode: str, operation: str | None):
        self.mode = self._Mode(mode)
        self.proposed_operation_id = operation


class _Router:
    """A router decision. `source='router_default'` is Stage 1's silent fall-through — the population the
    executive exists to rescue, and the shape the founder's turn took."""

    def __init__(self, *, execution_class="fast_conversation", capabilities=(), source="router_default",
                 production_path=semantic_shadow.PATH_ROUTED):
        self.execution_class = execution_class
        self.candidate_capabilities = tuple(capabilities)
        self.source = source
        self.action = None
        self.domain = None
        self.production_path = production_path


# --- capability truth is per USER and per TURN -------------------------------------------------------

def test_two_users_on_the_same_turn_get_different_reachable_sets(monkeypatch):
    """The property a global constant cannot have. Same words, same instant, different hands.

    A reachable set is not a fact about Bruce; it is a fact about a student's account. A snapshot that
    cannot tell these two apart is not capability truth, it is a copy of the registry.

    The broker's own connection probe is the seam being patched, so registration, liveness, connection
    and scope all run for real — the thing under test is the join, not a stub of it.
    """
    connected, disconnected = uuid4(), uuid4()

    async def _probe(user_id, provider):
        return tool_broker._Conn(True, (CAL_SCOPE,)) if user_id == connected else tool_broker._Conn(False)

    monkeypatch.setattr(tool_broker, "_provider_connection", _probe)

    async def _go():
        return (await semantic_shadow.turn_capability_truth(connected),
                await semantic_shadow.turn_capability_truth(disconnected))

    a, b = asyncio.run(_go())

    assert a.established and b.established, "capability truth was not established for a healthy probe"
    assert LIVE_OP in a.operations, "a connected, calendar-scoped user cannot reach calendar.create_event"
    assert SEND_OP not in a.operations, (
        "a calendar-only grant reached Gmail — the scope check is what separates the two hands that share "
        "one Google integration")
    assert b.operations == frozenset(), "a user with no Google connection was handed live capabilities"
    assert a.operations != b.operations, (
        "two users got the SAME reachable set, which is what `tool_registry.specs(None)` returns for "
        "everybody and is the defect this snapshot exists to remove")


def test_a_failed_probe_is_unknown_and_not_an_empty_set(monkeypatch):
    """"This user can reach nothing" and "we could not find out" must not be the same value.

    They are both an empty set, and only one of them is evidence. Counting the second would manufacture
    the denial metric out of an outage — the metric would look worst exactly when the data is least
    trustworthy.
    """
    def _explode(user_id):
        raise RuntimeError("the broker is unreachable")

    monkeypatch.setattr(tool_broker, "reachable_operations", _explode)
    snap = asyncio.run(semantic_shadow.turn_capability_truth(uuid4()))

    assert snap.established is False and snap.operations == frozenset()
    assert snap.as_json() is None, "an unestablished snapshot persisted as [] would claim we looked"


# --- the classification ------------------------------------------------------------------------------

def test_the_founder_turn_is_recorded_as_a_false_capability_denial():
    """All three conditions hold: registered + reachable FOR THIS USER, production disclaimed, executive
    found it."""
    cmp = semantic_shadow.compare(_Turn("new_goal", LIVE_OP), _Router(), reachable=REACHABLE.operations)

    assert cmp.false_capability_denial is True, (
        "the exact production defect — a live, registered, reachable capability denied to the student — "
        "was not counted, which is the number the authority decision is supposed to rest on")
    assert cmp.denied_operation_id == LIVE_OP, "the denial does not name which capability was denied"
    assert cmp.denial_reason == semantic_shadow.DENIAL_ROUTER_DEFAULT, (
        "the reason must distinguish the router's silent fall-through from a router that decided")
    assert cmp.agrees is False and cmp.divergence == "executive_found_work_router_missed"


def test_an_unreachable_operation_is_not_a_denial():
    """Condition (a). If the capability is not reachable for this user, production telling them so is
    TRUE. Counting it would turn every unconnected account into evidence against the router."""
    cmp = semantic_shadow.compare(_Turn("new_goal", LIVE_OP), _Router(),
                                  reachable=frozenset({SEND_OP}))
    assert cmp.false_capability_denial is False
    assert cmp.denied_operation_id is None and cmp.denial_reason is None


def test_an_unknown_reachable_set_is_never_read_as_a_denial():
    """Condition (a), the failure case that matters most.

    An EMPTY reachable set means either that this user can reach nothing or that capability truth could
    not be established. Treating either as "it was reachable" would manufacture this metric out of an
    unconnected account or an outage.
    """
    cmp = semantic_shadow.compare(_Turn("new_goal", LIVE_OP), _Router(), reachable=frozenset())
    assert cmp.false_capability_denial is False


def test_a_router_that_offered_the_work_is_not_a_denial():
    """Condition (b). Production routed the turn to a real action, so nothing was denied or disclaimed —
    the executive and the router simply agree."""
    cmp = semantic_shadow.compare(_Turn("new_goal", LIVE_OP),
                                  _Router(execution_class="direct_action", capabilities=(LIVE_OP,),
                                          source="stage0"),
                                  reachable=REACHABLE.operations)
    assert cmp.false_capability_denial is False
    assert cmp.agrees is True


def test_a_router_naming_a_different_capability_is_a_disagreement_not_a_denial():
    """Condition (b), narrowed. The router named A CAPABILITY — it did not tell the student it had no
    hands. Filing that as a denial would merge two findings with different fixes: one is a routing
    disagreement, the other is Bruce disclaiming a tool it has."""
    cmp = semantic_shadow.compare(_Turn("new_goal", LIVE_OP),
                                  _Router(execution_class="direct_action",
                                          capabilities=(SEND_OP,), source="stage0"),
                                  reachable=REACHABLE.operations)
    assert cmp.false_capability_denial is False
    assert cmp.divergence == "different_capability"


def test_a_lane_that_was_not_routing_can_never_deny_a_capability():
    """Condition (b), on the population that was previously never observed at all.

    A resolved continuation is answered from state: production is ADVANCING the work, and the student was
    told nothing about capabilities. Now that those turns are observed, reading their empty router
    snapshot as "production named no capability" would file a false denial against the lane that was busy
    performing the very capability in question — so widening the sample would itself inflate the metric,
    which would look exactly like evidence for granting authority.
    """
    cmp = semantic_shadow.compare(
        _Turn("continue_goal", LIVE_OP),
        _Router(production_path=semantic_shadow.PATH_CONTINUATION, capabilities=(LIVE_OP,), source=None),
        reachable=REACHABLE.operations)
    assert cmp.false_capability_denial is False, "a continuation was recorded as a capability denial"
    assert cmp.agrees is True


def test_a_router_outage_is_not_a_capability_denial():
    """Condition (b) again. The router RAISED, so production fell through to the reasoner and never
    decided anything. An outage is not a denial, and counting it would make this metric a function of
    infrastructure health."""
    cmp = semantic_shadow.compare(_Turn("new_goal", LIVE_OP),
                                  _Router(production_path=semantic_shadow.PATH_ROUTER_ERROR, source=None),
                                  reachable=REACHABLE.operations)
    assert cmp.false_capability_denial is False


def test_a_denial_needs_the_executive_to_have_found_the_operation():
    """Condition (c). If the executive proposed nothing either, both readers came up empty — that is the
    executive failing too, not production denying something the executive had found."""
    cmp = semantic_shadow.compare(_Turn("new_goal", None), _Router(), reachable=REACHABLE.operations)
    assert cmp.false_capability_denial is False


def test_an_unregistered_operation_is_not_a_denial():
    """Condition (a) again, on the registry rather than the broker.

    `reachable` is the broker's answer and the registry is what exists at all. A capability that is
    somehow reachable but not registered is a configuration fault, and blaming the router for it would
    let the executive's own invented ids inflate this metric.
    """
    ghost = "calendar.summon_event"
    cmp = semantic_shadow.compare(_Turn("new_goal", ghost), _Router(),
                                  reachable=frozenset({ghost}))
    assert cmp.false_capability_denial is False


def test_a_denial_is_recorded_on_a_turn_the_router_decided_with_a_different_reason_code():
    """The two ways production can disclaim, kept apart.

    `router_default` means Stage 1 had no provider and everything fell through — a configuration finding.
    A router that produced a real decision and still named nothing is a ROUTING finding. Same metric,
    completely different fix, so the reason code carries the difference.
    """
    cmp = semantic_shadow.compare(_Turn("new_goal", LIVE_OP),
                                  _Router(source="stage1_model"), reachable=REACHABLE.operations)
    assert cmp.false_capability_denial is True
    assert cmp.denial_reason == semantic_shadow.DENIAL_ROUTER_DECIDED


# --- and it reaches the row --------------------------------------------------------------------------

class _ReadsACalendarRequest:
    """A reader whose answer derives `calendar.create_event` — the live capability the founder was told
    Bruce did not have."""
    provider, model = "fake", "reads"

    async def read(self, body):
        return SemanticTurn(turn_role=TurnRole.new_goal, actionability=Actionability.executable,
                            domain_candidates=(Family.calendar,),
                            operation_family=OperationFamily.create, goal_count=GoalCount.one,
                            confidence=0.9)


class _Decision:
    execution_class = "fast_conversation"
    action = None
    domain = None
    candidate_capabilities = ()
    source = "router_default"
    production_path = semantic_shadow.PATH_ROUTED


def _observe(uid, reachable, *, reader=None):
    """One turn, all the way through: intake with the turn's snapshot, then the worker's read of it."""
    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID,
                       text="can you put lunch with the counselor on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                     decision=_Decision(), reachable=reachable, store=store,
                                     tally=semantic_shadow.EnqueueLedger())
        observed = await semantic_shadow.claim_and_observe(
            worker_id="w1", store=store, triage=reader or _ReadsACalendarRequest())
        return observed, next(iter(store.jobs.values()))

    return asyncio.run(_go())


def test_the_denial_is_classified_during_observation_and_stored_on_the_row(monkeypatch):
    """End to end through the worker for a user who CAN reach the capability: read, classify, persist.

    Asserted on STORED STATE rather than on the returned object, because the returned object is telemetry
    nothing consumes — the row is what the authority decision will actually be counted from.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)

    observed, row = _observe(uuid4(), REACHABLE)

    assert observed.outcome == semantic_shadow.OK and observed.false_capability_denial is True
    assert row.false_capability_denial is True, (
        "the denial was computed and thrown away — it has to be a column, because counting it later from "
        "stored message bodies is impossible by design: there are none")
    assert row.observation["capability"] == {
        "false_denial": True, "operation": LIVE_OP,
        "reason": semantic_shadow.DENIAL_ROUTER_DEFAULT,
        "reachable_established": True, "reachable_count": 2}, (
        "only the boolean, the operation id, the short reason code and the SIZE of the turn's reachable "
        "set may be persisted")


def test_a_user_with_no_google_connection_produces_no_false_capability_denial(monkeypatch):
    """THE DEFECT, as a test. Same words, same router, same reader — a student who never connected Google.

    Production told them Bruce cannot schedule anything, and production was TELLING THE TRUTH. Under the
    global live registry this turn recorded a false capability denial, because `calendar.create_event` is
    live for the product and unreachable for this account. Replacing the persisted snapshot with
    `tool_registry.specs(None)` makes this test fail, which is the point of it.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)

    observed, row = _observe(uuid4(), NOTHING_REACHABLE)

    assert observed.outcome == semantic_shadow.OK, "the read did not land, so this proves nothing"
    assert row.false_capability_denial is False, (
        "a router that correctly told an unconnected student Bruce has no hands was recorded as having "
        "falsely denied a capability — the flagship metric over-counting in the direction that argues "
        "FOR granting the executive authority")
    assert row.observation["exec"]["operation"] is None, (
        "understanding named an operation this user cannot reach, so capability truth reached the "
        "executive's context from somewhere other than this turn's snapshot")
    assert row.observation["capability"]["reachable_count"] == 0
    assert row.observation["capability"]["reachable_established"] is True, (
        "an established-but-empty snapshot was recorded as unknown, which loses the difference between "
        "'this user can reach nothing' and 'we could not find out'")


def test_the_global_live_registry_would_have_recorded_that_denial():
    """The mutation, made explicit, so the assertion above is not vacuously true.

    If the two answers were the same there would be nothing to fix. This is the exact substitution the
    worker used to make — `tool_registry.specs(None) if s.live` in place of the turn's own snapshot — and
    it flips the metric on a turn where the router was right.
    """
    global_live = frozenset(s.capability for s in tool_registry.specs(None) if s.live)

    truthful = semantic_shadow.compare(_Turn("new_goal", LIVE_OP), _Router(),
                                       reachable=NOTHING_REACHABLE.operations)
    with_global = semantic_shadow.compare(_Turn("new_goal", LIVE_OP), _Router(), reachable=global_live)

    assert LIVE_OP in global_live, "the registry no longer declares the capability this defect was about"
    assert truthful.false_capability_denial is False
    assert with_global.false_capability_denial is True, (
        "the global registry and this user's real reachable set produce the SAME answer, so either the "
        "registry changed or this test no longer exercises the substitution it describes")


def test_connecting_an_integration_after_enqueue_does_not_change_the_recorded_comparison(monkeypatch):
    """Capability truth belongs to the TURN, not to whenever the worker happens to wake.

    A student connects Google a minute after asking. If the worker recomputed the reachable set at drain
    time, the same turn would flip from "the router was right" to "the router falsely denied a live
    capability" with no student and no router having done anything. The comparison would then describe a
    world that did not exist when Bruce answered, and the sample would drift with connection activity,
    which is not a property of the executive at all.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    uid = uuid4()

    # The world CHANGES between the request and the drain: at intake this account reaches nothing; by the
    # time the worker runs, the broker would happily report a full calendar-and-Gmail connection.
    async def _now_connected(user_id, provider):
        return tool_broker._Conn(True, (CAL_SCOPE, GMAIL_SEND_SCOPE, GMAIL_READ_SCOPE))

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID,
                       text="can you put lunch with the counselor on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                     decision=_Decision(), reachable=NOTHING_REACHABLE, store=store,
                                     tally=semantic_shadow.EnqueueLedger())
        monkeypatch.setattr(tool_broker, "_provider_connection", _now_connected)
        observed = await semantic_shadow.claim_and_observe(worker_id="w1", store=store,
                                                           triage=_ReadsACalendarRequest())
        return observed, next(iter(store.jobs.values()))

    observed, row = asyncio.run(_go())

    assert observed.outcome == semantic_shadow.OK
    assert row.false_capability_denial is False, (
        "an integration connected AFTER the turn rewrote what the comparison says happened during it")
    assert row.observation["capability"]["reachable_count"] == 0, (
        "the worker recomputed capability truth instead of reading the turn's persisted snapshot")


def test_a_disconnection_after_enqueue_does_not_erase_a_recorded_denial(monkeypatch):
    """The same property in the other direction, which is the one that hides a real defect.

    The student HAD Google connected when the router told them it could not schedule anything — a true
    false denial — and revoked it before the worker ran. A worker-time recomputation would quietly drop
    the finding, and the metric would under-count exactly the accounts that gave up on Bruce.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)
    uid = uuid4()

    async def _now_revoked(user_id, provider):
        return tool_broker._Conn(False)

    async def _go():
        store = semantic_shadow.InMemoryShadowStore()
        store.add_turn(uid, channel=CHANNEL, provider_message_id=PMID,
                       text="can you put lunch with the counselor on friday")
        await semantic_shadow.intake(uid, channel=CHANNEL, provider_message_id=PMID,
                                     decision=_Decision(), reachable=REACHABLE, store=store,
                                     tally=semantic_shadow.EnqueueLedger())
        monkeypatch.setattr(tool_broker, "_provider_connection", _now_revoked)
        observed = await semantic_shadow.claim_and_observe(worker_id="w1", store=store,
                                                           triage=_ReadsACalendarRequest())
        return observed, next(iter(store.jobs.values()))

    observed, row = asyncio.run(_go())

    assert observed.outcome == semantic_shadow.OK
    assert row.false_capability_denial is True, (
        "a revocation AFTER the turn erased a real finding about what the router did during it")


def test_an_unestablished_snapshot_records_no_denial_and_says_so(monkeypatch):
    """The outage case, end to end. The broker could not answer at the request boundary.

    The observation still happens — a failed capability probe is no reason to lose the reading — but no
    denial may be counted from it, and the row has to SAY the truth was unknown. Otherwise "no denials"
    and "we could not tell" are the same number, and the denominator of the metric is unknowable.
    """
    monkeypatch.setenv("BRUCE_SEMANTIC_SHADOW", "1")
    monkeypatch.setattr(semantic_shadow, "SHADOW_BUDGET_S", 5.0)

    observed, row = _observe(uuid4(), UNKNOWN)

    assert observed.outcome == semantic_shadow.OK, "an unknown reachable set cost us the observation"
    assert row.false_capability_denial is False
    assert row.observation["capability"]["reachable_established"] is False


# --- the pending capability, which was carried and then discarded -------------------------------------
#
# `RouterSnapshot.work_in_flight(pending_capability=...)` reads the registry id off the STILL-PENDING
# Decision — the operation the student is actually being asked to approve — and `conversation_runtime`
# has been supplying it on every continuation and every ambiguous goal selection. `compare` accepted it
# and threw it away: the work-in-flight branch returned agreement on `exec_mode` alone, so on the entire
# approval population "agrees" meant no more than "both readers knew this was a continuation".
#
# That is the most dangerous place in the sample to be blind. An approval TRANSFERS CONSENT: "yes send
# it" is consent to the operation the student was shown, and an executive that reads it as approval of a
# DIFFERENT operation would spend that consent on an action nobody agreed to. It was scoring as
# agreement, and agreement on the safety-critical population is the number that argues for authority.


def test_an_executive_approving_a_different_operation_than_the_pending_one_is_a_divergence():
    """The field is CONSUMED. The student was asked about `calendar.create_event`; the reading says the
    approval is about sending mail. Both readers agree the turn is a continuation and they mean two
    entirely different actions, which is exactly the disagreement worth catching."""
    cmp = semantic_shadow.compare(
        _Turn("approve", SEND_OP),
        semantic_shadow.RouterSnapshot.work_in_flight(
            semantic_shadow.PATH_CONTINUATION, pending_capability=LIVE_OP, evidence="approval"),
        reachable=REACHABLE.operations)

    assert cmp.agrees is False, (
        "an approval of a different capability than the one the student was shown scored as agreement — "
        "which is consent transferred to an action nobody consented to, counted as evidence FOR "
        "granting the executive authority")
    assert cmp.divergence == "different_capability_in_flight"
    assert cmp.false_capability_denial is False, (
        "a lane that was advancing work told the student nothing about capabilities, so it denied none")


def test_an_approval_of_the_same_pending_operation_agrees():
    """The positive control. Same operation on both sides is agreement, and it must stay agreement —
    a comparison that fires on the ordinary case is a comparison nobody can read."""
    cmp = semantic_shadow.compare(
        _Turn("approve", LIVE_OP),
        semantic_shadow.RouterSnapshot.work_in_flight(
            semantic_shadow.PATH_CONTINUATION, pending_capability=LIVE_OP, evidence="approval"),
        reachable=REACHABLE.operations)
    assert cmp.agrees is True and cmp.divergence is None


def test_a_missing_value_on_either_side_is_never_a_divergence():
    """The metric may not be manufactured out of an absence.

    An ambiguous goal selection has NO pending Decision, so there is no operation the student was being
    asked about; and a reading that named no operation is claiming only that the turn is about work in
    flight. Neither is a disagreement about which capability was meant, and counting either would make
    this number a function of how often a value happened to be missing.
    """
    no_pending = semantic_shadow.compare(
        _Turn("approve", SEND_OP),
        semantic_shadow.RouterSnapshot.work_in_flight(semantic_shadow.PATH_GOAL_AMBIGUOUS),
        reachable=REACHABLE.operations)
    assert no_pending.agrees is True and no_pending.divergence is None

    no_reading = semantic_shadow.compare(
        _Turn("approve", None),
        semantic_shadow.RouterSnapshot.work_in_flight(
            semantic_shadow.PATH_CONTINUATION, pending_capability=LIVE_OP, evidence="approval"),
        reachable=REACHABLE.operations)
    assert no_reading.agrees is True and no_reading.divergence is None


def test_the_pending_capability_reaches_compare_from_a_rehydrated_row():
    """The worker does not hold the live snapshot — it rehydrates one from JSONB on the job row. If the
    field survived `as_json` and not `from_json` (or the reverse) the comparison would be right in memory
    and wrong in production, which is the only place it runs."""
    live = semantic_shadow.RouterSnapshot.work_in_flight(
        semantic_shadow.PATH_CONTINUATION, pending_capability=LIVE_OP, evidence="approval")
    rehydrated = semantic_shadow.RouterSnapshot.from_json(live.as_json())

    assert rehydrated.candidate_capabilities == (LIVE_OP,)
    assert rehydrated.production_path == semantic_shadow.PATH_CONTINUATION
    cmp = semantic_shadow.compare(_Turn("approve", SEND_OP), rehydrated,
                                  reachable=REACHABLE.operations)
    assert cmp.divergence == "different_capability_in_flight", (
        "the pending capability did not survive the round trip through the row, so the comparison the "
        "worker actually makes is not the one the tests above exercise")
