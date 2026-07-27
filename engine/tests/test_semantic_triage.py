"""M2 — Stage-1 triage: schema shape, failure honesty, and retry policy. No real model call anywhere here.

Two of these are structural assertions about the schema itself rather than about behaviour. That is
deliberate: the defect being fixed was the SHAPE of the question, and a shape regression would otherwise
only show up as a slow drift in an eval score.
"""

from __future__ import annotations

import asyncio

import pytest

from bruce_engine import router_model, semantic_triage
from bruce_engine.semantic_contracts import (Actionability, Family, OperationFamily, SemanticTurn,
                                             TriageFailure, TurnRole)


# --- the schema must not ask for orchestration ---------------------------------------------------------

def test_the_semantic_schema_contains_no_execution_class():
    """The whole M2 premise. If an execution class reappears in the semantic output, the model is being
    asked to orchestrate again."""
    fields = set(semantic_triage._TriageOut.model_fields)
    assert "execution_class" not in fields
    forbidden = {"direct_action", "foreground_agent", "background_mission", "fast_conversation"}
    schema = str(semantic_triage._TriageOut.model_json_schema())
    assert not (forbidden & set(schema.split('"'))), "an execution class leaked into the semantic schema"


def test_understanding_fields_are_generated_before_anything_else():
    """Structured output is produced left to right, so field order is a real constraint on what the model
    has committed to before it answers. Role and actionability must lead."""
    order = list(semantic_triage._TriageOut.model_fields)
    assert order[0] == "turn_role" and order[1] == "actionability"
    assert order.index("domain_candidates") < order.index("confidence")


def test_the_prompt_names_no_provider():
    """The semantic layer describes meaning; Gmail and Google Calendar are the orchestrator's vocabulary."""
    system = semantic_triage._SYSTEM.lower()
    for provider in ("gmail", "google", "outlook", "canvas", "drive"):
        assert provider not in system


# --- coercion must not discard a good read -------------------------------------------------------------

def test_an_unrecognised_optional_field_does_not_destroy_the_reading():
    """'A response must not be discarded because an optional orchestration field failed validation.'"""
    class Raw:
        turn_role = "new_goal"
        actionability = "executable"
        domain_candidates = ["communication", "not_a_family"]
        operation_family = "teleport"
        confidence = 0.88
        needs_frontier = False

    turn = semantic_triage.to_semantic_turn(Raw())
    assert turn.turn_role is TurnRole.new_goal
    assert turn.actionability is Actionability.executable
    assert turn.domain_candidates == (Family.communication, Family.unknown)
    assert turn.operation_family is OperationFamily.none      # degraded, not fatal
    assert turn.confidence == 0.88


# --- failure honesty and retry policy ------------------------------------------------------------------

class FakeTriage:
    def __init__(self, *, raises=None, delay=0.0, turn=None):
        self.raises, self.delay, self.turn, self.calls = raises, delay, turn, 0

    async def read(self, body):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises and self.calls <= getattr(self, "fail_times", 1):
            raise self.raises
        return self.turn or SemanticTurn(turn_role=TurnRole.new_goal,
                                         actionability=Actionability.executable,
                                         domain_candidates=(Family.communication,),
                                         operation_family=OperationFamily.send, confidence=0.9)


@pytest.mark.asyncio
async def test_a_timeout_is_reported_as_a_timeout_and_never_retried():
    """Retrying a timeout doubles exactly the latency the deadline exists to protect."""
    fake = FakeTriage(delay=0.2)
    out = await semantic_triage.triage("MESSAGE: hi", provider=fake, timeout_s=0.02)
    assert isinstance(out, TriageFailure) and out.reason == "timeout"
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_one_transport_failure_is_retried_once():
    fake = FakeTriage(raises=ConnectionError("reset"))
    out = await semantic_triage.triage("MESSAGE: hi", provider=fake, timeout_s=2)
    assert isinstance(out, SemanticTurn) and fake.calls == 2


@pytest.mark.asyncio
async def test_a_schema_failure_is_not_retried():
    fake = FakeTriage(raises=ValueError("bad json"))
    fake.fail_times = 5
    out = await semantic_triage.triage("MESSAGE: hi", provider=fake, timeout_s=2)
    assert isinstance(out, TriageFailure) and out.reason == "invalid_schema"
    assert fake.calls == 1                       # the same answer twice is not worth the latency


@pytest.mark.asyncio
async def test_a_low_confidence_read_is_preserved_rather_than_thrown_away():
    unsure = SemanticTurn(turn_role=TurnRole.new_goal, actionability=Actionability.ambiguous,
                          confidence=0.2)
    out = await semantic_triage.triage("MESSAGE: hm", provider=FakeTriage(turn=unsure), timeout_s=2,
                                       min_confidence=0.5)
    assert isinstance(out, TriageFailure) and out.reason == "low_confidence"
    assert out.partial is unsure                 # the derivation still gets to ask a question about it


# --- the provider seam ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_router_provider_turns_meaning_into_an_execution_class():
    from bruce_engine.router_model_contract import RouterModelRequest

    monitor = SemanticTurn(turn_role=TurnRole.new_goal, actionability=Actionability.durable_monitoring,
                           domain_candidates=(Family.communication,),
                           operation_family=OperationFamily.send, confidence=0.93)
    provider = router_model.SemanticRouterModel(triage_provider=FakeTriage(turn=monitor))

    async def fake_ctx(_request):
        from bruce_engine.semantic_contracts import TurnContext
        return TurnContext(live_families=frozenset({Family.communication}),
                           live_capabilities=frozenset({"gmail.send_message", "gmail.find_reply"}))

    provider._context = fake_ctx
    resp = await provider.classify(RouterModelRequest(text="email coach and tell me when he replies"))
    assert resp.execution_class.value == "background_mission"
    assert resp.domain == "gmail"


@pytest.mark.asyncio
async def test_a_failed_triage_raises_so_the_router_falls_back_with_a_reason(monkeypatch):
    """A dead Stage 1 must reach the deterministic default through a NAMED failure, never by quietly
    producing a chat classification that looks like a correct one."""
    from bruce_engine.router_model_contract import RouterModelRequest

    monkeypatch.setattr(semantic_triage, "TRIAGE_TIMEOUT_S", 0.02)
    provider = router_model.SemanticRouterModel(triage_provider=FakeTriage(delay=0.3))
    with pytest.raises(RuntimeError, match="triage_failed:timeout"):
        await provider.classify(RouterModelRequest(text="hi"))


def test_the_agent_is_built_once_so_connections_are_reused(monkeypatch):
    """Measured: a fresh Agent per call meant a fresh HTTP client per call, and the resulting TLS
    handshake on the first turn took 3003ms — over the 3s deadline, and the run's only fallback."""
    built = []

    class FakeAgent:
        def __init__(self, *a, **kw):
            built.append(1)

    monkeypatch.setattr(semantic_triage, "Agent", FakeAgent)
    monkeypatch.setattr(semantic_triage.llm, "routing_model", lambda: object())
    p = semantic_triage.SemanticTriage()
    p._agent(), p._agent(), p._agent()
    assert len(built) == 1


def test_the_default_provider_is_shared_across_turns():
    assert semantic_triage.default_provider() is semantic_triage.default_provider()


def test_families_reach_the_model_in_provider_free_words():
    from bruce_engine.router_model_contract import RouterModelRequest

    req = RouterModelRequest(text="x", live_capability_families=("calendar", "gmail"))
    assert router_model.live_capability_families(req) == ("calendar", "communication")
