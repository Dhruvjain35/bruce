"""M2 — the orchestrator's truth table. No model, no database, no network: derivation is a pure function
of an understood turn plus runtime facts, and if any test here needed a model the split would be broken.

The measured defect this closes: the model was scored 0.615 on execution class while scoring 0.95 on
actionability. It understood the goals and could not see the leases, connections, and notification
guarantees that decide how a goal runs. Those live here.
"""

from __future__ import annotations

import itertools

import pytest

from bruce_engine import execution_derivation as ed
from bruce_engine.semantic_contracts import (Actionability, Family, OperationFamily, SemanticTurn,
                                             TurnContext, TurnRole)

CAL = "calendar.create_event"
SEND = "gmail.send_message"
FIND_REPLY = "gmail.find_reply"

LIVE = TurnContext(live_families=frozenset({Family.calendar, Family.communication}),
                   live_capabilities=frozenset({CAL, "calendar.update_event", "calendar.delete_event",
                                                SEND, FIND_REPLY}))


def turn(**kw) -> SemanticTurn:
    base = dict(turn_role=TurnRole.new_goal, actionability=Actionability.executable,
                domain_candidates=(Family.communication,), operation_family=OperationFamily.send,
                confidence=0.9)
    base.update(kw)
    return SemanticTurn(**base)


# --- the executable path -------------------------------------------------------------------------------

def test_send_is_a_direct_action_on_the_send_capability():
    d = ed.derive(turn(), LIVE)
    assert d.execution_class == "direct_action"
    assert d.domain == "gmail" and d.capabilities == (SEND,)
    assert d.action == "send"


def test_calendar_create_is_a_direct_action():
    d = ed.derive(turn(domain_candidates=(Family.calendar,), operation_family=OperationFamily.create), LIVE)
    assert (d.execution_class, d.domain, d.capabilities) == ("direct_action", "calendar", (CAL,))


def test_monitoring_a_reply_is_a_background_mission_not_a_send():
    """The one the model got wrong six times: it read 'let me know when they reply' correctly and had no
    way to know that outliving the turn means a durable run."""
    d = ed.derive(turn(actionability=Actionability.durable_monitoring), LIVE)
    assert d.execution_class == "background_mission"
    assert d.capabilities == (SEND, FIND_REPLY)      # verified send in-turn, then pure monitoring


def test_a_bare_monitor_operation_is_also_a_mission():
    d = ed.derive(turn(operation_family=OperationFamily.monitor), LIVE)
    assert d.execution_class == "background_mission"


def test_remembering_a_fact_needs_no_provider():
    d = ed.derive(turn(domain_candidates=(Family.memory,), operation_family=OperationFamily.remember), LIVE)
    assert (d.execution_class, d.domain, d.action) == ("direct_action", "world", "remember")


# --- not actionable ------------------------------------------------------------------------------------

@pytest.mark.parametrize("act", [Actionability.no_action, Actionability.information_only])
def test_chat_is_only_reached_by_a_positive_reading(act):
    d = ed.derive(turn(turn_role=TurnRole.conversation, actionability=act), LIVE)
    assert d.execution_class == "fast_conversation" and d.rule == "not_actionable"


# --- uncertainty asks, it does not guess ---------------------------------------------------------------

def test_low_confidence_asks_rather_than_silently_chatting():
    """THE regression that made misses invisible: an unsure read used to become fast_conversation, which is
    indistinguishable from a correct chat classification. It must become a question."""
    d = ed.derive(turn(confidence=0.2), LIVE)
    assert d.execution_class == "foreground_agent"
    assert d.needs_clarification and d.clarification_reason == "low_confidence"


def test_ambiguous_actionability_asks():
    d = ed.derive(turn(actionability=Actionability.ambiguous), LIVE)
    assert d.needs_clarification and d.execution_class == "foreground_agent"


def test_the_operation_breaks_a_two_family_tie_without_asking():
    """Measured: 'write to my teacher about the deadline' came back as {communication, calendar} because a
    deadline is date-shaped. Only one of those can send, so the runtime already knows the answer and
    asking would be theatre."""
    d = ed.derive(turn(domain_candidates=(Family.communication, Family.calendar),
                       operation_family=OperationFamily.send), LIVE)
    assert d.execution_class == "direct_action" and d.domain == "gmail"


def test_the_tie_break_works_in_the_other_direction_too():
    """'stick english essay due monday on there' -> {calendar, communication} + create. Only calendar
    natively creates."""
    d = ed.derive(turn(domain_candidates=(Family.calendar, Family.communication),
                       operation_family=OperationFamily.create), LIVE)
    assert d.execution_class == "direct_action" and d.domain == "calendar"


def test_a_genuine_tie_still_asks():
    """Both families support `find`, so nothing in the runtime resolves it and the student must."""
    d = ed.derive(turn(domain_candidates=(Family.calendar, Family.communication),
                       operation_family=OperationFamily.find), LIVE)
    assert d.needs_clarification and d.clarification_reason == "multiple_domains"


def test_drafting_an_email_takes_the_send_route():
    """'draft an email to my counselor' reads as create. Bruce has no standalone draft capability — one
    compose-show-approve-send route — so create in this family IS that route, not a dead end."""
    d = ed.derive(turn(operation_family=OperationFamily.create), LIVE)
    assert d.execution_class == "direct_action" and d.capabilities == (SEND,)


def test_no_family_asks():
    d = ed.derive(turn(domain_candidates=()), LIVE)
    assert d.needs_clarification and d.clarification_reason == "no_domain"


def test_unknown_family_is_not_mistaken_for_a_resolved_one():
    d = ed.derive(turn(domain_candidates=(Family.unknown,)), LIVE)
    assert d.needs_clarification


# --- capability truth ----------------------------------------------------------------------------------

def test_a_goal_at_an_unconnected_family_never_becomes_an_action():
    nothing_live = TurnContext()
    d = ed.derive(turn(), nothing_live)
    assert d.execution_class == "fast_conversation" and d.rule == "family_not_live"
    assert not d.capabilities            # nothing is proposed against a tool that cannot run


def test_a_family_that_is_live_but_lacks_the_operation_is_reported_honestly():
    """calendar.search_events is declared live=False. Searching must not be approximated with create."""
    d = ed.derive(turn(domain_candidates=(Family.calendar,), operation_family=OperationFamily.find), LIVE)
    assert d.execution_class == "fast_conversation" and d.rule == "operation_not_live"


def test_an_operation_that_does_not_exist_in_the_family_asks():
    d = ed.derive(turn(domain_candidates=(Family.calendar,), operation_family=OperationFamily.send), LIVE)
    assert d.needs_clarification and d.clarification_reason == "operation_not_in_family"


# --- conversation structure outranks sentence shape ----------------------------------------------------

def test_a_decision_response_belongs_to_the_run_that_asked():
    ctx = TurnContext(live_families=LIVE.live_families, live_capabilities=LIVE.live_capabilities,
                      pending_decision_id="dec-1", active_run_domain="calendar")
    d = ed.derive(turn(turn_role=TurnRole.decision_response, actionability=Actionability.executable), ctx)
    assert d.execution_class == "direct_action" and d.decision_id == "dec-1"


def test_a_decision_response_with_nothing_pending_does_not_invent_work():
    d = ed.derive(turn(turn_role=TurnRole.decision_response,
                       actionability=Actionability.no_action), LIVE)
    assert d.execution_class == "fast_conversation"


def test_cancellation_with_nothing_in_flight_is_just_talk():
    d = ed.derive(turn(turn_role=TurnRole.cancellation), LIVE)
    assert d.execution_class == "fast_conversation"


def test_cancellation_against_live_work_cancels_it():
    ctx = TurnContext(active_run_id="run-9", active_run_domain="gmail")
    d = ed.derive(turn(turn_role=TurnRole.cancellation), ctx)
    assert d.execution_class == "direct_action" and d.continuation_run_id == "run-9"


def test_a_bare_reference_resolves_against_open_work():
    ctx = TurnContext(active_run_id="run-2", active_run_domain="gmail")
    d = ed.derive(turn(turn_role=TurnRole.reference_only), ctx)
    assert d.continuation_run_id == "run-2" and d.execution_class == "fast_conversation"


def test_a_bare_reference_with_nothing_open_asks_what_it_refers_to():
    d = ed.derive(turn(turn_role=TurnRole.reference_only), LIVE)
    assert d.needs_clarification and d.clarification_reason == "referent_unknown"


def test_a_correction_carries_the_run_it_repairs():
    ctx = TurnContext(live_families=LIVE.live_families, live_capabilities=LIVE.live_capabilities,
                      active_run_id="run-7", active_run_domain="calendar")
    d = ed.derive(turn(turn_role=TurnRole.correction, domain_candidates=(Family.calendar,),
                       operation_family=OperationFamily.update), ctx)
    assert d.correction_of_run_id == "run-7" and d.execution_class == "direct_action"


# --- totality ------------------------------------------------------------------------------------------

def test_derivation_is_total_over_the_whole_vocabulary():
    """Every combination must yield a decision. A crash in routing is an outage, and the cross product is
    small enough to simply exhaust rather than argue about."""
    contexts = [TurnContext(), LIVE,
                TurnContext(pending_decision_id="d", active_run_id="r", active_run_domain="calendar")]
    classes = {"fast_conversation", "direct_action", "foreground_agent", "background_mission"}
    n = 0
    for role, act, fam, op, ctx in itertools.product(
            TurnRole, Actionability, Family, OperationFamily, contexts):
        d = ed.derive(SemanticTurn(turn_role=role, actionability=act, domain_candidates=(fam,),
                                   operation_family=op, confidence=0.9), ctx)
        assert d.execution_class in classes
        n += 1
    assert n == 7 * 5 * 7 * 9 * 3


def test_nothing_executes_against_a_capability_that_is_not_live():
    """The invariant that matters most: whatever the model said, a derived action never names a capability
    the runtime did not confirm."""
    for role, act, fam, op in itertools.product(TurnRole, Actionability, Family, OperationFamily):
        d = ed.derive(SemanticTurn(turn_role=role, actionability=act, domain_candidates=(fam,),
                                   operation_family=op, confidence=0.95), LIVE)
        if d.execution_class in ("direct_action", "background_mission"):
            assert all(c in LIVE.live_capabilities for c in d.capabilities)
