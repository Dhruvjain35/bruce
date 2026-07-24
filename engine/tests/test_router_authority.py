"""Router authority harness (Phase A) — the canary bucketing is deterministic + kill-switchable, and the
reasoner is skipped ONLY on the lanes the router provably owns (deterministic text-action, no perception),
never on chat / perception / attachments / low-confidence. The synthetic decision is valid + persistable."""

from __future__ import annotations

from uuid import UUID, uuid4

from bruce_engine import router_authority as ra
from bruce_engine.runtime_contracts import ExecutionClass, GoalAction, RouterDecision


def _rd(ec, action, *, source="deterministic", confidence=1.0, decision_id=None):
    return RouterDecision(execution_class=ec, action=action, source=source, confidence=confidence,
                          decision_id=decision_id)


# --- canary bucketing + kill switch ---------------------------------------------------------------

def test_bucket_is_deterministic(monkeypatch):
    monkeypatch.setenv("BRUCE_ROUTER_AUTHORITY_PCT", "100")
    uid = uuid4()
    assert ra.is_authoritative(uid) == ra.is_authoritative(uid)     # stable across calls


def test_pct_bounds(monkeypatch):
    uid = uuid4()
    monkeypatch.setenv("BRUCE_ROUTER_AUTHORITY_PCT", "0")
    assert ra.is_authoritative(uid) is False                        # 0% -> nobody
    monkeypatch.setenv("BRUCE_ROUTER_AUTHORITY_PCT", "100")
    assert ra.is_authoritative(uid) is True                         # 100% -> everybody


def test_pct_partial_splits_users(monkeypatch):
    monkeypatch.setenv("BRUCE_ROUTER_AUTHORITY_PCT", "50")
    ids = [uuid4() for _ in range(400)]
    on = sum(1 for u in ids if ra.is_authoritative(u))
    assert 120 < on < 280                                           # roughly half, deterministic per user


def test_kill_switch_forces_legacy(monkeypatch):
    monkeypatch.setenv("BRUCE_ROUTER_AUTHORITY_PCT", "100")
    monkeypatch.setenv("BRUCE_ROUTER_AUTHORITY_OFF", "true")
    assert ra.authority_pct() == 0
    assert ra.is_authoritative(uuid4()) is False


def test_bad_pct_is_zero(monkeypatch):
    monkeypatch.setenv("BRUCE_ROUTER_AUTHORITY_PCT", "not-a-number")
    assert ra.authority_pct() == 0


# --- skippable lanes ------------------------------------------------------------------------------

def test_deterministic_text_action_lanes_are_skippable():
    for action in (GoalAction.update, GoalAction.delete, GoalAction.repair, GoalAction.remember):
        d = _rd(ExecutionClass.direct_action, action)
        assert ra.reasoner_skippable(d, has_attachments=False, has_reply_ref=False) is True, action


def test_approval_continuation_is_skippable_only_with_decision_id():
    with_id = _rd(ExecutionClass.direct_action, GoalAction.create, decision_id="m1")
    without = _rd(ExecutionClass.direct_action, GoalAction.create)
    assert ra.reasoner_skippable(with_id, has_attachments=False, has_reply_ref=False) is True
    assert ra.reasoner_skippable(without, has_attachments=False, has_reply_ref=False) is False   # fresh schedule needs perception


def test_chat_and_perception_lanes_are_not_skippable():
    assert not ra.reasoner_skippable(_rd(ExecutionClass.fast_conversation, GoalAction.answer),
                                     has_attachments=False, has_reply_ref=False)
    assert not ra.reasoner_skippable(_rd(ExecutionClass.foreground_agent, GoalAction.create),
                                     has_attachments=False, has_reply_ref=False)
    assert not ra.reasoner_skippable(_rd(ExecutionClass.background_mission, GoalAction.plan),
                                     has_attachments=False, has_reply_ref=False)


def test_attachments_or_reply_ref_force_the_reasoner():
    d = _rd(ExecutionClass.direct_action, GoalAction.update)
    assert not ra.reasoner_skippable(d, has_attachments=True, has_reply_ref=False)
    assert not ra.reasoner_skippable(d, has_attachments=False, has_reply_ref=True)


def test_low_confidence_or_model_sourced_is_not_skippable():
    assert not ra.reasoner_skippable(_rd(ExecutionClass.direct_action, GoalAction.update, confidence=0.5),
                                     has_attachments=False, has_reply_ref=False)
    assert not ra.reasoner_skippable(_rd(ExecutionClass.direct_action, GoalAction.update, source="router_model"),
                                     has_attachments=False, has_reply_ref=False)


# --- synthetic decision ---------------------------------------------------------------------------

def test_synthetic_decision_is_valid_and_persistable():
    d = ra.synthetic_decision(_rd(ExecutionClass.direct_action, GoalAction.update))
    assert d.intent.value and d.response_type.value
    assert d.model_dump(mode="json")                                # persist_assistant_turn does this
    appr = ra.synthetic_decision(_rd(ExecutionClass.direct_action, GoalAction.create, decision_id="m1"))
    assert appr.intent.value == "approval"
