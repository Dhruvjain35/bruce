"""Compact Stage-1 router model (Phase C) — the gates, with a fake provider (no live model call): a trusted
response only when it validates + clears the confidence gate; a HARD timeout, any error, an invalid enum, or
low confidence ALL fall back to the deterministic default; the request is compact (no tool schemas); and the
model is NEVER called when deterministic Stage 0 already resolved the turn."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from bruce_engine import fast_router, router_model
from bruce_engine.router_model_contract import EntityRef, RouterModelRequest, RouterModelResponse
from bruce_engine.runtime_contracts import ExecutionClass, GoalAction


def _run(c):
    return asyncio.run(c)


class FakeProvider:
    def __init__(self, response=None, *, raises=False, delay=0.0):
        self._response, self._raises, self._delay = response, raises, delay
        self.called = False

    async def classify(self, request):
        self.called = True
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise RuntimeError("model down: move chess to 9pm")   # error text may echo the message
        return self._response


def _resp(ec=ExecutionClass.direct_action, action=GoalAction.update, conf=0.9):
    return RouterModelResponse(execution_class=ec, action=action, domain="calendar", confidence=conf)


def _req(text="what should i do about the group project"):
    return RouterModelRequest(text=text, live_capability_families=("calendar",))


def test_stage1_ok_returns_trusted_response():
    out = _run(router_model.route_stage1(_req(), provider=FakeProvider(_resp())))
    assert out.response is not None and out.reason == "ok" and out.fell_back is False
    d = out.response.to_router_decision()
    assert d.source == "router_model" and d.execution_class is ExecutionClass.direct_action


def test_stage1_hard_timeout_falls_back():
    out = _run(router_model.route_stage1(_req(), provider=FakeProvider(_resp(), delay=0.5), timeout_s=0.05))
    assert out.response is None and out.reason == "timeout" and out.fell_back is True


def test_stage1_error_falls_back():
    out = _run(router_model.route_stage1(_req(), provider=FakeProvider(raises=True)))
    assert out.response is None and out.reason == "error"


def test_stage1_low_confidence_falls_back():
    out = _run(router_model.route_stage1(_req(), provider=FakeProvider(_resp(conf=0.3))))
    assert out.response is None and out.reason == "low_confidence"


def test_invalid_enum_is_rejected():
    with pytest.raises(ValueError):
        router_model._to_response(router_model._RouterOut(execution_class="teleport"))
    with pytest.raises(ValueError):
        router_model._to_response(router_model._RouterOut(execution_class="direct_action", action="frobnicate"))


def test_request_is_compact_no_tool_schemas():
    req = RouterModelRequest(text="move chess to 9pm", live_capability_families=("calendar",),
                             top_entities=(EntityRef("1", "chess club", "Jul 25 3pm"),))
    body = router_model._render_request(req)
    assert "move chess to 9pm" in body and "chess club" in body          # what the model needs
    assert "arg_schema" not in body and "requires_scope" not in body     # NO tool schemas leak to the router
    assert "start" not in body.lower() or "Jul" in body                   # entity is a title, not a full row


def test_no_model_call_when_stage0_resolves():
    """The load-bearing efficiency guarantee: a deterministically-resolved turn never invokes the model."""
    spy = FakeProvider(_resp())
    d, _t = _run(fast_router.route(uuid4(), "add dentist appt tmr at 3pm", model=spy))
    assert d.execution_class is ExecutionClass.direct_action and d.source == "deterministic"
    assert spy.called is False


def test_stage1_used_only_when_stage0_inconclusive():
    """A pure chat text (no Stage-0 signal) reaches Stage 1 -> the injected model IS consulted."""
    spy = FakeProvider(_resp(ec=ExecutionClass.fast_conversation, action=GoalAction.answer))
    d, _t = _run(fast_router.route(uuid4(), "lol that's so real honestly", model=spy))
    assert spy.called is True and d.source == "router_model"


# --- metrics harness machinery (validated with a fake; runnable against the real model) -----------

class LabelEcho:
    """A perfect fake: returns each example's labeled class at high confidence -> accuracy 1.0, ece ~0."""
    def __init__(self, by_text):
        self._by_text = by_text
    async def classify(self, request):
        ec = ExecutionClass(self._by_text[request.text])
        return RouterModelResponse(execution_class=ec, action=None, domain="calendar", confidence=0.95)


class AlwaysChat:
    async def classify(self, request):
        return RouterModelResponse(execution_class=ExecutionClass.fast_conversation,
                                   action=GoalAction.answer, domain=None, confidence=0.9)


def test_evaluate_stage1_computes_metrics():
    ex = [{"text": "a", "expected_class": "direct_action"},
          {"text": "b", "expected_class": "fast_conversation"},
          {"text": "c", "expected_class": "background_mission"}]
    m = _run(router_model.evaluate_stage1(ex, provider=LabelEcho({e["text"]: e["expected_class"] for e in ex})))
    assert m.n == 3 and m.exec_class_accuracy == 1.0 and m.fallback_rate == 0.0 and m.timeout_rate == 0.0
    assert m.ece < 0.2                                          # confident + correct -> well-calibrated

    # a provider that always says chat scores only the chat example
    m2 = _run(router_model.evaluate_stage1(ex, provider=AlwaysChat()))
    assert abs(m2.exec_class_accuracy - (1 / 3)) < 1e-9


def test_evaluate_stage1_counts_fallbacks():
    ex = [{"text": "x", "expected_class": "direct_action"}]

    class LowConf:
        async def classify(self, request):
            return RouterModelResponse(execution_class=ExecutionClass.direct_action, confidence=0.1)

    m = _run(router_model.evaluate_stage1(ex, provider=LowConf()))
    assert m.fallback_rate == 1.0 and m.exec_class_accuracy == 0.0
