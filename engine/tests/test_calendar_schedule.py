"""Unit tests for the calendar 'schedule this' seam — event building (all-day / multi-day / timed),
Google exclusive end-date semantics, the honest reply copy, and the pure handler evaluation. No DB, no
network (the operation-graph + Postgres behaviour is in test_calendar_schedule_pg.py)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from bruce_engine import calendar_schedule as cs
from bruce_engine import conversation_outcomes as co
from bruce_engine.conversation_contract import (
    ConversationDecision, ExtractedEntity, IntentKind, ResponseType, RiskLevel,
)
from bruce_engine.messaging_outbound import gate_outbound_text
from bruce_engine.models import CalendarEvent


def _decision(*, text, entities, intent=IntentKind.actionable, caps=None, goal=None):
    return ConversationDecision(
        intent=intent, response_type=ResponseType.extraction_result, user_visible_response=text,
        extracted_entities=entities, required_capabilities=caps or [], needs_mission=True,
        proposed_goal=goal, risk_level=RiskLevel.none, confidence=0.9)


def _startup_school(text="handle this for me, add it to my calendar"):
    return _decision(
        text=text, caps=["calendar"], goal="add Startup School to my calendar",
        entities=[
            ExtractedEntity(type="event_title", value="Startup School 2026"),
            ExtractedEntity(type="date", value="July 25", normalized="2026-07-25"),
            ExtractedEntity(type="date", value="July 26", normalized="2026-07-26"),
            ExtractedEntity(type="location", value="Chase Center, San Francisco"),
        ])


def _run(c):
    return asyncio.run(c)


# --- event building -------------------------------------------------------------------------------

def test_multiday_all_day_uses_google_exclusive_end():
    """July 25-26 all-day -> start 2026-07-25, end 2026-07-27 (day AFTER the last inclusive day)."""
    ev = cs.build_calendar_event(_startup_school())
    assert ev is not None
    assert ev.start == "2026-07-25" and ev.end == "2026-07-27"     # exclusive end
    assert cs.is_all_day(ev) is True
    assert ev.title == "Startup School 2026"
    assert ev.location == "Chase Center, San Francisco"


def test_single_all_day_is_one_day_exclusive_end():
    ev = cs.build_calendar_event(_decision(
        text="add this", caps=["calendar"],
        entities=[ExtractedEntity(type="event_title", value="Club Fair"),
                  ExtractedEntity(type="date", value="Sept 3", normalized="2026-09-03")]))
    assert ev.start == "2026-09-03" and ev.end == "2026-09-04"
    assert cs.is_all_day(ev)


def test_timed_event_gets_an_hour_when_no_end():
    ev = cs.build_calendar_event(_decision(
        text="put this on my calendar", caps=["calendar"],
        entities=[ExtractedEntity(type="event_title", value="Info Session"),
                  ExtractedEntity(type="datetime", value="6pm", normalized="2026-07-25T18:00:00")]))
    assert ev.start == "2026-07-25T18:00:00" and ev.end == "2026-07-25T19:00:00"
    assert not cs.is_all_day(ev)


def test_no_resolvable_date_returns_none_never_guesses():
    ev = cs.build_calendar_event(_decision(
        text="add this", caps=["calendar"],
        entities=[ExtractedEntity(type="event_title", value="Some Talk")]))
    assert ev is None


# --- human copy -----------------------------------------------------------------------------------

def test_human_when_collapses_exclusive_end_to_inclusive_range():
    ev = cs.build_calendar_event(_startup_school())
    assert cs.human_when(ev) == "july 25–26"                 # inclusive, not 25-27


def test_human_when_single_day():
    ev = CalendarEvent(title="X", start="2026-09-03", end="2026-09-04")
    assert cs.human_when(ev) == "september 3"


def test_verified_reply_is_honest_and_survives_the_outbound_gate():
    ev = cs.build_calendar_event(_startup_school())
    res = cs.ScheduleResult(state=cs.ScheduleState.verified, mission_id=uuid4(),
                            title=ev.title, all_day=True, event_id="abc", account="me@example.com")
    reply = co._calendar_reply(res, ev)
    assert reply.startswith("done, startup school 2026 is on ur calendar for july 25–26 ✅")
    assert "all-day event" in reply
    gated = gate_outbound_text(reply, "self_hosted_imessage")
    assert "—" not in gated and "–" in gated                # em dash gone; numeric range preserved


def test_unverified_states_never_say_done():
    ev = cs.build_calendar_event(_startup_school())
    for state in (cs.ScheduleState.not_connected, cs.ScheduleState.failed,
                  cs.ScheduleState.verification_inconclusive):
        res = cs.ScheduleResult(state=state, mission_id=uuid4(), title=ev.title, all_day=True)
        reply = co._calendar_reply(res, ev).lower()
        assert not reply.startswith("done")
        assert "✅" not in reply


def test_attachment_digest_is_stable_and_order_independent():
    a = [{"media_type": "image/jpeg", "filename": "flyer.jpg", "sha256": "aa"}]
    b = [{"media_type": "image/jpeg", "filename": "flyer.jpg", "sha256": "aa"}]
    assert cs.attachment_digest(a) == cs.attachment_digest(b) != ""
    assert cs.attachment_digest([]) == ""


# --- pure handler evaluation ----------------------------------------------------------------------

def _octx(decision, *, text):
    return co.OutcomeContext(
        user_id=uuid4(), decision=decision, capsule=object(),
        msg=type("M", (), {"text": text, "attachments": []})(),
        profile=object(), channel="self_hosted_imessage", pmid="p1", style=object(), store=None)


class _ConnStub:
    status = "connected"
    revoked_at = None
    refresh_token_encrypted = "x"


def _patch_connected(monkeypatch, integ=_ConnStub()):
    from bruce_engine import oauth_google
    async def _fake(user_id):
        return integ
    monkeypatch.setattr(oauth_google, "get_integration", _fake)


def test_handler_claims_on_authorized_calendar_event_handoff_when_connected(monkeypatch):
    _patch_connected(monkeypatch)
    d = _startup_school(text="handle this for me, add it to my calendar")
    v = _run(co.CalendarScheduleHandler().evaluate(_octx(d, text="handle this for me, add it to my calendar")))
    assert v.disposition == co.Disposition.claim and v.priority == 70


def test_handler_declines_when_calendar_not_connected(monkeypatch):
    # authorized event handoff, but no connected calendar -> DECLINE so generic capture owns it (P0 path)
    _patch_connected(monkeypatch, integ=None)
    d = _startup_school(text="handle this for me, add it to my calendar")
    v = _run(co.CalendarScheduleHandler().evaluate(_octx(d, text="handle this for me, add it to my calendar")))
    assert v.disposition == co.Disposition.decline and v.reason == "calendar_not_connected"


def test_handler_declines_without_explicit_handoff(monkeypatch):
    # just a flyer, no 'handle this' -> Bruce must NOT auto-write; declines so it can offer instead
    _patch_connected(monkeypatch)
    d = _startup_school(text="startup school looks cool")
    v = _run(co.CalendarScheduleHandler().evaluate(_octx(d, text="startup school looks cool")))
    assert v.disposition == co.Disposition.decline


def test_handler_declines_when_no_date_resolvable():
    # a date-TYPED entity exists (so _is_event passes) but it has no ISO value -> can't build an event
    d = _decision(text="handle this for me, add to my calendar", caps=["calendar"],
                  entities=[ExtractedEntity(type="event_title", value="Mystery Event"),
                            ExtractedEntity(type="date", value="sometime next month")])
    v = _run(co.CalendarScheduleHandler().evaluate(_octx(d, text="handle this for me, add to my calendar")))
    assert v.disposition == co.Disposition.decline and v.reason == "no_resolvable_date"
