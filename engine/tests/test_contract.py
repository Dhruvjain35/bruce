"""The mission contract — the server decides what a client may do, never the client.

These tests exist because the failure they prevent is external and irreversible: a client that
infers "Approve" from a phase string will eventually offer Approve on a mission that has already
executed, and the tap races a real calendar write on a real student's calendar. So the permitted
set is asserted here, per state, as a server property.
"""

from __future__ import annotations

import pytest

from bruce_engine.contract import (
    DISPLAY,
    RECOVERABLE_STATES,
    TERMINAL_STATES,
    Action,
    MachineState,
    ObjectRefs,
    VerificationState,
    available_actions,
    build_contract,
)

REFS = ObjectRefs(mission_id="m1", source_id="s1")


def test_every_state_has_a_concise_display_string():
    for st in MachineState:
        assert st in DISPLAY and DISPLAY[st]
        assert len(DISPLAY[st]) <= 40, "display state must be one concise line, not a paragraph"


def test_display_strings_carry_no_fake_certainty_or_chain_of_thought():
    """No invented percentages, no 'thinking…', no emoji status."""
    for text in DISPLAY.values():
        assert "%" not in text
        assert "think" not in text.lower()
        assert text.isascii(), f"no emoji/decorative status: {text!r}"


def test_approve_is_offered_only_while_awaiting_approval():
    """The core safety property: a stale client cannot get approve authorized after execution."""
    for st in MachineState:
        acts = available_actions(st, verification=VerificationState.verified)
        if st == MachineState.awaiting_approval:
            assert Action.approve_calendar_action in acts
        else:
            assert Action.approve_calendar_action not in acts, f"approve leaked into {st}"


def test_undo_requires_a_verified_execution():
    """Offering undo on an unverified/mismatched execution invites deleting something we cannot
    prove exists — and the delete could hit the wrong state."""
    assert Action.undo in available_actions(MachineState.succeeded, verification=VerificationState.verified)
    for v in (VerificationState.pending, VerificationState.unverified,
              VerificationState.mismatch, VerificationState.not_applicable):
        assert Action.undo not in available_actions(MachineState.succeeded, verification=v), v


def test_retry_is_never_offered_on_a_verified_success():
    """Retrying a verified external action is how a student gets double-booked."""
    assert Action.retry not in available_actions(
        MachineState.succeeded, verification=VerificationState.verified
    )


def test_no_actions_are_offered_mid_execution():
    """The external call is in flight. 'Cancelled' would be a claim about state we do not control."""
    for st in (MachineState.executing, MachineState.waiting_external, MachineState.verifying):
        acts = available_actions(st)
        assert Action.cancel_mission not in acts
        assert Action.approve_calendar_action not in acts


def test_terminal_states_offer_no_cancel():
    """succeeded/cancelled are genuinely over — there is nothing to stop."""
    for st in TERMINAL_STATES:
        assert Action.cancel_mission not in available_actions(st)


def test_recoverable_states_are_not_terminal_and_offer_a_way_out():
    """blocked/failed must NOT be terminal: both are retryable, and a student must be able to give
    up on one. Treating them as terminal is how a Decisions queue gets permanently stuck."""
    for st in RECOVERABLE_STATES:
        assert st not in TERMINAL_STATES
        acts = available_actions(st)
        assert Action.retry in acts and Action.cancel_mission in acts


def test_blocked_and_failed_offer_retry():
    for st in (MachineState.blocked, MachineState.failed):
        assert Action.retry in available_actions(st)


def test_open_source_is_offered_whenever_a_source_exists():
    """Grounding must always be inspectable — that is the proof, not decoration."""
    acts = available_actions(MachineState.succeeded, verification=VerificationState.verified, has_source=True)
    assert Action.open_source in acts


def test_contract_marks_requires_approval_only_in_awaiting_approval():
    for st in MachineState:
        c = build_contract(state=st, updated_at="2026-07-17T00:00:00Z", refs=REFS)
        assert c.requires_approval == (st == MachineState.awaiting_approval)


def test_success_is_reported_only_alongside_a_verification_state():
    """succeeded must never be paired with 'not_applicable' for a calendar mission — the API layer
    supplies the real verification; this asserts the contract carries it rather than implying it."""
    c = build_contract(
        state=MachineState.succeeded, updated_at="t", refs=REFS, verification=VerificationState.verified
    )
    assert c.verification == VerificationState.verified
    assert c.display_state == "Added and verified"


def test_mismatch_is_not_success_shaped():
    """A read-back mismatch must not render as a success anywhere in the contract."""
    c = build_contract(
        state=MachineState.failed, updated_at="t", refs=REFS,
        verification=VerificationState.mismatch, blocking_reason="read-back did not match",
    )
    assert c.verification == VerificationState.mismatch
    assert "verified" not in c.display_state.lower()
    assert c.blocking_reason


def test_blocking_reason_is_carried_for_blocked_missions():
    c = build_contract(
        state=MachineState.blocked, updated_at="t", refs=REFS,
        blocking_reason="the model provider is not available for this account",
    )
    assert c.blocking_reason and c.state == MachineState.blocked


def test_refs_carry_stable_ids_for_every_canonical_object():
    refs = ObjectRefs(
        mission_id="m", source_id="s", span_ids=["sp"], task_ids=["t"], decision_id="d",
        calendar_proposal_ids=["c"], external_event_id="e", receipt_id="r",
    )
    c = build_contract(state=MachineState.succeeded, updated_at="t", refs=refs,
                       verification=VerificationState.verified)
    d = c.model_dump()
    for key in ("mission_id", "source_id", "span_ids", "task_ids", "decision_id",
                "calendar_proposal_ids", "external_event_id", "receipt_id"):
        assert key in d["refs"]


def test_actions_are_a_closed_set_the_client_can_switch_on():
    """No free-form strings: an unknown action in a response would be unrenderable."""
    for st in MachineState:
        for a in available_actions(st, verification=VerificationState.verified, has_source=True):
            assert isinstance(a, Action)
