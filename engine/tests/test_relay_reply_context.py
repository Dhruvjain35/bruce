"""A3.2b — the relay builds + attaches the ReplyContextEnvelope on the inbound POST when a message
explicitly references an earlier one. Temp chat.db + fake backend, offline."""

from __future__ import annotations

import asyncio
import sqlite3

from relay.chatdb import ChatDb
from relay.checkpoint import FileCheckpoint
from relay.fake_imsg import InProcessImsg
from relay.pending import PendingStore
from relay.relay import Relay

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48
_APPLE_NS = 100 * 86400 * 1_000_000_000


def _make_chatdb(tmp_path, *, ref_guid="R1", chat="chatA", attach_path=None, transfer_state=5, mime="image/png"):
    p = tmp_path / "chat.db"
    conn = sqlite3.connect(str(p))
    conn.executescript("""
        CREATE TABLE message (guid TEXT, handle_id INTEGER, is_from_me INTEGER, date INTEGER,
            reply_to_guid TEXT, thread_originator_guid TEXT, date_edited INTEGER, date_retracted INTEGER,
            service TEXT, associated_message_guid TEXT, associated_message_type INTEGER,
            cache_has_attachments INTEGER, date_read INTEGER, text TEXT);
        CREATE TABLE attachment (guid TEXT, mime_type TEXT, uti TEXT, total_bytes INTEGER,
            transfer_state INTEGER, transfer_name TEXT, filename TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        CREATE TABLE chat (guid TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    """)
    conn.execute("INSERT INTO message (guid,handle_id,is_from_me,date,service,cache_has_attachments,text) "
                 "VALUES (?,7,0,?,'iMessage',1,'secret')", (ref_guid, _APPLE_NS))
    conn.execute("INSERT INTO chat (guid) VALUES (?)", (chat,))
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
    conn.execute("INSERT INTO attachment (guid,mime_type,total_bytes,transfer_state,transfer_name,filename) "
                 "VALUES ('att1',?,48,?,'IMG.png',?)", (mime, transfer_state, attach_path or "/nope.png"))
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
    conn.commit(); conn.close()
    return str(p)


class _Backend:
    def __init__(self):
        self.inbound = []
        self.uploads = []

    async def post_inbound(self, ev):
        self.inbound.append(ev); return {"status": "processed"}

    async def upload(self, data, mime, name):
        self.uploads.append((mime, len(data))); return f"upl-{len(self.uploads)}"

    async def claim(self):
        return None

    async def ack(self, *a, **k):
        pass

    async def heartbeat(self):
        return {"ok": True}


def _relay(tmp_path, be, chatdb):
    (tmp_path / "spool").mkdir(exist_ok=True)
    return Relay(imsg=InProcessImsg(), backend=be, checkpoint=FileCheckpoint(str(tmp_path / "cp.json")),
                 spool_dir=str(tmp_path / "spool"), poll_interval=0.01,
                 pending=PendingStore(str(tmp_path / "pending.json")), chatdb=chatdb)


def _run(c):
    return asyncio.run(c)


def test_reply_event_attaches_resolved_envelope(tmp_path):
    img = tmp_path / "IMG.png"; img.write_bytes(PNG)
    db = ChatDb(_make_chatdb(tmp_path, attach_path=str(img), transfer_state=5))
    be = _Backend()
    _run(_relay(tmp_path, be, db).process_inbound_dict(
        {"guid": "cur1", "sender": "+1", "text": "why 0.4", "reply_to_guid": "R1"}))
    rc = be.inbound[-1]["reply_context"]
    assert rc is not None and rc["resolution_source"] == "relay_exact_lookup"
    assert rc["referenced_chat_guid"] == "chatA" and rc["referenced_direction"] == "inbound"
    refs = rc["referenced_attachment_refs"]
    assert len(refs) == 1 and refs[0]["available"] is True and refs[0]["upload_ref"]
    blob = str(rc)                                          # no path/filename crosses the wire
    assert str(img) not in blob and "IMG.png" not in blob and "filename" not in blob


def test_reply_to_undownloaded_attachment_is_honest(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path, attach_path="/nope.png", transfer_state=1))
    be = _Backend()
    _run(_relay(tmp_path, be, db).process_inbound_dict(
        {"guid": "cur2", "sender": "+1", "reply_to_guid": "R1"}))
    rc = be.inbound[-1]["reply_context"]
    assert rc["referenced_attachment_refs"][0]["available"] is False


def test_normal_message_has_no_reply_context(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path))
    be = _Backend()
    _run(_relay(tmp_path, be, db).process_inbound_dict({"guid": "n1", "sender": "+1", "text": "hi"}))
    assert be.inbound[-1]["reply_context"] is None


def test_no_chatdb_disables_enrichment(tmp_path):
    be = _Backend()
    _run(_relay(tmp_path, be, None).process_inbound_dict(
        {"guid": "c2", "sender": "+1", "reply_to_guid": "R1"}))
    assert be.inbound[-1]["reply_context"] is None
