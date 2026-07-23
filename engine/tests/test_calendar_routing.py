"""P0.3/P0.4/P0.5 — the live-failure regressions.

The live bug: "schedule ts" on a connected calendar did NOTHING (no mission, no event) and Bruce then
said "i can't actually schedule calendar events from here". These tests lock the routing + intent +
capability-truth fixes so that never happens again:

  * scheduling verbs ("schedule this/ts", "put this on my calendar", "block this off", "yo can u
    schedule this for me") deterministically authorize the write; questions/self-handling do not.
  * CalendarScheduleHandler CLAIMS an executable scheduling request over generic vision/mission handlers.
  * exactly one handler claims; a disconnected calendar declines (connect-required, never a fake add).
  * a model reply that DENIES a connected calendar capability is overridden with the truth.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from bruce_engine import capability_truth
from bruce_engine import conversation_outcomes as co
from bruce_engine import handoff
from bruce_engine.conversation_contract import (
    ConversationDecision, ExtractedEntity, IntentKind, ResponseType, RiskLevel,
)


def _run(c):
    return asyncio.run(c)


# --- P0.4 scheduling intent (deterministic authorizer) --------------------------------------------

@pytest.mark.parametrize("text", [
    "schedule this", "schedule ts", "put this on my calendar", "add this to my calendar",
    "calendar this", "save these dates", "block this off", "yo can u schedule this for me",
    "can u add this to my cal rq", "schedule this for me pls", "add ts to my calendar",
])
def test_scheduling_intent_positives(text):
    assert handoff.has_scheduling_execution_intent(text) is True


@pytest.mark.parametrize("text", [
    "how do i schedule this?", "should i schedule this?", "what would i put on my calendar?",
    "i'll schedule this myself", "imma schedule this later", "what's on my calendar?",
    "when is this event?", "cool flyer", "",
])
def test_scheduling_intent_negatives(text):
    assert handoff.has_scheduling_execution_intent(text) is False


# --- P0.5 capability truth ------------------------------------------------------------------------

def test_capability_denial_detected_and_corrected_when_connected():
    denial = "i can't actually schedule calendar events from here. paste the details and i'll help you draft"
    assert capability_truth.mentions_calendar_denial(denial) is True
    corr = capability_truth.grounded_calendar_correction()
    assert "can" in corr.lower() and "connected" in corr.lower()
    assert "done" not in corr.lower()          # never fabricates completion


@pytest.mark.parametrize("text", [
    "i can't actually schedule calendar events from here",
    "i cannot add this to your calendar",
    "i don't have access to your calendar",
    "can't schedule calendar events from here",
])
def test_denial_variants_detected(text):
    assert capability_truth.mentions_calendar_denial(text) is True


def test_non_denial_not_flagged():
    assert capability_truth.mentions_calendar_denial("done, it's on ur calendar for july 25–26 ✅") is False
    assert capability_truth.mentions_calendar_denial("want me to add this to your calendar?") is False


# --- P0.3 routing: handler selection --------------------------------------------------------------

def _sched_decision(text="schedule ts"):
    return ConversationDecision(
        intent=IntentKind.image_understanding, response_type=ResponseType.extraction_result,
        user_visible_response="i pulled the key details i can read from the image: ...",
        extracted_entities=[
            ExtractedEntity(type="event_title", value="Startup School 2026"),
            ExtractedEntity(type="date", value="July 25", normalized="2026-07-25"),
            ExtractedEntity(type="date", value="July 26", normalized="2026-07-26"),
            ExtractedEntity(type="location", value="Chase Center, SF")],
        needs_mission=False, risk_level=RiskLevel.none, confidence=0.9)


class _Conn:
    status = "connected"; revoked_at = None; refresh_token_encrypted = "x"


def _octx(decision, *, text):
    return co.OutcomeContext(
        user_id=uuid4(), decision=decision, capsule=object(),
        msg=type("M", (), {"text": text, "attachments": []})(),
        profile=object(), channel="self_hosted_imessage", pmid="p1", style=object(), store=None)


def _patch_conn(monkeypatch, integ=_Conn()):
    from bruce_engine import oauth_google
    async def _f(uid): return integ
    monkeypatch.setattr(oauth_google, "get_integration", _f)


def _dispositions(octx):
    """Every handler's disposition for this context (the full evaluate pass)."""
    out = {}
    for h in co.default_handlers():
        out[h.name] = _run(h.evaluate(octx)).disposition
    return out


def test_schedule_ts_routes_to_calendar_over_vision_and_mission(monkeypatch):
    _patch_conn(monkeypatch)
    octx = _octx(_sched_decision("schedule ts"), text="schedule ts")
    disp = _dispositions(octx)
    assert disp["calendar_schedule"] == co.Disposition.claim
    # generic vision/model (event_candidate) must NOT out-claim the executable scheduling request:
    # calendar_schedule is priority 70, event_candidate 50 -> selection picks calendar_schedule.
    verdicts = [(h, _run(h.evaluate(octx))) for h in co.default_handlers()]
    winner = co.select_owner(verdicts)
    assert winner is not None and winner.name == "calendar_schedule"


def test_exactly_one_top_claim(monkeypatch):
    _patch_conn(monkeypatch)
    octx = _octx(_sched_decision("schedule this"), text="schedule this")
    verdicts = [(h, _run(h.evaluate(octx))) for h in co.default_handlers()]
    claims = [h for h, v in verdicts if v.disposition == co.Disposition.claim]
    # more than one handler may be *willing*, but selection yields exactly one owner at the top priority
    winner = co.select_owner(verdicts)
    assert winner.name == "calendar_schedule"
    top = max(v.priority for _, v in verdicts if v.disposition == co.Disposition.claim)
    assert sum(1 for _, v in verdicts if v.disposition == co.Disposition.claim and v.priority == top) == 1


def test_disconnected_calendar_declines_scheduling(monkeypatch):
    _patch_conn(monkeypatch, integ=None)                       # not connected
    octx = _octx(_sched_decision("schedule ts"), text="schedule ts")
    v = _run(co.CalendarScheduleHandler().evaluate(octx))
    assert v.disposition == co.Disposition.decline and v.reason == "calendar_not_connected"


def test_connected_calendar_scheduling_claims(monkeypatch):
    _patch_conn(monkeypatch)
    v = _run(co.CalendarScheduleHandler().evaluate(_octx(_sched_decision("schedule ts"), text="schedule ts")))
    assert v.disposition == co.Disposition.claim


def test_question_about_scheduling_does_not_claim(monkeypatch):
    _patch_conn(monkeypatch)
    v = _run(co.CalendarScheduleHandler().evaluate(
        _octx(_sched_decision("how do i schedule this?"), text="how do i schedule this?")))
    assert v.disposition == co.Disposition.decline and v.reason == "not_authorized_to_schedule"
