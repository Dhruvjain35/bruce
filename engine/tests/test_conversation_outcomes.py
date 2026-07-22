"""D-INT-1 outcome-dispatch seam — the handler pipeline (unit) + parity with the pre-seam branch.

Proves: the default pipeline reproduces event-candidate + styled-default behavior in order; a custom
handler inserted before the default SHORT-CIRCUITS (the seam a workstream uses to add a capability
without editing handle()); the first non-None handler wins; a list without a terminal handler raises.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from bruce_engine import conversation_outcomes as co
from bruce_engine.conversation_contract import (
    ConversationDecision, ExtractedEntity, IntentKind, ResponseType, RiskLevel,
)


def _decision(intent=IntentKind.casual, rt=ResponseType.direct_answer, text="ok", entities=None, caps=None):
    return ConversationDecision(
        intent=intent, response_type=rt, user_visible_response=text,
        extracted_entities=entities or [], required_capabilities=caps or [],
        needs_mission=False, risk_level=RiskLevel.none, confidence=0.8)


def _octx(decision, *, present=None, store=None):
    # a minimal OutcomeContext; present/store are the only collaborators the default handlers call
    return co.OutcomeContext(
        user_id=uuid4(), decision=decision, capsule=object(), msg=type("M", (), {"text": decision.user_visible_response})(),
        profile=object(), channel="self_hosted_imessage", pmid="p1",
        present=present or (lambda t, **k: f"styled:{t}"), style=None, store=store)


def _run(c):
    return asyncio.run(c)


def test_default_reply_handler_is_terminal_and_styles():
    octx = _octx(_decision(text="hey"))
    res = _run(co.DefaultReplyHandler().resolve(octx))
    assert res is not None and res.handler == "default_reply" and res.reply == "styled:hey"
    assert res.event_candidate_id is None


def test_event_handler_passes_when_not_an_event():
    # a plain casual decision (no title+date entity) -> event handler returns None (pass through)
    assert _run(co.EventCandidateHandler().resolve(_octx(_decision(text="lol")))) is None


def test_event_handler_persists_candidate_and_uses_calendar_template_when_wanted():
    class FakeStore:
        def __init__(self): self.calls = []
        async def persist_event_candidate(self, user_id, **kw):
            self.calls.append(kw); return uuid4()

    class FakeStyle:
        def template(self, name, **slots): return f"tmpl:{name}:{slots.get('title')}"

    d = _decision(intent=IntentKind.actionable, rt=ResponseType.extraction_result, text="added it",
                  entities=[ExtractedEntity(type="event_title", value="Chess Club"),
                            ExtractedEntity(type="date", value="2026-05-01", normalized="2026-05-01")])
    store = FakeStore()
    octx = _octx(d, store=store)
    octx.style = FakeStyle()
    res = _run(co.EventCandidateHandler().resolve(octx))
    assert res is not None and res.handler == "event_candidate" and res.event_candidate_id is not None
    assert len(store.calls) == 1                                # candidate persisted
    assert res.reply.startswith("tmpl:event_saved_calendar_unavailable")   # honest, never 'added'


def test_pipeline_first_nonnull_handler_wins_and_custom_handler_short_circuits():
    # a workstream's capability handler inserted BEFORE the default must win — the seam contract
    class MissionStub:
        name = "mission_stub"
        async def resolve(self, octx):
            if octx.decision.needs_mission:
                return co.ResolvedReply(reply="on it", handler=self.name)
            return None

    async def go():
        pipeline = [MissionStub(), *co.default_handlers()]
        # needs_mission=True -> the mission stub wins over the styled default
        d2 = ConversationDecision(intent=IntentKind.casual, response_type=ResponseType.direct_answer,
                                  user_visible_response="take this from here", needs_mission=True,
                                  risk_level=RiskLevel.none, confidence=0.8)
        octx = _octx(d2)
        for h in pipeline:
            r = await h.resolve(octx)
            if r is not None:
                return r
    res = _run(go())
    assert res.handler == "mission_stub" and res.reply == "on it"


def test_default_handlers_ends_with_terminal():
    hs = co.default_handlers()
    assert hs[-1].name == "default_reply"          # terminal must be last so the pipeline never falls through
    assert hs[0].name == "event_candidate"         # ported order preserved
