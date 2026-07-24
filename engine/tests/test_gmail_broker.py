"""Gmail as an INHERITED hand (Phase G) — Gmail flows through the SAME ToolBroker as calendar with NO
Gmail-specific brokering code. The three honest verdicts come out of the generic availability()/select():
a send is OK only on a gmail.send-scoped Google connection, INSUFFICIENT_SCOPE on a calendar-only
connection (the exact 'connect gmail' case), and DISCONNECTED with no Google connection at all."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

from bruce_engine import tool_broker
from bruce_engine.runtime_contracts import GoalAction

CAL = "https://www.googleapis.com/auth/calendar.events"
GSEND = "https://www.googleapis.com/auth/gmail.send"
GREAD = "https://www.googleapis.com/auth/gmail.readonly"


def _run(c):
    return asyncio.run(c)


@contextmanager
def _google(connected: bool, *, scopes=()):
    async def _conn(_uid, _provider):
        return tool_broker._Conn(connected=connected, scopes=(tuple(scopes) if connected else ()))
    with patch.object(tool_broker, "_provider_connection", _conn):
        yield


def test_gmail_send_ok_only_when_send_scope_granted():
    with _google(True, scopes=(CAL, GSEND, GREAD)):
        av = _run(tool_broker.availability(uuid4(), "gmail.send_message"))
    assert av.status == tool_broker.OK and av.ok


def test_gmail_send_insufficient_scope_on_calendar_only_connection():
    # the student connected Google for calendar but never granted gmail.send -> honest 'connect gmail',
    # NEVER a fake 'sent'. This is the inherited-hand's most important honest failure.
    with _google(True, scopes=(CAL,)):
        av = _run(tool_broker.availability(uuid4(), "gmail.send_message"))
    assert av.status == tool_broker.INSUFFICIENT_SCOPE
    assert GSEND in av.missing_scopes


def test_gmail_disconnected_when_no_google_connection():
    with _google(False):
        av = _run(tool_broker.availability(uuid4(), "gmail.send_message"))
    assert av.status == tool_broker.DISCONNECTED


def test_select_send_picks_gmail_send_via_generic_path():
    with _google(True, scopes=(CAL, GSEND, GREAD)):
        sel = _run(tool_broker.select(uuid4(), domain="gmail", action=GoalAction.send,
                                      candidate_capabilities=("gmail.send_message",)))
    assert sel.status == tool_broker.OK and sel.chosen.capability == "gmail.send_message"
    assert sel.chosen.arg_schema.get("to") == "str"           # the compact schema rides along for the planner
    assert GSEND in sel.chosen.required_scopes


def test_select_send_reports_scope_gap_not_a_tool():
    with _google(True, scopes=(CAL,)):
        sel = _run(tool_broker.select(uuid4(), domain="gmail", action=GoalAction.send,
                                      candidate_capabilities=("gmail.send_message",)))
    assert sel.status == tool_broker.INSUFFICIENT_SCOPE and sel.chosen is None


def test_not_yet_live_gmail_tools_are_unsupported():
    # resolve_recipient / create_draft / search / monitor are honest live=False -> UNSUPPORTED, never faked
    for cap in ("gmail.resolve_recipient", "gmail.create_draft", "gmail.search_messages", "gmail.monitor_thread"):
        with _google(True, scopes=(CAL, GSEND, GREAD)):
            av = _run(tool_broker.availability(uuid4(), cap))
        assert av.status == tool_broker.UNSUPPORTED, cap


def test_gmail_reply_is_a_write_needing_send_scope():
    with _google(True, scopes=(CAL, GREAD)):        # read-only gmail grant, no send
        av = _run(tool_broker.availability(uuid4(), "gmail.reply_to_thread"))
    assert av.status == tool_broker.INSUFFICIENT_SCOPE and GSEND in av.missing_scopes
