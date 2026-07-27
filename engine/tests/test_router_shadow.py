"""M1B — a Stage-0 miss must be observable, and must not change what the router decides.

THE PROBLEM. `_stage0` returns None when no deterministic rule fires; `_stage1` returns `_DEFAULT`
(fast_conversation) whenever no model provider exists — which is always, because Stage-1 has been gated
off since live calibration measured p50 1383ms / p95 2002ms / 12.5% timeouts. So every paraphrase the
router does not recognise silently becomes "casual chat", and a miss looks identical to a correct chat
classification. The true miss rate is currently unmeasurable.

These tests pin two things: the miss is CLASSIFIED, and the turn is UNCHANGED.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from bruce_engine import router_shadow as rs


def _run(c):
    return asyncio.run(c)


def _stub(monkeypatch, *, pending=False, active=False):
    async def _p(_uid):
        return {"id": "d1"} if pending else None

    async def _a(_uid, *, domain=None):
        return {"id": "r1"} if active else None

    import bruce_engine.mission_kernel as mk
    import bruce_engine.agent_run_store as ars
    monkeypatch.setattr(mk, "latest_pending_calendar_mission", _p)
    monkeypatch.setattr(ars, "latest_active", _a)


# --- precedence: exactly one bucket, strongest explanation first ---------------------------------------

def test_an_open_decision_explains_the_miss(monkeypatch):
    _stub(monkeypatch, pending=True, active=True)
    obs = _run(rs.classify_miss(uuid.uuid4(), "ya do it"))
    assert obs.bucket == rs.PENDING_DECISION, "a reply to an open question outranks other explanations"
    assert obs.had_pending_decision


def test_work_in_flight_explains_the_miss(monkeypatch):
    _stub(monkeypatch, active=True)
    obs = _run(rs.classify_miss(uuid.uuid4(), "any word on that yet?"))
    assert obs.bucket == rs.ACTIVE_RUN


def test_bare_deixis_is_recorded_as_reference_only(monkeypatch):
    _stub(monkeypatch)
    for text in ("what about that one", "the other one", "did they say anything"):
        assert _run(rs.classify_miss(uuid.uuid4(), text)).bucket == rs.REFERENCE_ONLY, text


def test_an_unexplained_miss_is_marked_as_needing_semantics(monkeypatch):
    _stub(monkeypatch)
    for text in ("jot down dentist thursday", "can u handle the scholarship thing"):
        assert _run(rs.classify_miss(uuid.uuid4(), text)).bucket == rs.NEEDS_SEMANTIC, text


# --- the shadow may never affect the turn ---------------------------------------------------------------

def test_a_shadow_failure_is_contained(monkeypatch):
    async def _boom(_uid):
        raise RuntimeError("db down")
    import bruce_engine.mission_kernel as mk
    import bruce_engine.agent_run_store as ars
    monkeypatch.setattr(mk, "latest_pending_calendar_mission", _boom)

    async def _a(_uid, *, domain=None):
        return None
    monkeypatch.setattr(ars, "latest_active", _a)
    obs = _run(rs.classify_miss(uuid.uuid4(), "hello"))
    assert obs.bucket in (rs.NEEDS_SEMANTIC, rs.REFERENCE_ONLY, rs.SHADOW_ERROR)


def test_classification_makes_no_model_call(monkeypatch):
    """Deliberate: a shadow semantic call on every miss would add the latency that got Stage-1 gated
    off, on turns already falling through. The cheap lookups answer the first question."""
    import inspect
    src = inspect.getsource(rs.classify_miss)
    for forbidden in ("router_model", "CompactRouterModel", "openai", "Agent("):
        assert forbidden not in src, f"shadow must not invoke a model ({forbidden})"


def test_observations_are_content_free():
    """Telemetry carries buckets, booleans, length and latency — never message text."""
    import inspect
    src = inspect.getsource(rs)
    assert "text_len=len(" in src
    assert 'log.info("router_miss' in src
    assert "%s" in src and "text=%s" not in src, "message text must never be logged"


# --- the miss marker itself -----------------------------------------------------------------------------

def test_is_miss_recognises_the_silent_default():
    from bruce_engine.fast_router import _DEFAULT
    assert rs.is_miss(_DEFAULT), "_DEFAULT already carries source=router_default; nothing consumed it"


def test_a_real_classification_is_not_a_miss():
    from bruce_engine.runtime_contracts import ExecutionClass, GoalAction, RouterDecision
    real = RouterDecision(ExecutionClass.direct_action, action=GoalAction.send, domain="gmail")
    assert not rs.is_miss(real)


# --- cross-domain visibility ----------------------------------------------------------------------------

def test_active_run_lookup_is_not_calendar_only():
    """latest_active defaulted to domain="calendar" with no way to opt out, which made every gmail
    background mission structurally invisible. A missing domain is UNKNOWN, never Calendar."""
    import inspect
    from bruce_engine import agent_run_store
    sig = inspect.signature(agent_run_store.latest_active)
    assert sig.parameters["domain"].annotation in ("str | None", "Optional[str]")
    src = inspect.getsource(agent_run_store.latest_active)
    assert "if domain is not None" in src


def test_dead_letter_counts_as_terminal():
    """The two terminal-status sets in this module genuinely disagreed: the SQL at :234 excluded
    dead_letter, latest_active did not — so a dead run read as ACTIVE forever and the runtime would
    report in-flight work nothing will ever advance."""
    import inspect
    from bruce_engine import agent_run_store
    assert "dead_letter" in inspect.getsource(agent_run_store.latest_active)


# --- kill switch ----------------------------------------------------------------------------------------

def test_shadow_is_on_by_default_and_can_be_disabled(monkeypatch):
    from bruce_engine import fast_router
    assert fast_router._shadow_enabled() is True
    monkeypatch.setenv("BRUCE_ROUTER_SHADOW_OFF", "1")
    assert fast_router._shadow_enabled() is False
