"""Bite 2 A (PR A1) — reply/reaction/relationship metadata TRANSPORT contract.

Null-safe, additive: provider-neutral relationship fields ride ImsgEvent -> relay serialization ->
POST body -> RelayInboundRequest -> InboundMessage, with a FAIL-CLOSED guard so tapback / unsend /
edit events never enter the message->reply pipeline (no turn, no outbound). NO persistence (A2) and
NO referenced-message/attachment retrieval (A3) here. Fake imsg + fake backend, offline.
"""

from __future__ import annotations

import asyncio

import bruce_engine.api as api
from bruce_engine.api import RelayInboundRequest, relay_inbound
from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.imsg import ImsgEvent, parse_event, reaction_of
from relay.pending import PendingStore
from relay.relay import Relay, _event_to_dict


def _run(c):
    return asyncio.run(c)


# --- imsg parse + verified-only reaction mapping -------------------------------------------------

def test_new_fields_parse_null_safe():
    e = parse_event({"guid": "g1", "text": "hi"})
    assert e.reply_to_guid is None and e.thread_originator_guid is None
    assert e.associated_message_guid is None and e.associated_message_type is None
    assert e.service is None and e.is_edited is False and e.is_unsent is False


def test_reaction_type_mapping_correct():
    assert reaction_of(2000) == ("love", False)
    assert reaction_of(2001) == ("like", False)
    assert reaction_of(2002) == ("dislike", False)
    assert reaction_of(2003) == ("laugh", False)
    assert reaction_of(2004) == ("emphasis", False)
    assert reaction_of(2005) == ("question", False)


def test_reaction_removal_correct():
    assert reaction_of(3000) == ("love", True)
    assert reaction_of(3002) == ("dislike", True)


def test_unknown_reaction_types_remain_unknown():
    assert reaction_of(2099) == ("unknown", False)
    assert reaction_of(3099) == ("unknown", True)


def test_non_reaction_types_are_not_reactions():
    for v in (None, 0, 1, 1999, 4000, 5000):
        assert reaction_of(v) == (None, False)


def test_associated_type_coerced_to_int_null_safe():
    assert parse_event({"guid": "g", "associated_message_type": "2000"}).associated_message_type == 2000
    assert parse_event({"guid": "g", "associated_message_type": "notanint"}).associated_message_type is None
    assert parse_event({"guid": "g"}).associated_message_type is None


# --- restart-safe serialization ------------------------------------------------------------------

def test_serialization_survives_relay_restart():
    e = ImsgEvent(guid="g1", chat_guid="c1", sender="+1", is_from_me=False, is_group=False,
                  text="x", created_at="t", attachments=[], reply_to_guid="r1",
                  thread_originator_guid="root1", associated_message_guid="tgt1",
                  associated_message_type=2003, service="iMessage", is_edited=True, is_unsent=False)
    back = parse_event(_event_to_dict(e))
    for f in ("reply_to_guid", "thread_originator_guid", "associated_message_guid",
              "associated_message_type", "service", "is_edited", "is_unsent"):
        assert getattr(back, f) == getattr(e, f)


# --- POST body (relay -> server) -----------------------------------------------------------------

class _Backend:
    def __init__(self):
        self.inbound: list[dict] = []

    async def post_inbound(self, ev):
        self.inbound.append(ev)
        return {"status": "processed"}

    async def upload(self, *a):
        return "u1"

    async def claim(self):
        return None

    async def ack(self, *a, **k):
        pass

    async def heartbeat(self):
        return {"ok": True}


def _relay(tmp_path, be):
    return Relay(imsg=InProcessImsg(), backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
                 spool_dir=str(tmp_path / "spool"), poll_interval=0.01,
                 pending=PendingStore(str(tmp_path / "pending.json")))


def test_post_body_preserves_relationship_fields(tmp_path):
    be = _Backend()
    _run(_relay(tmp_path, be).process_inbound_dict(
        {"guid": "m1", "sender": "+1", "text": "see this", "reply_to_guid": "r9",
         "thread_originator_guid": "root9", "service": "iMessage"}))
    b = be.inbound[-1]
    assert b["reply_to_message_id"] == "r9"
    assert b["thread_root_message_id"] == "root9"
    assert b["service"] == "iMessage"
    assert b["reaction_type"] is None and b["reaction_target_message_id"] is None
    assert b["edited"] is False and b["unsent"] is False


def test_post_body_maps_reaction(tmp_path):
    be = _Backend()
    _run(_relay(tmp_path, be).process_inbound_dict(
        {"guid": "rx1", "sender": "+1", "text": "❤", "associated_message_guid": "tgt",
         "associated_message_type": 2000}))
    b = be.inbound[-1]
    assert b["reaction_type"] == "love" and b["reaction_removed"] is False
    assert b["reaction_target_message_id"] == "tgt"


def test_ordinary_message_post_body_unchanged(tmp_path):
    be = _Backend()
    _run(_relay(tmp_path, be).process_inbound_dict({"guid": "t1", "sender": "+1", "text": "hey"}))
    b = be.inbound[-1]
    assert b["reaction_type"] is None and b["thread_root_message_id"] is None
    assert b["edited"] is False and b["unsent"] is False and b["service"] is None
    assert b["reply_to_message_id"] is None


# --- server handler: fail-closed guard + mapping (no DB; handle_inbound mocked) -------------------

def _inbound(req, monkeypatch):
    calls = {}

    async def fake_handle(channel, msg):
        calls["msg"] = msg
        from bruce_engine.messaging_inbound import InboundOutcome
        return InboundOutcome(status="processed", user_id=None)

    monkeypatch.setattr(api.messaging_inbound, "handle_inbound", fake_handle)
    monkeypatch.setattr(api.messaging_outbound, "QueueChannel", lambda: object())
    status = _run(relay_inbound(req, device=object()))
    return status, calls


def _req(**kw):
    base = dict(provider_message_id="m1", channel_identity="+1", timestamp=None)
    base.update(kw)
    return RelayInboundRequest(**base)


def test_api_mapping_preserves_fields_for_normal_message(monkeypatch):
    _, calls = _inbound(_req(text="hey", chat_guid="chatA", reply_to_message_id="r1",
                             thread_root_message_id="rootA", service="iMessage"), monkeypatch)
    msg = calls["msg"]
    assert msg.reply_to_message_id == "r1"
    assert msg.thread_id == "chatA"                 # conversation/chat id
    assert msg.thread_root_message_id == "rootA"    # inline-reply root, DISTINCT
    assert msg.service == "iMessage"


def test_reply_does_not_collide_with_chat_or_thread(monkeypatch):
    _, calls = _inbound(_req(text="x", chat_guid="chatA", thread_root_message_id="rootA"), monkeypatch)
    msg = calls["msg"]
    assert msg.thread_id == "chatA" and msg.thread_root_message_id == "rootA"
    assert msg.thread_id != msg.thread_root_message_id


def test_reaction_event_produces_no_turn(monkeypatch):
    status, calls = _inbound(_req(reaction_type="love", reaction_target_message_id="tgt"), monkeypatch)
    assert status["status"] == "reaction_ignored_until_context_graph"
    assert "msg" not in calls                       # handle_inbound NEVER called


def test_unsent_event_produces_no_turn(monkeypatch):
    status, calls = _inbound(_req(unsent=True), monkeypatch)
    assert status["status"] == "unsent_event_recorded" and "msg" not in calls


def test_edited_event_no_duplicate_reply(monkeypatch):
    status, calls = _inbound(_req(edited=True, text="edited body"), monkeypatch)
    assert status["status"] == "relationship_event_recorded" and "msg" not in calls


def test_duplicate_relationship_events_idempotent(monkeypatch):
    req = _req(reaction_type="like", reaction_target_message_id="tgt")
    s1, c1 = _inbound(req, monkeypatch)
    s2, c2 = _inbound(req, monkeypatch)
    assert s1 == s2 == {"status": "reaction_ignored_until_context_graph"}
    assert "msg" not in c1 and "msg" not in c2


def test_ordinary_text_still_processed_unchanged(monkeypatch):
    status, calls = _inbound(_req(text="what's up"), monkeypatch)
    assert status["status"] == "processed" and calls["msg"].text == "what's up"
    assert calls["msg"].reaction_type is None and calls["msg"].unsent is False
