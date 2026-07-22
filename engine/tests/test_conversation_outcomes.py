"""D-INT-3 outcome-dispatch seam — structured dispositions, pure evaluation, deterministic selection.

Proves integration invariant 1: evaluate() is pure (no mutation); selection is by explicit priority
(claims outrank blocked); exactly one primary owner; a tie FAILS LOUDLY (OutcomeCollision); zero owners
route to the explicit fallback; the telemetry-only mission handler NEVER claims even on a hallucinated flag.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from bruce_engine import conversation_outcomes as co
from bruce_engine.conversation_contract import (
    ConversationDecision, ExtractedEntity, IntentKind, ResponseType, RiskLevel,
)


def _decision(intent=IntentKind.casual, rt=ResponseType.direct_answer, text="ok", entities=None,
              caps=None, needs_mission=False, proposed_goal=None):
    return ConversationDecision(
        intent=intent, response_type=rt, user_visible_response=text,
        extracted_entities=entities or [], required_capabilities=caps or [],
        needs_mission=needs_mission, proposed_goal=proposed_goal, risk_level=RiskLevel.none, confidence=0.8)


class FakeStore:
    def __init__(self):
        self.calls = []
    async def persist_event_candidate(self, user_id, **kw):
        self.calls.append(kw)
        return uuid4()


class FakeStyle:
    def template(self, name, **slots):
        return f"tmpl:{name}:{slots.get('title')}"


def _octx(decision, *, store=None, text=None):
    return co.OutcomeContext(
        user_id=uuid4(), decision=decision, capsule=object(),
        msg=type("M", (), {"text": text if text is not None else decision.user_visible_response})(),
        profile=object(), channel="self_hosted_imessage", pmid="p1", style=FakeStyle(), store=store)


def _run(c):
    return asyncio.run(c)


# --- pure evaluation ------------------------------------------------------------------------------

def test_evaluate_is_pure_no_store_mutation():
    store = FakeStore()
    # an EVENT decision would, on execute, persist a candidate — but evaluate must not
    d = _decision(entities=[ExtractedEntity(type="event_title", value="X"),
                            ExtractedEntity(type="date", value="2026-05-01")])
    octx = _octx(d, store=store)
    for h in co.default_handlers():
        _run(h.evaluate(octx))
    assert store.calls == []                               # evaluation mutated nothing


def test_mission_handoff_declines_and_never_claims_even_with_hallucinated_flag():
    # model says needs_mission=True but the user did NOT ask for a handoff -> still DECLINE, no claim
    d = _decision(text="what's 8x7", needs_mission=True, proposed_goal="do my homework")
    v = _run(co.MissionHandoffHandler().evaluate(_octx(d, text="what's 8x7")))
    assert v.disposition == co.Disposition.decline
    assert v.telemetry["authorizes_mutation"] is False
    assert v.telemetry["action"] in ("answer_only", "remember_context")


def test_event_handler_claims_on_event_declines_otherwise():
    ev = _decision(entities=[ExtractedEntity(type="event_title", value="Chess"),
                             ExtractedEntity(type="date", value="2026-05-01")])
    assert _run(co.EventCandidateHandler().evaluate(_octx(ev))).disposition == co.Disposition.claim
    assert _run(co.EventCandidateHandler().evaluate(_octx(_decision(text="lol")))).disposition == co.Disposition.decline


# --- execute --------------------------------------------------------------------------------------

def test_event_execute_persists_and_returns_fact_locked_template_unstyled():
    store = FakeStore()
    d = _decision(intent=IntentKind.actionable, rt=ResponseType.extraction_result, text="added it",
                  entities=[ExtractedEntity(type="event_title", value="Chess Club"),
                            ExtractedEntity(type="date", value="2026-05-01", normalized="2026-05-01")])
    out = _run(co.EventCandidateHandler().execute(_octx(d, store=store, text="add to calendar")))
    assert len(store.calls) == 1 and out.event_candidate_id is not None
    assert out.styled is False                             # fact-locked copy is not voice-styled
    assert out.text.startswith("tmpl:event_saved_calendar_unavailable")   # honest, never 'added'


def test_default_fallback_execute_is_styled_model_reply():
    out = _run(co.default_fallback().execute(_octx(_decision(text="hey there"))))
    assert out.text == "hey there" and out.styled is True


# --- deterministic selection ----------------------------------------------------------------------

def _verdict(disp, prio):
    return co.HandlerVerdict(disposition=disp, priority=prio)


class _H:
    def __init__(self, name): self.name = name


def test_select_single_claim_wins():
    h = _H("a")
    assert co.select_owner([(h, _verdict(co.Disposition.claim, 50))]) is h


def test_select_higher_priority_claim_wins():
    a, b = _H("a"), _H("b")
    assert co.select_owner([(a, _verdict(co.Disposition.claim, 50)),
                            (b, _verdict(co.Disposition.claim, 60))]) is b


def test_select_claims_outrank_blocked_regardless_of_priority():
    a, b = _H("claim_low"), _H("blocked_high")
    assert co.select_owner([(a, _verdict(co.Disposition.claim, 5)),
                            (b, _verdict(co.Disposition.blocked, 99))]) is a


def test_select_zero_owners_returns_none_for_fallback():
    a, b = _H("a"), _H("b")
    assert co.select_owner([(a, _verdict(co.Disposition.decline, 0)),
                            (b, _verdict(co.Disposition.decline, 10))]) is None


def test_select_tie_at_top_priority_fails_loudly():
    import pytest
    a, b = _H("a"), _H("b")
    with pytest.raises(co.OutcomeCollision):
        co.select_owner([(a, _verdict(co.Disposition.claim, 50)),
                         (b, _verdict(co.Disposition.claim, 50))])


def test_default_pipeline_shape():
    hs = co.default_handlers()
    assert [h.name for h in hs] == ["mission_handoff", "event_candidate"]
    assert co.default_fallback().name == "default_reply"
    # explicit priorities: handoff outranks event; fallback is lowest and never a claim
    assert co.MissionHandoffHandler().priority > co.EventCandidateHandler().priority
    assert co.DefaultReplyHandler().priority == 0
