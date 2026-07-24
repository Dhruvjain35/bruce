"""Stage-1 router-model contract (Phase C prep) — the interface is sound: a model response lifts into a
RouterDecision with source=router_model, the policy gate rejects low-confidence/over-broad responses, and
the confidence-calibration harness (ECE) computes correctly. No model call — pure contract validation."""

from __future__ import annotations

from bruce_engine.router_model_contract import (CalibrationPoint, RouterModelPolicy, RouterModelResponse,
                                                expected_calibration_error)
from bruce_engine.runtime_contracts import ExecutionClass, GoalAction


def test_response_lifts_to_router_decision():
    r = RouterModelResponse(execution_class=ExecutionClass.direct_action, action=GoalAction.update,
                            domain="calendar", candidate_capabilities=("calendar.update_event",),
                            confidence=0.82, needs_deeper_planning=False)
    d = r.to_router_decision()
    assert d.execution_class is ExecutionClass.direct_action and d.action is GoalAction.update
    assert d.domain == "calendar" and d.source == "router_model" and d.confidence == 0.82


def test_policy_gate_rejects_low_confidence_and_over_broad():
    p = RouterModelPolicy(min_confidence=0.55, max_candidates=4)
    good = RouterModelResponse(execution_class=ExecutionClass.fast_conversation, confidence=0.7)
    low = RouterModelResponse(execution_class=ExecutionClass.fast_conversation, confidence=0.3)
    broad = RouterModelResponse(execution_class=ExecutionClass.foreground_agent, confidence=0.9,
                                candidate_capabilities=tuple(f"c{i}" for i in range(5)))
    assert p.accepts(good) is True
    assert p.accepts(low) is False           # low confidence -> deterministic fallback
    assert p.accepts(broad) is False         # over-broad candidate set -> reject


def test_ece_perfect_and_miscalibrated():
    # perfectly calibrated: confidence 1.0 always correct, 0.0 always wrong -> ECE 0
    perfect = [CalibrationPoint(1.0, True)] * 10 + [CalibrationPoint(0.0, False)] * 10
    assert expected_calibration_error(perfect) < 1e-9
    # badly miscalibrated: claims 0.99 but always wrong -> ECE ~0.99
    overconfident = [CalibrationPoint(0.99, False)] * 20
    assert expected_calibration_error(overconfident) > 0.9
    assert expected_calibration_error([]) == 0.0
