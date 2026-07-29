"""The state machine the model does not get a vote in.

These tests exist because of one transcript. Bruce was asked to email a named address a thank-you
note; it drafted it, then asked for the recipient it had been given a turn earlier, then for the
message it had been given three turns earlier, then announced it could not send messages at all while
the broker was reporting `gmail.send_message` ok=True. Twenty-two turns, zero runs. The model was
holding the state and re-deciding it, wrongly and confidently, on every turn.

So the properties asserted here are not style. `succeeded` unreachable except from `verifying` with a
read-back is what stops Bruce claiming it sent something it did not send; `missing_slots` naming what
is absent is what stops it asking twice; and the happy-path walk is what stops the table from being
"safe" by refusing everything.
"""

from __future__ import annotations

from bruce_engine import transitions as tr
from bruce_engine.contract import RECOVERABLE_STATES, TERMINAL_STATES, Action, MachineState
from bruce_engine.contract import available_actions
from bruce_engine.transitions import GuardContext, propose_transition

EMPTY = GuardContext()

# The transcript's own action, fully resolved: a real registry id, a reachable account, every argument
# from gmail.send_message's arg_schema filled, and a live authorization.
SENDABLE = GuardContext(
    capability="gmail.send_message",
    capability_available=True,
    availability_status="ok",
    required_slots=("to", "subject", "body"),
    filled_slots=("to", "subject", "body"),
    authorization_open=True,
)

# What a real verification produced: an independent read-back that found the message.
READ_BACK = GuardContext(verified_read_back=True, read_back_entity_id="18fa2c9b0e11")

DECIDED = GuardContext(decision_id="dec-77")


# --- the table itself ----------------------------------------------------------------------------------


def test_the_edge_table_is_total_and_typed():
    """A state missing from the table would be a state with no rules — and `legal_targets` would read
    that as "nothing is allowed" for one caller and as "unconstrained" for the next person editing."""
    assert set(tr.ALLOWED) == set(MachineState)
    for src, targets in tr.ALLOWED.items():
        assert isinstance(targets, frozenset)
        for t in targets:
            assert isinstance(t, MachineState), f"{src} -> {t!r} is not a MachineState"
        assert src not in targets, f"{src} has a self-edge; 'still {src.value}' is not a transition"


def test_terminal_states_have_no_outgoing_edges_at_all():
    """succeeded and cancelled are genuinely over. Anything that moves one is a defect, and it gets its
    own reason so the runtime does not confuse it with an illegal move."""
    for st in TERMINAL_STATES:
        assert tr.legal_targets(st) == frozenset()
        for target in MachineState:
            r = propose_transition(st, target, guard_ctx=SENDABLE)
            assert r.rejected and r.reason == tr.TERMINAL_STATE, f"{st} -> {target} was permitted"


def test_recoverable_states_are_not_dead_ends():
    """Positive control for the test above: emptiness is a property of terminal states specifically, not
    the default everywhere. A blocked or failed mission that could not move would sit in the Decisions
    queue forever."""
    for st in RECOVERABLE_STATES:
        assert tr.legal_targets(st), f"{st} has no way out"
        assert propose_transition(st, MachineState.cancelled, guard_ctx=EMPTY).ok
        assert propose_transition(st, MachineState.preparing, guard_ctx=EMPTY).ok


def test_every_state_is_reachable_from_understanding():
    """Guards against the other failure mode of a hand-written table: one so restrictive that a real run
    can never legally arrive somewhere it observably ends up."""
    seen = {MachineState.understanding}
    frontier = [MachineState.understanding]
    while frontier:
        for nxt in tr.legal_targets(frontier.pop()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(MachineState), f"unreachable: {set(MachineState) - seen}"


def test_a_state_cannot_transition_to_itself():
    for st in MachineState:
        assert propose_transition(st, st, guard_ctx=SENDABLE).rejected


# --- the happy paths -----------------------------------------------------------------------------------


def test_the_full_legal_path_walks_end_to_end():
    """If every edge below were not permitted, every other assertion in this file would pass vacuously —
    a machine that refuses everything refuses the wrong things too."""
    walk = [
        (MachineState.understanding, MachineState.preparing, EMPTY),
        (MachineState.preparing, MachineState.awaiting_approval, DECIDED),
        (MachineState.awaiting_approval, MachineState.executing, SENDABLE),
        (MachineState.executing, MachineState.verifying, EMPTY),
        (MachineState.verifying, MachineState.succeeded, READ_BACK),
    ]
    for current, target, ctx in walk:
        r = propose_transition(current, target, guard_ctx=ctx)
        assert r.ok and r.reason == tr.ALLOW, f"{current} -> {target} rejected: {r.reason} {r.missing_slots}"
    assert walk[-1][1] in TERMINAL_STATES


def test_an_async_provider_may_wait_before_it_is_verified():
    """Some providers accept a write and finish it later. That is `waiting_external` — and it still has
    to pass through verification like everything else."""
    for current, target in ((MachineState.executing, MachineState.waiting_external),
                            (MachineState.waiting_external, MachineState.verifying)):
        assert propose_transition(current, target, guard_ctx=EMPTY).ok
    assert propose_transition(MachineState.waiting_external, MachineState.succeeded, guard_ctx=READ_BACK).rejected


def test_a_direct_action_may_execute_without_an_approval_stop():
    """One known operation, low risk: `preparing -> executing` exists so the router's direct_action path
    is legal. Without it the machine would force an approval prompt onto every trivial request."""
    assert propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=SENDABLE).ok


# --- succeeded: the rule that stops a fabricated receipt -------------------------------------------------


def test_awaiting_approval_cannot_jump_to_succeeded():
    r = propose_transition(MachineState.awaiting_approval, MachineState.succeeded, guard_ctx=READ_BACK)
    assert r.rejected and r.reason == tr.NOT_AN_EDGE


def test_executing_cannot_reach_succeeded_without_verifying():
    """`gmail.send_message` returning ok=True is exactly the evidence Bruce had when it was wrong about
    what it had done. A write is not a read-back."""
    r = propose_transition(MachineState.executing, MachineState.succeeded, guard_ctx=READ_BACK)
    assert r.rejected and r.reason == tr.NOT_AN_EDGE


def test_no_state_except_verifying_can_reach_succeeded():
    for st in MachineState:
        r = propose_transition(st, MachineState.succeeded, guard_ctx=READ_BACK)
        if st is MachineState.verifying:
            assert r.ok, "positive control: verification with read-back MUST be able to succeed"
        else:
            assert r.rejected, f"{st} reached succeeded"
    assert sum(MachineState.succeeded in t for t in tr.ALLOWED.values()) == 1


def test_the_verification_guard_does_not_depend_on_the_table_being_right(monkeypatch):
    """The table is data, and data gets edited by someone who needs a stuck run to finish. This proves a
    second, independent check: even with the edge added, an unverified success is refused.

    The reason is also the positive control — `not_from_verifying` instead of `not_an_edge` is only
    reachable if the patched edge really was consulted."""
    monkeypatch.setitem(tr.ALLOWED, MachineState.executing,
                        tr.ALLOWED[MachineState.executing] | {MachineState.succeeded})
    r = propose_transition(MachineState.executing, MachineState.succeeded, guard_ctx=READ_BACK)
    assert r.rejected and r.reason == tr.NOT_FROM_VERIFYING


def test_verifying_reaches_succeeded_only_with_read_back_evidence():
    ok = propose_transition(MachineState.verifying, MachineState.succeeded, guard_ctx=READ_BACK)
    assert ok.ok and ok.missing_slots == ()

    bare = propose_transition(MachineState.verifying, MachineState.succeeded, guard_ctx=EMPTY)
    assert bare.rejected and bare.reason == tr.UNVERIFIED
    assert set(bare.missing_slots) == {tr.VERIFIED_READ_BACK, tr.READ_BACK_ENTITY_ID}


def test_a_verified_flag_with_nothing_read_back_is_not_success():
    """Somebody's `verified=True` with no provider entity behind it is an opinion. The id is what a
    receipt and a later undo are built from, so its absence is named, not shrugged at."""
    claimed = GuardContext(verified_read_back=True, read_back_entity_id="   ")
    r = propose_transition(MachineState.verifying, MachineState.succeeded, guard_ctx=claimed)
    assert r.rejected and r.missing_slots == (tr.READ_BACK_ENTITY_ID,)


def test_a_mismatched_read_back_can_still_fail_the_run():
    """Verification must have somewhere honest to go when the read-back disagrees."""
    assert propose_transition(MachineState.verifying, MachineState.failed, guard_ctx=EMPTY).ok
    assert propose_transition(MachineState.verifying, MachineState.blocked, guard_ctx=EMPTY).ok


# --- executing: naming what is missing instead of asking again -------------------------------------------


def test_executing_names_the_missing_arguments():
    """The whole point of a machine-usable rejection: the next question is generated from these names,
    so a slot already filled is never asked for a second time."""
    ctx = GuardContext(capability="gmail.send_message", capability_available=True, availability_status="ok",
                       required_slots=("to", "subject", "body"), filled_slots=("to",),
                       authorization_open=True)
    r = propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=ctx)
    assert r.rejected and r.reason == tr.MISSING_SLOTS
    assert r.missing_slots == ("subject", "body")
    assert "to" not in r.missing_slots, "the recipient was given; asking again is the reported bug"


def test_missing_slots_carry_names_and_never_values():
    """A rejection is logged and handed to a model. Argument VALUES — a real address, a real body — must
    not travel with it. Paired with a positive control so this is not asserting an empty tuple."""
    args = {"to": "coach@school.example", "subject": "", "body": None}
    ctx = GuardContext(capability="gmail.send_message", capability_available=True,
                       required_slots=("to", "subject", "body"), filled_slots=tr.slots_filled(args),
                       authorization_open=True)
    r = propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=ctx)
    assert r.missing_slots == ("subject", "body")                 # positive control: blanks ARE detected
    assert "coach@school.example" not in " ".join(r.missing_slots) + r.reason


def test_slots_filled_treats_blank_as_absent():
    assert tr.slots_filled({"to": "a@b.example", "subject": "  ", "body": None, "thread_id": "t1"}) == \
        ("to", "thread_id")


def test_executing_requires_a_live_authorization():
    """Fully specified and reachable is not consent. The gate at the adapter would raise on this write;
    the machine refuses to enter the state in the first place."""
    unapproved = GuardContext(capability="gmail.send_message", capability_available=True,
                              availability_status="ok", required_slots=("to", "subject", "body"),
                              filled_slots=("to", "subject", "body"), authorization_open=False)
    r = propose_transition(MachineState.awaiting_approval, MachineState.executing, guard_ctx=unapproved)
    assert r.rejected and r.reason == tr.NO_AUTHORIZATION and r.missing_slots == (tr.AUTHORIZATION,)


def test_a_free_text_capability_is_diagnosed_as_invented_not_as_unavailable():
    """The transcript's second root cause: the model emitted "sending messages", which joins to no row in
    the registry. Collapsing that into "unavailable" would have the runtime tell a student to reconnect
    an account that was connected the whole time."""
    invented = GuardContext(capability="sending messages", capability_available=True,
                            required_slots=(), filled_slots=(), authorization_open=True)
    r = propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=invented)
    assert r.rejected and r.reason == tr.CAPABILITY_NOT_AN_OPERATION_ID
    assert r.missing_slots == (tr.CAPABILITY,)


def test_is_operation_id_accepts_registry_ids_and_rejects_prose():
    for good in ("gmail.send_message", "calendar.create_event", "calendar.update_event", "gmail.verify_sent"):
        assert tr.is_operation_id(good)
    for bad in ("sending messages", "send an email", "gmail", "", None, "gmail.", ".send_message",
                "gmail.send message", "gmail.send.message"):
        assert not tr.is_operation_id(bad), bad


def test_an_unreachable_capability_is_reported_before_arguments_are_collected():
    """Order matters as much as the checks do. Collecting a recipient and a subject line and only THEN
    admitting the account is not connected is the exchange that made Bruce feel broken."""
    disconnected = GuardContext(capability="gmail.send_message", capability_available=False,
                                availability_status="disconnected",
                                required_slots=("to", "subject", "body"), filled_slots=(),
                                authorization_open=False)
    r = propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=disconnected)
    assert r.rejected and r.reason == tr.CAPABILITY_UNAVAILABLE
    assert r.missing_slots == (tr.CAPABILITY,)
    assert "subject" not in r.missing_slots


def test_executing_is_permitted_once_every_precondition_holds():
    """Positive control for all four executing guards: with a real id, a reachable account, filled
    arguments and an authorization, the machine gets out of the way."""
    r = propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=SENDABLE)
    assert r.ok and r.reason == tr.ALLOW and r.missing_slots == ()


# --- awaiting_approval ------------------------------------------------------------------------------------


def test_awaiting_approval_requires_a_decision_to_exist():
    r = propose_transition(MachineState.preparing, MachineState.awaiting_approval, guard_ctx=EMPTY)
    assert r.rejected and r.reason == tr.NO_DECISION and r.missing_slots == (tr.DECISION_ID,)
    assert propose_transition(MachineState.preparing, MachineState.awaiting_approval, guard_ctx=DECIDED).ok


def test_a_blank_decision_id_is_not_a_decision():
    blank = GuardContext(decision_id="   ")
    assert propose_transition(MachineState.preparing, MachineState.awaiting_approval, guard_ctx=blank).rejected


def test_an_approval_may_be_edited_or_declined():
    """"no, friday" goes back to preparing; a decline is a cancel. Both must be legal or a student is
    trapped in front of a proposal they do not want."""
    assert propose_transition(MachineState.awaiting_approval, MachineState.preparing, guard_ctx=EMPTY).ok
    assert propose_transition(MachineState.awaiting_approval, MachineState.cancelled, guard_ctx=EMPTY).ok


# --- shape of a rejection, and agreement with the client contract -----------------------------------------


def test_rejections_are_machine_usable_rather_than_prose():
    """Every reason is a declared constant and every named slot is a bare identifier — the layer that
    writes the next question maps names, and must never have to parse a sentence."""
    probes = [
        propose_transition(MachineState.succeeded, MachineState.preparing, guard_ctx=EMPTY),
        propose_transition(MachineState.understanding, MachineState.executing, guard_ctx=EMPTY),
        propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=EMPTY),
        propose_transition(MachineState.preparing, MachineState.awaiting_approval, guard_ctx=EMPTY),
        propose_transition(MachineState.verifying, MachineState.succeeded, guard_ctx=EMPTY),
        propose_transition(MachineState.preparing, MachineState.executing, guard_ctx=SENDABLE),
    ]
    assert any(p.ok for p in probes) and any(p.rejected for p in probes)   # both branches really ran
    for p in probes:
        assert p.reason in tr.REASONS, p.reason
        assert isinstance(p.missing_slots, tuple)
        for slot in p.missing_slots:
            assert slot and slot.strip() == slot and " " not in slot, f"prose leaked into a slot: {slot!r}"
        assert p.ok is (p.reason == tr.ALLOW)


def test_cancel_is_legal_exactly_where_the_client_contract_offers_it():
    """Two modules, one answer. If `contract.available_actions` offers Cancel in a state the machine
    refuses to leave, a student taps a button that silently does nothing."""
    for st in MachineState:
        offered = Action.cancel_mission in available_actions(st)
        reachable = MachineState.cancelled in tr.legal_targets(st)
        assert offered == reachable, f"{st}: contract offers cancel={offered}, machine allows={reachable}"


def test_nothing_is_cancelled_while_an_external_call_is_in_flight():
    """"Cancelled" would be a claim about a provider we do not control. Undo after verification is the
    honest path, and `contract` already refuses to offer cancel here."""
    for st in (MachineState.executing, MachineState.waiting_external, MachineState.verifying):
        assert propose_transition(st, MachineState.cancelled, guard_ctx=EMPTY).rejected
