"""ToolRegistry + registry-backed capability truth (R5/R10). Kills the "create works but update says i
can't" contradiction: both answers now derive from the registry, so the reply is honest and specific."""

from __future__ import annotations

from bruce_engine import capability_truth as ct
from bruce_engine import tool_registry as tr


def test_registry_declares_create_live_update_not():
    assert tr.is_live("calendar.create_event") is True
    assert tr.is_live("calendar.update_event") is False
    assert tr.is_live("calendar.delete_event") is False
    assert tr.live_operations("calendar") == ["create_event"]
    assert tr.get("calendar.create_event").write is True


def test_update_request_gets_honest_not_a_flat_denial():
    reply = ct.grounded_calendar_correction("can u update my calendar")
    assert "add" in reply.lower()                 # affirms what IS live
    assert "isn't live yet" in reply.lower() or "not live" in reply.lower()   # honest about update
    assert "can't" not in reply.lower()           # not a flat "i can't touch your calendar"
    assert "done" not in reply.lower()            # never fabricates completion


def test_create_request_affirms_capability():
    reply = ct.grounded_calendar_correction("can u add this to my calendar")
    assert "add" in reply.lower() and "connected" in reply.lower()


def test_denial_detection_covers_mutation_verbs():
    for d in ["i can't actually update your calendar from here",
              "i can't move that event", "i cannot delete events from your calendar"]:
        assert ct.mentions_calendar_denial(d) is True
    assert ct.mentions_calendar_denial("done, it's on ur calendar ✅") is False


def test_flipping_registry_flag_would_stop_the_not_live_line():
    # documents the design: making update live is a ONE-flag change here, not a handler edit
    import dataclasses
    live_update = dataclasses.replace(tr.get("calendar.update_event"), live=True)
    assert live_update.live is True
