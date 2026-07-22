"""Narrow, READ-ONLY chat.db adapter for A3 reply-context enrichment (relay-side, Mac-local).

Security contract:
  * FIXED prepared queries only — no arbitrary SQL, and NEVER any SQL supplied by the server.
  * Exact-GUID lookups only; no history scans, no "recent N" queries.
  * Read-only SQLite (``mode=ro``); the DB is never written, never uploaded.
  * Result sizes are hard-bounded (a message maps to a small, capped set of attachments).
  * Columns are FEATURE-DETECTED (thread_originator_guid / date_edited / date_retracted / service exist
    only on the Ventura+ schema tier) — a missing column degrades honestly, never errors.
  * Local filesystem paths NEVER leave this module: ``local_path`` is used only to stage the exact
    referenced attachment into the relay spool; it is never placed in the ReplyContextEnvelope.
  * Errors are privacy-safe CATEGORIES — no message text, handle, guid, filename, or path in any message.

This adapter returns only the minimum metadata to establish ownership / chat / time / service /
reply+thread relation / attachment availability for ONE explicitly-referenced message.
"""

from __future__ import annotations

import datetime
import os
import re
import sqlite3
from dataclasses import dataclass, field

# iMessage part-prefixes on a reply/thread guid (``p:0/GUID`` reply-part, ``bp:GUID``) — these DON'T
# appear on the bare ``message.guid`` they point at, so an exact lookup misses unless we strip them.
_GUID_PART_PREFIX = re.compile(r"^(?:bp:|p:\d+/)")


def _normalize_guid(guid: str | None) -> str | None:
    """Strip an iMessage part-prefix so a reply_to_guid / thread_originator_guid matches the bare
    ``message.guid`` it refers to. Idempotent; None-safe. No content, just a routing key."""
    if not guid:
        return guid
    return _GUID_PART_PREFIX.sub("", guid.strip())

_APPLE_EPOCH = 978307200            # 2001-01-01T00:00:00Z in unix seconds (Apple absolute time base)
_MAX_ATTACHMENTS = 16              # hard bound on attachments returned for one referenced message
_CONNECT_TIMEOUT_S = 3.0

# Optional columns present only on newer macOS chat.db schema tiers — feature-detected, never assumed.
_OPTIONAL_MESSAGE_COLS = ("reply_to_guid", "thread_originator_guid", "date_edited", "date_retracted",
                          "service", "associated_message_guid", "associated_message_type",
                          "cache_has_attachments", "date_read")


class ChatDbUnavailable(Exception):
    """chat.db could not be opened (missing / no Full Disk Access / locked). Category only."""


class ChatDbSchemaUnsupported(Exception):
    """The chat.db schema tier lacks a column this operation requires. Category only."""


@dataclass
class ChatMessageRow:
    guid: str
    chat_guid: str | None
    sender_handle_id: int | None
    is_from_me: bool
    service: str | None
    sent_at: str | None                 # ISO-8601 UTC, or None
    reply_to_guid: str | None
    thread_originator_guid: str | None
    edited: bool
    unsent: bool
    has_attachments: bool


@dataclass
class ChatAttachmentRow:
    attachment_guid: str
    mime_type: str | None
    uti: str | None
    total_bytes: int | None
    transfer_state: int | None          # 5 == transfer complete (downloaded)
    transfer_name: str | None
    local_path: str | None              # LOCAL ONLY — never serialized off the relay; staging use only

    @property
    def downloaded(self) -> bool:
        return (self.transfer_state == 5 and self.local_path is not None
                and os.path.isfile(os.path.expanduser(self.local_path)))


def _to_iso(apple_ns) -> str | None:
    if not apple_ns:
        return None
    try:
        unix = int(apple_ns) / 1_000_000_000 + _APPLE_EPOCH
        return datetime.datetime.fromtimestamp(unix, tz=datetime.timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class ChatDb:
    """One short-lived read-only connection per lookup. Never held open, never written."""

    def __init__(self, db_path: str = "~/Library/Messages/chat.db") -> None:
        self._path = os.path.expanduser(db_path)

    def _connect(self) -> sqlite3.Connection:
        if not os.path.isfile(self._path):
            raise ChatDbUnavailable("chatdb_missing")
        try:
            conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True, timeout=_CONNECT_TIMEOUT_S)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return conn
        except sqlite3.Error:
            raise ChatDbUnavailable("chatdb_open_failed")

    def _present_message_cols(self, conn: sqlite3.Connection) -> set[str]:
        rows = conn.execute("SELECT name FROM pragma_table_info('message')").fetchall()
        return {r["name"] for r in rows}

    def get_message_by_guid(self, guid: str) -> ChatMessageRow | None:
        """The single message row for an EXACT provider guid (+ its chat guid via the join), or None."""
        if not guid:
            return None
        guid = _normalize_guid(guid)                    # a prefixed reply_to_guid must match the bare guid
        try:
            conn = self._connect()
            try:
                cols = self._present_message_cols(conn)
                opt = {c: (c in cols) for c in _OPTIONAL_MESSAGE_COLS}
                sel = ["m.ROWID AS rowid", "m.guid AS guid", "m.handle_id AS handle_id",
                       "m.is_from_me AS is_from_me", "m.date AS date"]
                for c in _OPTIONAL_MESSAGE_COLS:
                    sel.append(f"m.{c} AS {c}" if opt[c] else f"NULL AS {c}")
                sql = (f"SELECT {', '.join(sel)}, "
                       "(SELECT c.guid FROM chat c JOIN chat_message_join cmj ON cmj.chat_id=c.ROWID "
                       " WHERE cmj.message_id=m.ROWID LIMIT 1) AS chat_guid "
                       "FROM message m WHERE m.guid = ? LIMIT 1")
                r = conn.execute(sql, (guid,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            raise ChatDbUnavailable("chatdb_query_failed")
        if r is None:
            return None
        return ChatMessageRow(
            guid=r["guid"], chat_guid=r["chat_guid"], sender_handle_id=r["handle_id"],
            is_from_me=bool(r["is_from_me"]), service=r["service"], sent_at=_to_iso(r["date"]),
            reply_to_guid=_normalize_guid(r["reply_to_guid"]),
            thread_originator_guid=_normalize_guid(r["thread_originator_guid"]),
            edited=bool(r["date_edited"]), unsent=bool(r["date_retracted"]),
            has_attachments=bool(r["cache_has_attachments"]))

    def get_attachments_for_message_guid(self, guid: str) -> list[ChatAttachmentRow]:
        """The attachments EXPLICITLY joined to one message guid (bounded). local_path stays local."""
        if not guid:
            return []
        guid = _normalize_guid(guid)                    # match the bare message.guid the reply points at
        try:
            conn = self._connect()
            try:
                sql = ("SELECT a.guid AS aguid, a.mime_type AS mime_type, a.uti AS uti, "
                       "a.total_bytes AS total_bytes, a.transfer_state AS transfer_state, "
                       "a.transfer_name AS transfer_name, a.filename AS filename "
                       "FROM attachment a "
                       "JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID "
                       "JOIN message m ON m.ROWID = maj.message_id "
                       "WHERE m.guid = ? LIMIT ?")
                rows = conn.execute(sql, (guid, _MAX_ATTACHMENTS)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            raise ChatDbUnavailable("chatdb_query_failed")
        return [ChatAttachmentRow(
            attachment_guid=r["aguid"], mime_type=r["mime_type"], uti=r["uti"],
            total_bytes=r["total_bytes"], transfer_state=r["transfer_state"],
            transfer_name=r["transfer_name"], local_path=r["filename"]) for r in rows]

    def get_reply_relationship(self, guid: str) -> tuple[str | None, str | None]:
        """(reply_to_guid, thread_originator_guid) for a message — the explicit reply/thread links."""
        row = self.get_message_by_guid(guid)
        return (None, None) if row is None else (row.reply_to_guid, row.thread_originator_guid)

    def get_message_state(self, guid: str) -> tuple[bool, bool]:
        """(edited, unsent). Unsent content is NOT recovered — macOS already erased the text."""
        row = self.get_message_by_guid(guid)
        return (False, False) if row is None else (row.edited, row.unsent)
