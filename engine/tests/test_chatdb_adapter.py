"""A3.1 — the read-only chat.db adapter + exact-reference enrichment. Runs against a TEMP SQLite that
mirrors the real chat.db schema (offline; no Messages DB needed), so CI exercises the fixed queries,
feature-detection, staging, privacy (no path in the envelope), and honest failure."""

from __future__ import annotations

import asyncio
import os
import sqlite3

import pytest

from relay.chatdb import ChatDb, ChatDbUnavailable
from relay.reply_context import build_reply_envelope

# 2001-01-01 + 100 days, in Apple nanoseconds — a stable, known timestamp.
_APPLE_NS = 100 * 86400 * 1_000_000_000


def _make_chatdb(tmp_path, *, with_thread_col=True, attach_path=None, transfer_state=5):
    p = tmp_path / "chat.db"
    conn = sqlite3.connect(str(p))
    thread_col = "thread_originator_guid TEXT," if with_thread_col else ""
    conn.executescript(f"""
        CREATE TABLE message (
            guid TEXT, handle_id INTEGER, is_from_me INTEGER, date INTEGER,
            reply_to_guid TEXT, {thread_col} date_edited INTEGER, date_retracted INTEGER,
            service TEXT, associated_message_guid TEXT, associated_message_type INTEGER,
            cache_has_attachments INTEGER, date_read INTEGER, text TEXT);
        CREATE TABLE attachment (guid TEXT, mime_type TEXT, uti TEXT, total_bytes INTEGER,
            transfer_state INTEGER, transfer_name TEXT, filename TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        CREATE TABLE chat (guid TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    """)
    # one referenced INBOUND message ("ref1") in chat "chatA" with one attachment, edited, not unsent
    edited_extra = ", thread_originator_guid" if with_thread_col else ""
    edited_vals = ", 'rootX'" if with_thread_col else ""
    conn.execute(
        f"INSERT INTO message (guid,handle_id,is_from_me,date,reply_to_guid,date_edited,date_retracted,"
        f"service,cache_has_attachments,text{edited_extra}) VALUES "
        f"('ref1',7,0,{_APPLE_NS},NULL,{_APPLE_NS},0,'iMessage',1,'secret body'{edited_vals})")
    conn.execute("INSERT INTO chat (guid) VALUES ('chatA')")
    conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
    conn.execute("INSERT INTO attachment (guid,mime_type,uti,total_bytes,transfer_state,transfer_name,filename) "
                 "VALUES ('att1','image/heic','public.heic',1024,?,'IMG.heic',?)",
                 (transfer_state, attach_path or "/nope/missing.heic"))
    conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
    conn.commit()
    conn.close()
    return str(p)


def _run(c):
    return asyncio.run(c)


# --- adapter --------------------------------------------------------------------------------------

def test_get_message_by_guid_exact_with_chat_and_state(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path))
    row = db.get_message_by_guid("ref1")
    assert row is not None
    assert row.guid == "ref1" and row.chat_guid == "chatA" and row.service == "iMessage"
    assert row.is_from_me is False and row.has_attachments is True
    assert row.edited is True and row.unsent is False
    assert row.thread_originator_guid == "rootX"
    assert row.sent_at is not None and row.sent_at.startswith("2001-04-11")


def test_unknown_guid_returns_none_no_fuzzy_match(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path))
    assert db.get_message_by_guid("ref") is None        # exact only, no prefix/scan
    assert db.get_message_by_guid("nope") is None


def test_attachments_joined_to_exact_guid(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path))
    atts = db.get_attachments_for_message_guid("ref1")
    assert len(atts) == 1 and atts[0].mime_type == "image/heic" and atts[0].total_bytes == 1024
    assert db.get_attachments_for_message_guid("nope") == []


def test_feature_detect_missing_thread_column_degrades(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path, with_thread_col=False))
    row = db.get_message_by_guid("ref1")
    assert row is not None and row.thread_originator_guid is None   # missing column -> None, not error


def test_missing_db_is_unavailable_category(tmp_path):
    db = ChatDb(str(tmp_path / "nope.db"))
    with pytest.raises(ChatDbUnavailable):
        db.get_message_by_guid("ref1")


def test_adapter_does_not_modify_the_db(tmp_path):
    path = _make_chatdb(tmp_path)
    before = os.path.getmtime(path)
    db = ChatDb(path)
    db.get_message_by_guid("ref1")
    db.get_attachments_for_message_guid("ref1")
    assert os.path.getmtime(path) == before            # read-only: file untouched


# --- enrichment envelope --------------------------------------------------------------------------

async def _stage_ok(local_path, mime, name):
    _stage_ok.calls.append((local_path, mime, name))
    return "upload-1"
_stage_ok.calls = []


def test_envelope_resolves_and_stages_downloaded_attachment(tmp_path):
    f = tmp_path / "IMG.heic"; f.write_bytes(b"\x00" * 32)
    db = ChatDb(_make_chatdb(tmp_path, attach_path=str(f), transfer_state=5))
    _stage_ok.calls = []
    env = _run(build_reply_envelope(db, current_guid="cur1", referenced_guid="ref1",
                                    relationship_type="reply_to", stage_fn=_stage_ok))
    assert env.resolution_source == "relay_exact_lookup" and env.resolution_confidence == 1.0
    assert env.referenced_chat_guid == "chatA" and env.referenced_direction == "inbound"
    assert len(env.referenced_attachment_refs) == 1
    a = env.referenced_attachment_refs[0]
    assert a.available is True and a.upload_ref == "upload-1"
    # the LOCAL path was used for staging but NEVER put in the envelope
    assert _stage_ok.calls and _stage_ok.calls[0][0] == str(f)


def test_envelope_never_contains_a_path_or_filename(tmp_path):
    f = tmp_path / "IMG.heic"; f.write_bytes(b"\x00" * 32)
    db = ChatDb(_make_chatdb(tmp_path, attach_path=str(f), transfer_state=5))
    env = _run(build_reply_envelope(db, current_guid="cur1", referenced_guid="ref1",
                                    relationship_type="reply_to", stage_fn=_stage_ok))
    blob = str(env.to_wire())
    assert str(f) not in blob and "IMG.heic" not in blob and "filename" not in blob and "local_path" not in blob


def test_envelope_unavailable_when_attachment_not_downloaded(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path, attach_path="/nope/missing.heic", transfer_state=1))
    env = _run(build_reply_envelope(db, current_guid="cur1", referenced_guid="ref1",
                                    relationship_type="reply_to", stage_fn=_stage_ok))
    a = env.referenced_attachment_refs[0]
    assert a.available is False and a.unavailable_reason == "not_downloaded" and a.upload_ref is None


def test_envelope_unresolved_when_target_missing(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path))
    env = _run(build_reply_envelope(db, current_guid="cur1", referenced_guid="ghost",
                                    relationship_type="reply_to", stage_fn=_stage_ok))
    assert env.resolution_source == "unresolved" and env.unavailable_reason == "target_missing"
    assert env.referenced_attachment_refs == []


def test_envelope_unresolved_when_no_reference(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path))
    env = _run(build_reply_envelope(db, current_guid="cur1", referenced_guid=None,
                                    relationship_type="reply_to", stage_fn=_stage_ok))
    assert env.resolution_source == "unresolved" and env.unavailable_reason == "no_reference"


def test_envelope_unavailable_when_db_unreadable(tmp_path):
    db = ChatDb(str(tmp_path / "gone.db"))
    env = _run(build_reply_envelope(db, current_guid="cur1", referenced_guid="ref1",
                                    relationship_type="reply_to", stage_fn=_stage_ok))
    assert env.resolution_source == "unresolved" and env.unavailable_reason == "chatdb_missing"


def test_reply_pointer_part_prefix_normalizes_to_bare_guid(tmp_path):
    db = ChatDb(_make_chatdb(tmp_path))
    # iMessage can hand us a part-prefixed reply pointer; it must resolve to the bare message.guid
    assert db.get_message_by_guid("p:0/ref1").guid == "ref1"
    assert db.get_message_by_guid("bp:ref1").guid == "ref1"
    assert len(db.get_attachments_for_message_guid("p:0/ref1")) == 1
